"""Private, service-owned scheduling for explicitly selected project Git sharing."""
import hashlib
import json
import os
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
try:
    from .roadmap_store import StoreError
    from .workspace_project_contract import validate_manifest,resolve_project_remote
    from .workspace_sync import WorkspaceGitSync,SyncConflict,PrivacyBlocked
    from .workspace_git_transport import TransportError
except ImportError:
    from roadmap_store import StoreError
    from workspace_project_contract import validate_manifest,resolve_project_remote
    from workspace_sync import WorkspaceGitSync,SyncConflict,PrivacyBlocked
    from workspace_git_transport import TransportError

_LOCKS={}
_LOCK_GUARD=threading.Lock()

class WorkspaceSyncService:
    def __init__(self,store,engine_factory=WorkspaceGitSync):
        self.store=store;self.engine_factory=engine_factory
        self.home=Path(store.path).resolve().parent/'git-sync'

    def _connection(self,project,identity):
        if any(not isinstance(value,str) or not 0<len(value)<=200 for value in (project,identity)):raise StoreError(400,'Expected project and repository identifiers')
        with self.store._connection() as db:
            row=db.execute('SELECT * FROM repository_connections WHERE project_id=? AND id=?',(project,identity)).fetchone()
            if row is None:raise StoreError(404,'Scoped repository connection not found')
            return dict(row)

    def _path(self,project,identity):
        # IDs come from the scoped database row; hash them for filesystem names.
        key=hashlib.sha256((project+'\0'+identity).encode()).hexdigest()
        return self.home/key/'settings.json'

    @contextmanager
    def _locked(self,project,identity):
        self._connection(project,identity)
        path=self._path(project,identity)
        with _LOCK_GUARD:lock=_LOCKS.setdefault(str(path),threading.Lock())
        if not lock.acquire(blocking=False):raise StoreError(409,'Git synchronization is busy; retry after its current job')
        try:yield
        finally:lock.release()

    def _load(self,project,identity):
        path=self._path(project,identity)
        if not path.exists():
            return dict(enabled=False,export_selected=False,requested=False,state='off',error=None,
                        observed=None,last_attempt=0,retry_after=0,result=None)
        try:
            if path.stat().st_size>8*1024*1024:raise ValueError()
            state=json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(state,dict) or type(state.get('enabled')) is not bool:raise ValueError()
            return state
        except (OSError,ValueError):raise StoreError(503,'Local Git synchronization settings are unavailable') from None

    def _save(self,project,identity,state):
        path=self._path(project,identity);path.parent.mkdir(parents=True,exist_ok=True)
        temporary=None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent,delete=False) as output:
                temporary=output.name;output.write(json.dumps(state,sort_keys=True,allow_nan=False).encode())
                output.flush();os.fsync(output.fileno())
            os.replace(temporary,path)
        finally:
            if temporary and os.path.exists(temporary):os.unlink(temporary)
        # Runtime status is local connection metadata, not shared task progress.
        with self.store._connection() as db:
            db.execute('UPDATE repository_connections SET sync_state=? WHERE project_id=? AND id=?',(state['state'],project,identity))

    def _binding(self,project,identity):
        connection=self._connection(project,identity);root=Path(connection['root']).resolve()
        if self.home.resolve().is_relative_to(root):raise StoreError(400,'App Git storage must be outside the connected repository')
        path=root/'.research-workspace/project.json'
        if not path.resolve().is_relative_to(root) or not path.is_file() or path.stat().st_size>65536:
            raise StoreError(400,'Project manifest is unavailable or outside the repository')
        manifest=validate_manifest(json.loads(path.read_text(encoding='utf-8-sig')))
        if manifest['project_id']!=project:raise StoreError(409,'Connected project identity changed')
        config=manifest.get('synchronization') or resolve_project_remote(root)
        actual=resolve_project_remote(root,config.get('remote_name'))
        if config.get('repository')!=actual['repository']:raise StoreError(409,'Project remote changed; review synchronization setup')
        name=actual['remote_name']
        if name is None:raise StoreError(400,'Choose a Git remote in this project before enabling sharing')
        result=subprocess.run(['git','-C',str(root),'remote','get-url',name],capture_output=True,timeout=10)
        if result.returncode or len(result.stdout)>8192:raise StoreError(400,'Project remote could not be read')
        remote=os.fsdecode(result.stdout).strip()
        binding=hashlib.sha256((remote+'\0'+config.get('data_ref','refs/heads/workbench-data')).encode()).hexdigest()
        interval=config.get('min_interval_seconds',300);quiet=config.get('quiet_seconds',30)
        if interval<180:raise StoreError(400,'Automatic Git interval must be at least three minutes')
        return remote,binding,config.get('data_ref','refs/heads/workbench-data'),interval,quiet

    def _engine(self,project,identity,state):
        remote,binding,ref,interval,quiet=self._binding(project,identity)
        if binding!=state['binding']:raise StoreError(409,'Project Git destination changed; review setup before syncing')
        actor=self.store.read_v1('capabilities',project)['capabilities']['local_principal_id']
        return self.engine_factory(self.store,project,actor,remote,self._path(project,identity).parent/('cache-'+binding),
            export_selected=state['export_selected'],data_ref=ref,quiet_seconds=quiet,interval_seconds=interval)

    def configure(self,project,identity,*,enabled,export_selected=False):
        if type(enabled) is not bool or type(export_selected) is not bool:raise StoreError(400,'Sharing settings must be boolean')
        with self._locked(project,identity):
            state=self._load(project,identity)
            if enabled:
                if not export_selected:raise StoreError(400,'Explicit portable export selection is required')
                _,binding,ref,interval,quiet=self._binding(project,identity)
                state.update(binding=binding,data_ref=ref,interval_seconds=interval,quiet_seconds=quiet)
            state.update(enabled=enabled,export_selected=export_selected if enabled else False,
                         requested=False,state='pending' if enabled else 'off',error=None,retry_after=0)
            self._save(project,identity,state)
        return self.read(project,identity)

    def read(self,project,identity):
        self._connection(project,identity);state=self._load(project,identity)
        return {key:value for key,value in state.items() if key not in ('binding','observed')}

    def request_sync(self,project,identity):
        with self._locked(project,identity):
            state=self._load(project,identity)
            if not state['enabled']:raise StoreError(409,'Enable this project Git sharing first')
            state.update(requested=True,state='queued',retry_after=0,error=None)
            self._save(project,identity,state)
        return self.read(project,identity)

    def preview(self,project,identity):
        with self._locked(project,identity):
            state=self._load(project,identity)
            if not state['enabled']:raise StoreError(409,'Git sharing is off')
            return self._engine(project,identity,state).preview_remote()

    def apply(self,project,identity,choices,remote_tip,bundle_id,expected_revision):
        with self._locked(project,identity):
            state=self._load(project,identity)
            if not state['enabled']:raise StoreError(409,'Git sharing is off')
            result=self._engine(project,identity,state).apply_remote(choices,remote_tip,bundle_id,expected_revision)
            state.update(state='queued',requested=True,error=None,result=asdict(result),retry_after=0)
            self._save(project,identity,state)
        return self.read(project,identity)

    def tick(self,now=None):
        now=time.time() if now is None else now
        with self.store._connection() as db:
            connections=[dict(row) for row in db.execute('SELECT project_id,id FROM repository_connections')]
        for row in connections:
            project,identity=row['project_id'],row['id']
            try:
                with self._locked(project,identity):
                    state=self._load(project,identity)
                    if not state['enabled'] or (state['state']=='review-required' and not state['requested']) or now<state.get('retry_after',0):continue
                    try:
                        engine=self._engine(project,identity,state)
                        bundle=engine._portable(engine._export())
                        fingerprint=hashlib.sha256(json.dumps({'records':bundle['records'],'history':bundle['history']},sort_keys=True).encode()).hexdigest()
                        if fingerprint!=state['observed']:
                            engine.note_dirty(now=now);state['observed']=fingerprint;state['state']='pending'
                        due=engine.due(now=now)
                        schedule=engine.status()
                        quiet=schedule['dirty_since'] is None or now-schedule['dirty_since']>=state['quiet_seconds']
                        poll=quiet and now-state['last_attempt']>=state['interval_seconds']
                        if not state['requested'] and not due and not poll:
                            self._save(project,identity,state);continue
                        state.update(state='syncing',last_attempt=now,requested=True)
                        self._save(project,identity,state)
                        result=engine.sync_now(now=now)
                        state.update(state='pending' if result.pending_local else 'current',requested=False,error=None,result=asdict(result),retry_after=0)
                    except SyncConflict as exc:
                        state.update(state='review-required',requested=False,error='Explicit merge review required',alternatives=exc.alternatives)
                    except (StoreError,PrivacyBlocked,TransportError,ValueError,OSError,subprocess.SubprocessError):
                        # Never persist an OS/Git error containing an authenticated URL.
                        state.update(state='failed',requested=False,error='Git sharing could not finish; check the project binding, portable records and Git access',retry_after=now+60)
                    self._save(project,identity,state)
            except StoreError as exc:
                if exc.status!=409:raise
