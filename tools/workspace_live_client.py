"""Durable portable reconciliation with an optional authenticated project host.

The owning local service writes its SQLite projection. HTTP transports records,
never database files, credentials, local bindings or live leases.
"""
import hashlib
import json
import os
import tempfile
import threading
import uuid
from pathlib import Path
from urllib.parse import urlencode,urlsplit
try:
    from . import roadmap_exchange as exchange
    from .roadmap_cli import request,ApiError
    from .roadmap_store import StoreError
    from .workspace_sync import WorkspaceGitSync
except ImportError:
    import roadmap_exchange as exchange
    from roadmap_cli import request,ApiError
    from roadmap_store import StoreError
    from workspace_sync import WorkspaceGitSync

_LOCKS={}
_GUARD=threading.Lock()

class LiveConflict(RuntimeError):
    def __init__(self,alternatives):
        self.alternatives=alternatives
        super().__init__('Live reconciliation needs explicit review')

def validate_live_origin(url):
    if not isinstance(url,str) or not 0<len(url)<=2048 or any(char.isspace() or char=='\\' for char in url):
        raise ValueError('Live service requires a credential-free HTTPS origin')
    try:
        parsed=urlsplit(url);port=parsed.port
    except ValueError:raise ValueError('Invalid live service origin') from None
    if (parsed.scheme!='https' and not (parsed.scheme=='http' and parsed.hostname in ('127.0.0.1','localhost','::1'))
            or not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment or parsed.path not in ('','/')
            or port is not None and not 1<=port<=65535):
        raise ValueError('Live service requires a credential-free HTTPS origin')
    host='['+parsed.hostname+']' if ':' in parsed.hostname else parsed.hostname
    return parsed.scheme+'://'+host+(':'+str(port) if port is not None else '')


