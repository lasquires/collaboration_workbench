"""Explicit acknowledged host leases; never portable or inferred from local claims."""
import json
import os
import tempfile
import uuid
from urllib.parse import quote,urlencode
try:
    from .roadmap_store import StoreError
    from .roadmap_cli import ApiError
except ImportError:
    from roadmap_store import StoreError
    from roadmap_cli import ApiError


class WorkspaceLiveClaims:
    def __init__(self,client):
        self.client=client;self.path=client.directory/'host-claims.json'

    def _load(self):
        if not self.path.exists():return {'version':1,'binding':self.client.binding,'state':'idle','review':None,'result':None}
        try:
            if self.path.stat().st_size>256*1024:raise ValueError()
            state=json.loads(self.path.read_text(encoding='utf-8'))
            if state['version']!=1 or state['binding']!=self.client.binding or state['state'] not in ('idle','prepared','pending','acknowledged','rejected'):raise ValueError()
            return state
        except (ValueError,KeyError,OSError):raise StoreError(503,'Private host claim state is unavailable') from None

    def _save(self,state):
        raw=json.dumps(state,sort_keys=True,ensure_ascii=False,allow_nan=False).encode('utf-8')
        if len(raw)>256*1024:raise StoreError(400,'Host claim review exceeds its bound')
        name=None
        try:
            with tempfile.NamedTemporaryFile(dir=self.path.parent,delete=False) as output:
                name=output.name;output.write(raw);output.flush();os.fsync(output.fileno())
            os.replace(name,self.path)
        finally:
            if name and os.path.exists(name):os.unlink(name)

    def status(self):
        with self.client.lock:
            state=self._load();review=state['review']
            return {'scope':'host','state':state['state'],'pending':state['state']=='pending',
                    'review_id':review['review_id'] if review else None,
                    'task_id':review['request']['operations'][0]['task_id'] if review else None,
                    'acknowledgment_is_current_lease':False}

    def _caps(self):
        result=self.client._call('/v1/projects/'+quote(self.client.project_id,safe='')+'/capabilities')
        caps=result.get('capabilities',{}) if isinstance(result,dict) else {}
        if not isinstance(caps,dict):raise StoreError(409,'Host capabilities are invalid')
        if (not isinstance(result,dict) or result.get('project_id')!=self.client.project_id or caps.get('authenticated') is not True
                or not isinstance(caps.get('local_principal_id'),str) or not isinstance(result.get('revision'),str)):
            raise StoreError(409,'Host claim coordination requires authenticated scoped capabilities')
        local=self.client.store.read_v1('capabilities',self.client.project_id)['revision']
        if result['revision'].split(':',1)[0]==local.split(':',1)[0]:raise StoreError(409,'Use this installation directly for its claims')
        return result

    def preview(self,operation):
        if not isinstance(operation,dict):raise StoreError(400,'Expected a host claim operation')
        common={'op','task_id','rationale'}
        extras={'claim_task':{'reserved_paths','lease_seconds','session_id'},'heartbeat_claim':{'lease_seconds'},
                'release_claim':set(),'handoff_claim':{'lease_seconds','session_id','to_principal_id'}}
        kind=operation.get('op')
        if (not isinstance(kind,str) or kind not in extras or not common<=set(operation) or set(operation)-common-extras[kind]
                or not isinstance(operation['task_id'],str) or not 0<len(operation['task_id'])<=200
                or not isinstance(operation['rationale'],str) or not 0<len(operation['rationale'])<=10000):
            raise StoreError(400,'Unsupported host claim operation or fields')
        with self.client.lock:
            state=self._load()
            if state['state']=='pending':raise StoreError(409,'Replay the uncertain host claim before preparing another')
            caps=self._caps()
            context=self.client._call('/v1/tasks/'+quote(operation['task_id'],safe='')+'/context?'+urlencode({'project_id':self.client.project_id}))
            if not isinstance(context,dict) or context.get('project_id')!=self.client.project_id or context.get('revision')!=caps['revision']:
                raise StoreError(409,'Host changed during preview; reread and reconcile')
            task=context.get('context',{}).get('task',{})
            if not isinstance(task.get('id'),str) or type(task.get('record_revision')) is not int:raise StoreError(409,'Host task preview is invalid')
            command={**operation,'task_id':task['id'],'expected_record_revision':task['record_revision']}
            identity=str(uuid.uuid4())
            review={'review_id':identity,'request':{'project_id':self.client.project_id,
                    'actor':{'principal_id':caps['capabilities']['local_principal_id']},
                    'request_id':'host-claim-'+identity,'expected_revision':caps['revision'],'operations':[command]},
                    'task':task,'scope':'host'}
            state.update(state='prepared',review=review,result=None,transport_attempted=False);self._save(state)
            return json.loads(json.dumps(review))

    def submit(self,review_id):
        with self.client.lock:
            state=self._load();review=state['review']
            if not isinstance(review_id,str) or not review or review['review_id']!=review_id:raise StoreError(409,'Host claim review changed; use the exact prepared identity')
            if state['state']=='acknowledged':return {'scope':'host','state':'acknowledged','result':state['result'],'acknowledgment_is_current_lease':False}
            if state['state']=='rejected':raise StoreError(409,'Host rejected this request; prepare a fresh reviewed operation')
            state['state']='pending';self._save(state)
            caps=self._caps()
            if caps['capabilities']['local_principal_id']!=review['request']['actor']['principal_id']:
                raise StoreError(409,'Host credential principal changed; retain the original request and restore its credential')
            # Global and record guards belong to the frozen payload. Never rebase
            # a lost-response retry against a freshly read host revision.
            if caps['revision'].split(':',1)[0]!=review['request']['expected_revision'].split(':',1)[0]:
                raise StoreError(409,'Host installation changed; retain the original request for its original host')
            previously_attempted=state.get('transport_attempted',False)
            state['transport_attempted']=True;self._save(state)
            try:result=self.client._call('/v1/transactions',review['request'])
            except ApiError as error:
                if error.status in (400,404,409,413,422) or error.status in (401,403) and not previously_attempted:
                    state['state']='rejected';self._save(state)
                raise
            if (not isinstance(result,dict) or not isinstance(result.get('revision'),str)
                    or result.get('request_id')!=review['request']['request_id']
                    or result['revision'].split(':',1)[0]!=review['request']['expected_revision'].split(':',1)[0]):
                raise ApiError(0,'Host claim response was not confirmed')
            state.update(state='acknowledged',result=result);self._save(state)
            return {'scope':'host','state':'acknowledged','result':result,'acknowledgment_is_current_lease':False}

    def read(self,task_id,cursor=None):
        if not isinstance(task_id,str) or not 0<len(task_id)<=200:raise StoreError(400,'Expected host task identity')
        self._caps()
        query={'project_id':self.client.project_id,'task_id':task_id}
        if cursor is not None:query['cursor']=cursor
        result=self.client._call('/v1/claims?'+urlencode(query))
        if not isinstance(result,dict) or result.get('project_id')!=self.client.project_id or not isinstance(result.get('items'),list):
            raise ApiError(0,'Host claim list was not confirmed')
        return {**result,'scope':'host','local_claims_unchanged':True}
