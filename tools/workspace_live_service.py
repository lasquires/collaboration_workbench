"""Project-scoped private live-host configuration and reconciliation worker."""
import hashlib
import json
import os
import re
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
try:
    from .roadmap_store import StoreError
    from .roadmap_cli import ApiError
    from .workspace_project_contract import validate_manifest
    from .workspace_live_client import WorkspaceLiveClient,LiveConflict,validate_live_origin
    from .workspace_live_claims import WorkspaceLiveClaims
except ImportError:
    from roadmap_store import StoreError
    from roadmap_cli import ApiError
    from workspace_project_contract import validate_manifest
    from workspace_live_client import WorkspaceLiveClient,LiveConflict,validate_live_origin
    from workspace_live_claims import WorkspaceLiveClaims

_LOCKS={}
_GUARD=threading.Lock()

class WorkspaceLiveService:
    def __init__(self,store,client_factory=WorkspaceLiveClient):
        self.store=store;self.client_factory=client_factory
        self.home=Path(store.path).resolve().parent/'live-sync'

    def _project(self,project):
        if not isinstance(project,str) or not 0<len(project)<=200:raise StoreError(400,'Expected project identifier')
        with self.store._connection() as db:
            if db.execute('SELECT 1 FROM projects WHERE id=? AND archived_at IS NULL',(project,)).fetchone() is None:raise StoreError(404,'Scoped project not found')

    def _path(self,project):return self.home/hashlib.sha256(project.encode()).hexdigest()/'settings.json'

    @contextmanager
    def _locked(self,project):
        self._project(project)
        with _GUARD:lock=_LOCKS.setdefault(str(self._path(project)),threading.RLock())
        if not lock.acquire(blocking=False):raise StoreError(409,'Live reconciliation is busy; retry after the current operation')
        try:yield
        finally:lock.release()

    def _load(self,project):
        path=self._path(project)
        if not path.exists():return {'version':1,'project_id':project,'enabled':False,'state':'off','error':None,'last_status':None,'retry_after':0}
        try:
            if path.stat().st_size>65536:raise ValueError()
            value=json.loads(path.read_text(encoding='utf-8'))
            if value['version']!=1 or value['project_id']!=project or type(value['enabled']) is not bool:raise ValueError()
            return value
        except (OSError,ValueError,KeyError):raise StoreError(503,'Private live connection settings are unavailable') from None

    def _save(self,project,value):
        path=self._path(project);path.parent.mkdir(parents=True,exist_ok=True);name=None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent,delete=False) as output:
                name=output.name;output.write(json.dumps(value,sort_keys=True,allow_nan=False).encode('utf-8'));output.flush();os.fsync(output.fileno())
            os.replace(name,path)
        finally:
            if name and os.path.exists(name):os.unlink(name)

    def _roots(self):
        with self.store._connection() as db:return [Path(row[0]).resolve() for row in db.execute('SELECT root FROM repository_connections')]

    def _bound(self,project):
        with self.store._connection() as db:roots=[Path(row[0]).resolve() for row in db.execute('SELECT root FROM repository_connections WHERE project_id=?',(project,))]
        if not roots:raise StoreError(400,'Connect this project repository before configuring a live host')
        for root in roots:
            path=root/'.research-workspace/project.json'
            try:
                if not path.resolve().is_relative_to(root) or not path.is_file() or path.stat().st_size>65536:raise ValueError()
                manifest=validate_manifest(json.loads(path.read_text(encoding='utf-8-sig')))
                if manifest['project_id']!=project:raise ValueError()
                if self.home.resolve().is_relative_to(root):raise ValueError()
            except (ValueError,OSError):raise StoreError(409,'Project integration changed; review setup before reconnecting') from None

    def _credential(self,value):
        if not isinstance(value,str) or not 0<len(value)<=4000 or '\0' in value:raise StoreError(400,'Private credential file required')
        path=Path(value).expanduser().resolve()
        if any(path.is_relative_to(root) for root in self._roots()+[Path(__file__).resolve().parents[1]]):raise StoreError(400,'Keep live credentials outside project and application repositories')
        try:
            if not path.is_file() or not 32<=path.stat().st_size<=258:raise ValueError()
            token=path.read_text(encoding='ascii').strip()
            if not re.fullmatch(r'[A-Za-z0-9_-]{32,256}',token):raise ValueError()
        except (OSError,ValueError):raise StoreError(400,'Private credential file is unavailable or malformed') from None
        return str(path)

    def _endpoint(self,project,config):
        return self._path(project).parent/('endpoint-'+hashlib.sha256(config['url'].encode()).hexdigest())

    def _client(self,project,config):
        directory=self._endpoint(project,config)
        return self.client_factory(self.store,project,config['actor_id'],config['url'],config['token_file'],directory)

    def _claim_status(self,project,config):
        if config.get('url') and (self._endpoint(project,config)/'host-claims.json').exists():
            return WorkspaceLiveClaims(self._client(project,config)).status()
        return {'scope':'host','state':'idle','pending':False,'review_id':None,'task_id':None,'acknowledgment_is_current_lease':False}

    def _claim_control(self,project,method,*args):
        with self._locked(project):
            config=self._load(project)
            if not config['enabled']:raise StoreError(409,'Enable the project live connection first')
            self._bound(project)
            return getattr(WorkspaceLiveClaims(self._client(project,config)),method)(*args)

    def claims_read(self,project,task_id,cursor=None):return self._claim_control(project,'read',task_id,cursor)
    def claims_preview(self,project,operation):return self._claim_control(project,'preview',operation)
    def claims_submit(self,project,review_id):return self._claim_control(project,'submit',review_id)

    def configure(self,project,*,enabled,url=None,token_file=None):
        if type(enabled) is not bool:raise StoreError(400,'Live connection enabled must be boolean')
        with self._locked(project):
            config=self._load(project)
            if enabled:
                self._bound(project)
                try:origin=validate_live_origin(url if url is not None else config.get('url'))
                except ValueError:raise StoreError(400,'Live host requires a credential-free HTTPS origin') from None
                if config.get('url') and origin!=config['url']:
                    if self._client(project,config).status()['pending'] or self._claim_status(project,config)['pending']:raise StoreError(409,'Retry or resolve the pending old-host operation before replacing its endpoint')
                    if token_file is None:raise StoreError(400,'Choose a credential explicitly for the new host')
                credential=self._credential(token_file if token_file is not None else config.get('token_file'))
                actor=self.store.read_v1('capabilities',project)['capabilities']['local_principal_id']
                config.update(url=origin,token_file=credential,actor_id=actor)
                status=self._client(project,config).status()
                config.update(last_status=status,state='join-required' if status['state']=='not-joined' else status['state'])
            else:config['state']='off'
            config.update(enabled=enabled,error=None,retry_after=0);self._save(project,config)
        return self.read(project)

    def read(self,project):
        self._project(project);config=self._load(project)
        status=config.get('last_status') or {'pending':False,'phase':None,'claim_scope':'local-only','base_id':None}
        result={**status,'enabled':config['enabled'],'state':config['state'],'error':config.get('error') or status.get('error')}
        result['host_claims']=self._claim_status(project,config)
        if config.get('url'):result['url']=config['url']
        result['preferred_transport']='live' if config['enabled'] and result['state'] in ('current','pending','review-required') else 'git-fallback' if config['enabled'] else 'local'
        return result

    def _control(self,project,method,*args):
        with self._locked(project):
            config=self._load(project)
            if not config['enabled']:raise StoreError(409,'Enable the project live connection first')
            self._bound(project);client=self._client(project,config)
            try:
                result=getattr(client,method)(*args)
                status=client.status();config.update(last_status=status,state='join-required' if status['state']=='not-joined' else status['state'],error=None,retry_after=0);self._save(project,config)
                return result
            except LiveConflict:
                status=client.status();sharing=status['state'].endswith('sharing-review-required')
                config.update(last_status=status,state=status['state'] if sharing else 'review-required',error=status.get('error') if sharing else 'Live reconciliation needs explicit review');self._save(project,config);raise
            except ApiError:
                config.update(last_status=client.status(),state='offline',error='Host request was not confirmed; pending work is retained');self._save(project,config);raise

    def preview_join(self,project):return self._control(project,'preview_join')
    def join(self,project,review,choices):
        if (not isinstance(review,dict) or not isinstance(review.get('bundle'),dict) or not isinstance(review['bundle'].get('id'),str)
                or not isinstance(review.get('local_revision'),str) or not isinstance(choices,dict)):
            raise StoreError(400,'Join requires a reviewed host snapshot, local revision and field choices')
        return self._control(project,'join',review,choices)
    def sync_now(self,project):return self._control(project,'sync_once')
    def review(self,project,side):
        if side not in ('host','local'):raise StoreError(400,'Review side must be host or local')
        return self._control(project,'review_'+side)
    def resolve(self,project,side,review,choices):
        if side not in ('host','local'):raise StoreError(400,'Review side must be host or local')
        if (not isinstance(review,dict) or not isinstance(review.get('operation_id'),str) or not isinstance(review.get('revision'),str)
                or not isinstance(review.get('preview'),dict) or not isinstance(choices,dict)):
            raise StoreError(400,'Resolution requires the exact preview and field choices')
        return self._control(project,'resolve_'+side,review,choices)

    def tick(self,now=None):
        now=time.time() if now is None else now;available=set()
        with self.store._connection() as db:projects=[row[0] for row in db.execute('SELECT id FROM projects WHERE archived_at IS NULL')]
        for project in projects:
            try:
                with self._locked(project):
                    config=self._load(project)
                    if not config['enabled']:continue
                    if now<config.get('retry_after',0):
                        if config['state'] in ('current','pending','review-required'):available.add(project)
                        continue
                    try:
                        self._bound(project);client=self._client(project,config);status=client.status()
                        if status['state']=='not-joined' and not status['pending']:
                            config.update(state='join-required',last_status=status,error=None,retry_after=now+2)
                        else:
                            try:status=client.sync_once()
                            except LiveConflict:
                                status=client.status()
                                if not status['state'].endswith('sharing-review-required'):status['state']='review-required'
                            config.update(last_status=status,state=status['state'],error=status.get('error'),retry_after=now+(10 if status['state']=='offline' else 2))
                    except (StoreError,ValueError,OSError,ApiError):
                        config.update(state='failed',error='Live connection paused; check project setup, host access and the private credential file',retry_after=now+10)
                    self._save(project,config)
                    if config['state'] in ('current','pending','review-required'):available.add(project)
            except StoreError as error:
                if error.status!=409:raise
        return available