class WorkspaceLiveClient:
    def __init__(self,store,project_id,actor_id,url,token_file,directory,*,requester=request):
        self.store=store;self.project_id=project_id;self.actor_id=actor_id;self.url=validate_live_origin(url)
        self.token_file=Path(token_file);self.directory=Path(directory).resolve();self.requester=requester
        with store._connection() as db:
            if db.execute('SELECT 1 FROM projects WHERE id=?',(project_id,)).fetchone() is None:raise StoreError(404,'Scoped project not found')
            for row in db.execute('SELECT root FROM repository_connections'):
                if self.directory.is_relative_to(Path(row[0]).resolve()):raise ValueError('Live client state must stay outside project repositories')
        self.directory.mkdir(parents=True,exist_ok=True);self.path=self.directory/'live-state.json'
        self.binding=hashlib.sha256((self.url+'\0'+project_id+'\0'+actor_id).encode()).hexdigest()
        with _GUARD:self.lock=_LOCKS.setdefault(str(self.path),threading.RLock())
        self._load()

    def _load(self):
        if not self.path.exists():return {'version':1,'binding':self.binding,'base':None,'outbox':None,'state':'not-joined','error':None}
        try:
            if self.path.stat().st_size>32*1024*1024:raise ValueError()
            state=json.loads(self.path.read_text(encoding='utf-8'))
            if state['binding']!=self.binding or state['version']!=1:raise ValueError()
            return state
        except (ValueError,KeyError,OSError):raise ValueError('Private live-client state is incompatible') from None

    def _save(self,state):
        raw=json.dumps(state,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode('utf-8')
        if len(raw)>32*1024*1024:raise ValueError('Live reconciliation state exceeds its bound')
        name=None
        try:
            with tempfile.NamedTemporaryFile(dir=self.directory,delete=False) as output:
                name=output.name;output.write(raw);output.flush();os.fsync(output.fileno())
            os.replace(name,self.path)
        finally:
            if name and os.path.exists(name):os.unlink(name)

    def status(self):
        state=self._load();job=state['outbox']
        return {'state':state['state'],'claim_scope':'local-only','pending':job is not None or state['state']=='pending','base_id':state['base']['id'] if state['base'] else None,
                'operation_id':job['id'] if job else None,'phase':job['phase'] if job else None,'error':state['error']}

    def _call(self,path,payload=None):return self.requester(self.url,path,payload,token_file=self.token_file)

    def _remote(self,ancestor=None):
        query={'project_id':self.project_id,'portable':'true'}
        if ancestor:query['ancestor_id']=ancestor
        result=self._call('/v1/exchange-export?'+urlencode(query))
        if not isinstance(result,dict) or not isinstance(result.get('bundle'),dict):raise LiveConflict({'side':'host','reason':'Host returned an invalid snapshot envelope'})
        revision=result.get('revision')
        local_revision=self.store.read_v1('capabilities',self.project_id)['revision']
        if not isinstance(revision,str) or ':' not in revision:raise LiveConflict({'side':'host','reason':'Host returned an invalid service revision'})
        if revision.split(':',1)[0]==local_revision.split(':',1)[0]:
            raise LiveConflict({'reason':'This is the same installation; use its host service directly'})
        bundle=result['bundle']
        if (bundle.get('format'),bundle.get('version'),bundle.get('project_id'))!=('workbench-exchange',2,self.project_id):
            raise LiveConflict({'reason':'Host portable project or format mismatch'})
        try:
            with self.store._connection() as db:exchange._validate(db,self.project_id,bundle)
        except (StoreError,ValueError,TypeError,KeyError):
            raise LiveConflict({'side':'host','reason':'Host snapshot failed project/schema/content validation'}) from None
        return bundle

    def _export(self,ancestor=None):
        bundle=self.store.exchange_preview(self.project_id,ancestor_id=ancestor,portable=True)['bundle']
        state=self._load()
        return WorkspaceGitSync._portable(bundle,state['base'])

    @staticmethod
    def _same(a,b):
        # Unreferenced local actor profiles are not shared membership changes.
        records=lambda bundle:{k:v for k,v in bundle['records'].items() if k!='principals'}
        history=lambda bundle:[event for event in bundle['history'] if event['kind']!='import_bundle']
        return records(a)==records(b) and history(a)==history(b)

    def _payload(self,request_id,revision,operation,principal):
        return {'project_id':self.project_id,'actor':{'principal_id':principal},'request_id':request_id,
                'expected_revision':revision,'operations':[operation]}

    def preview_join(self):
        with self.lock:
            current=self._load()
            if current['base'] is not None or current['outbox'] is not None:raise LiveConflict({'reason':'Retry an existing join or pending operation before creating another join'})
            bundle=self._remote();envelope=self.store.exchange_preview(self.project_id,bundle=bundle)
            return {'bundle':bundle,'local_revision':envelope['revision'],'preview':envelope['preview']}

    def join(self,review,choices):
        with self.lock:
            state=self._load()
            if state['base'] is not None or state['outbox'] is not None:raise LiveConflict({'reason':'Retry the existing join or pending operation before creating another join'})
            bundle=self._remote()
            if bundle['id']!=review['bundle']['id']:raise LiveConflict({'reason':'Host changed since join preview'})
            identity=str(uuid.uuid4())
            state['outbox']={'id':identity,'phase':'apply-local','incoming':bundle,'joining':True,
                'local_request':self._payload('live-join-'+identity,review['local_revision'],{'op':'import_bundle','bundle':bundle,'choices':choices},self.actor_id)}
            self._save(state)
            return self._drain(state)

    def _local_plan(self,state,job,bundle):
        preview=self.store.exchange_preview(self.project_id,bundle=bundle)
        conflicts=[change for change in preview['preview']['changes'] if any(field['conflict'] for field in change['fields'].values())]
        job['incoming']=bundle
        if conflicts:
            job['phase']='review-local';job['review']=preview;state['state']='review-required';self._save(state)
            raise LiveConflict({'side':'client','operation_id':job['id'],'revision':preview['revision'],'preview':preview['preview']})
        job['local_request']=self._payload('live-pull-'+job['id']+'-'+bundle['id'],preview['revision'],{'op':'import_bundle','bundle':bundle,'choices':{}},self.actor_id)
        job['phase']='apply-local';self._save(state)

    def _drain(self,state):
        for _ in range(8):
            job=state['outbox'];phase=job['phase']
            if phase=='cache-outgoing':
                if not job.get('local_request'):
                    revision=self.store.read_v1('capabilities',self.project_id)['revision']
                    job['local_request']=self._payload('live-cache-'+job['id']+'-'+uuid.uuid4().hex,revision,{'op':'register_exchange_snapshot','bundle':job['outgoing']},self.actor_id);self._save(state)
                self.store.transact_v1(job['local_request']);job.pop('local_request',None);job['phase']='register-base';self._save(state)
            elif phase=='register-base':
                if not job.get('remote_request'):
                    caps=self._call('/v1/projects/'+self.project_id+'/capabilities')
                    actor=caps['capabilities']['local_principal_id']
                    if job.get('remote_actor') and job['remote_actor']!=actor:
                        state.update(state='access-error',error='Host credential identity changed; the old operation is retained');self._save(state)
                        raise LiveConflict({'reason':'Host credential identity changed'})
                    job['remote_actor']=actor
                    job['remote_request']=self._payload('live-base-'+job['id']+'-'+uuid.uuid4().hex,caps['revision'],{'op':'register_exchange_snapshot','bundle':state['base']},job['remote_actor']);self._save(state)
                self._call('/v1/transactions',job['remote_request']);job.pop('remote_request',None);job['phase']='preview-host';self._save(state)
            elif phase=='preview-host':
                preview=self._call('/v1/exchange-preview',{'project_id':self.project_id,'bundle':job['outgoing']})
                conflicts=[change for change in preview['preview']['changes'] if any(field['conflict'] for field in change['fields'].values())]
                if conflicts:
                    job['phase']='review-host';job['review']=preview;state['state']='review-required';self._save(state)
                    raise LiveConflict({'side':'host','operation_id':job['id'],'revision':preview['revision'],'preview':preview['preview']})
                job['remote_request']=self._payload('live-push-'+job['id'],preview['revision'],{'op':'import_bundle','bundle':job['outgoing'],'choices':{}},job['remote_actor'])
                job['phase']='post';self._save(state)
            elif phase=='post':
                self._call('/v1/transactions',job['remote_request']);job['phase']='pull';self._save(state)
            elif phase=='pull':self._local_plan(state,job,self._remote(job['outgoing']['id']))
            elif phase=='apply-local':
                self.store.transact_v1(job['local_request'])
                state['base']=job['incoming'];state['outbox']=None;state['error']=None;state['state']='current';self._save(state)
                if not self._same(self._export(state['base']['id']),state['base']):state['state']='pending';self._save(state)
                return self.status()
            elif phase.startswith('review-'):
                raise LiveConflict({'side':phase.removeprefix('review-'),'operation_id':job['id'],'revision':job['review']['revision'],'preview':job['review']['preview']})
            else:raise ValueError('Unknown durable live reconciliation phase')
        raise ValueError('Live reconciliation transition bound exceeded')

    @staticmethod
    def _choices(preview,supplied):
        expected={json.dumps([change['table'],change['key'],field],separators=(',',':'),ensure_ascii=False)
                  for change in preview['changes'] for field,value in change['fields'].items() if value['conflict']}
        if not isinstance(supplied,dict) or set(supplied)!=expected or any(value not in ('local','remote') for value in supplied.values()):
            raise ValueError('Choose exactly one side for every conflicting field')
        return json.loads(json.dumps(supplied))

    def review_host(self):
        with self.lock:
            state=self._load();job=state['outbox']
            if not job:raise LiveConflict({'reason':'No pending host reconciliation'})
            if job['phase']=='post' and state.get('last_http_status')==409:
                old=job.pop('remote_request');job.setdefault('uncommitted_attempts',[]).append({'request_id':old['request_id'],'revision':old['expected_revision'],'choices':old['operations'][0].get('choices',{})})
                job['phase']='review-host'
            if job['phase'] not in ('preview-host','review-host'):raise LiveConflict({'reason':'Retry the frozen pending request before changing its choices'})
            envelope=self._call('/v1/exchange-preview',{'project_id':self.project_id,'bundle':job['outgoing']})
            job['phase']='review-host';job['review']=envelope;state['state']='review-required';self._save(state)
            return {'operation_id':job['id'],'revision':envelope['revision'],'preview':envelope['preview']}

    def resolve_host(self,review,choices):
        with self.lock:
            state=self._load();job=state['outbox']
            if not job or job['phase']!='review-host' or job['id']!=review['operation_id']:raise LiveConflict({'reason':'Pending host operation changed'})
            current=self._call('/v1/exchange-preview',{'project_id':self.project_id,'bundle':job['outgoing']})
            if current['revision']!=review['revision'] or current['preview']!=review['preview']:
                raise LiveConflict({'side':'host','reason':'Host changed since review','revision':current['revision'],'preview':current['preview']})
            selected=self._choices(current['preview'],choices)
            job['remote_request']=self._payload('live-reviewed-'+str(uuid.uuid4()),current['revision'],{'op':'import_bundle','bundle':job['outgoing'],'choices':selected},job['remote_actor'])
            job['phase']='post';state.pop('last_http_status',None);state['state']='pending';self._save(state)
            return self.sync_once()

    def review_local(self):
        with self.lock:
            state=self._load();job=state['outbox']
            if not job or job['phase'] not in ('review-local','apply-local'):raise LiveConflict({'reason':'No pending local reconciliation'})
            if job['phase']=='apply-local' and not state.get('last_local_conflict'):
                raise LiveConflict({'reason':'Retry the uncertain local request before changing its choices'})
            preview=self.store.exchange_preview(self.project_id,bundle=job['incoming'])
            job['phase']='review-local';job['review']=preview;job.pop('local_request',None);self._save(state)
            return {'operation_id':job['id'],'revision':preview['revision'],'preview':preview['preview']}

    def resolve_local(self,review,choices):
        with self.lock:
            state=self._load();job=state['outbox']
            if not job or job['phase']!='review-local' or job['id']!=review['operation_id']:raise LiveConflict({'reason':'Pending local operation changed'})
            current=self.store.exchange_preview(self.project_id,bundle=job['incoming'])
            if current['revision']!=review['revision'] or current['preview']!=review['preview']:
                raise LiveConflict({'side':'client','reason':'Local state changed since review','revision':current['revision'],'preview':current['preview']})
            selected=self._choices(current['preview'],choices)
            job['local_request']=self._payload('live-local-reviewed-'+str(uuid.uuid4()),current['revision'],{'op':'import_bundle','bundle':job['incoming'],'choices':selected},self.actor_id)
            job['phase']='apply-local';state.pop('last_local_conflict',None);state['state']='pending';self._save(state)
            return self.sync_once()

    def sync_once(self):
        with self.lock:
            state=self._load()
            if state['base'] is None and state['outbox'] is None:raise LiveConflict({'reason':'Preview and join the host before live synchronization'})
            try:
                if state['outbox'] is None:
                    current=self._export(state['base']['id'])
                    if self._same(current,state['base']):
                        incoming=self._remote(state['base']['id'])
                        if self._same(current,incoming):state.update(state='current',error=None);self._save(state);return self.status()
                        job={'id':str(uuid.uuid4()),'phase':'pull','outgoing':current};state['outbox']=job
                        self._local_plan(state,job,incoming)
                    else:
                        state['outbox']={'id':str(uuid.uuid4()),'phase':'cache-outgoing','outgoing':current}
                        state['state']='pending';self._save(state)
                return self._drain(state)
            except LiveConflict:
                latest=self._load();latest.update(state='review-required',error='Live reconciliation needs review; local and pending records are retained');self._save(latest)
                raise
            except ApiError as error:
                job=state['outbox']
                if error.status==409 and job and job['phase']=='register-base' and 'revision changed' in str(error).lower():
                    old=job.pop('remote_request')
                    job.setdefault('uncommitted_attempts',[]).append({'request_id':old['request_id'],'revision':old['expected_revision'],'operation':'register_exchange_snapshot'})
                    state.update(state='pending',error='Host advanced during immutable base registration; retry will reread its revision')
                    self._save(state);return self.status()
                state['state']='offline' if error.status==0 or error.status>=500 else 'review-required' if error.status==409 else 'access-error'
                state['error']='Host request was not confirmed; pending changes and exact request identity are retained'
                state['last_http_status']=error.status;self._save(state);return self.status()
            except StoreError as error:
                job=state['outbox']
                if error.status==409 and job and job['phase']=='cache-outgoing' and 'revision changed' in str(error).lower():
                    old=job.pop('local_request')
                    job.setdefault('uncommitted_attempts',[]).append({'request_id':old['request_id'],'revision':old['expected_revision'],'operation':'register_exchange_snapshot'})
                    state.update(state='pending',error='Local state advanced during immutable snapshot registration; retry will reread its revision')
                    self._save(state);return self.status()
                state['state']='review-required' if error.status==409 else 'local-store-unavailable'
                state['error']='Local reconciliation was not confirmed; pending records are retained';state['last_local_conflict']=error.status==409;self._save(state)
                if error.status==409:raise LiveConflict({'side':'client','reason':'Local revision changed; review pending changes'}) from None
                return self.status()
