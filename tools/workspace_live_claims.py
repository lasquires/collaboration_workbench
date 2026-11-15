"""Explicit acknowledged host leases; never portable or inferred from local claims."""
import json
import hashlib
import threading
from datetime import datetime,timezone
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


_RECEIPT_GUARD=threading.Lock()
_RECEIPT_LOCKS={}

class WorkspaceLiveClaims:
    def __init__(self,client):
        self.client=client;self.path=client.directory/'host-claims.json'
        with client.store._connection() as db:store_id=db.execute('SELECT store_id FROM store_meta').fetchone()[0]
        self.local_binding={'store':store_id,'project':client.project_id,'actor':client.actor_id}
        key=hashlib.sha256(json.dumps(self.local_binding,sort_keys=True).encode()).hexdigest()
        self.receipts_path=client.directory.parent/('host-claim-outcomes-'+key+'.json')
        with _RECEIPT_GUARD:self.receipt_lock=_RECEIPT_LOCKS.setdefault(str(self.receipts_path),threading.RLock())

    def _receipts(self):
        if not self.receipts_path.exists():return []
        try:
            if self.receipts_path.is_symlink() or self.receipts_path.stat().st_size>2*1024*1024:raise ValueError()
            data=json.loads(self.receipts_path.read_text(encoding='utf-8'))
            if not isinstance(data,dict) or set(data)!={'version','binding','items'} or type(data['version']) is not int or data['version']!=1 or data['binding']!=self.local_binding or not isinstance(data['items'],list) or len(data['items'])>256:raise ValueError()
            seen=set()
            for row in data['items']:
                if not isinstance(row,dict) or set(row)!={'review_id','phase','request_id','remote_actor','remote_store','url','revision','recorded_at'}:raise ValueError()
                if any(not isinstance(value,str) or not 0<len(value)<=(4000 if key=='url' else 300) for key,value in row.items()):raise ValueError()
                if row['phase'] not in ('acknowledged','rejected') or row['review_id'] in seen or row['request_id']!='host-claim-'+row['review_id'] or row['remote_store']!=row['revision'].split(':',1)[0]:raise ValueError()
                seen.add(row['review_id'])
            return data['items']
        except (ValueError,KeyError,TypeError,OSError):raise StoreError(503,'Private host claim outcomes are unavailable; no confirmation was discarded') from None

    @staticmethod
    def _atomic(path,raw):
        name=None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent,delete=False) as output:
                name=output.name;output.write(raw);output.flush();os.fsync(output.fileno())
            os.replace(name,path)
        finally:
            if name and os.path.exists(name):os.unlink(name)

    def _retain(self,state):
        if state['state'] not in ('acknowledged','rejected'):return
        review=state['review'];request=review['request'];phase=state['state']
        rows=self._receipts();prior=next((row for row in rows if row['review_id']==review['review_id']),None)
        row={'review_id':review['review_id'],'phase':phase,'request_id':request['request_id'],
             'remote_actor':request['actor']['principal_id'],'remote_store':request['expected_revision'].split(':',1)[0],
             'url':self.client.url,'revision':state['result']['revision'] if phase=='acknowledged' else request['expected_revision'],
             'recorded_at':datetime.now(timezone.utc).isoformat()}
        if prior:
            if any(prior[key]!=row[key] for key in row if key!='recorded_at'):raise StoreError(409,'Stored host claim outcome conflicts with this command')
            return
        raw=json.dumps({'version':1,'binding':self.local_binding,'items':(rows+[row])[-256:]},sort_keys=True,ensure_ascii=False,allow_nan=False).encode('utf-8')
        if len(raw)>2*1024*1024:raise StoreError(413,'Host claim outcome ledger exceeds its bound')
        self._atomic(self.receipts_path,raw)

    @staticmethod
    def _confirmation(row):
        return {'scope':'host','state':row['phase'],'review_id':row['review_id'],
                'result':{'request_id':row['request_id'],'revision':row['revision']} if row['phase']=='acknowledged' else None,
                'command_receipt':dict(row),'historical_only':True,'current_access_checked':False,
                'not_applied':row['phase']=='rejected','acknowledgment_is_current_lease':False}

    def _load(self):
        receipts=self._receipts()
        if not self.path.exists():return {'version':1,'binding':self.client.binding,'state':'idle','review':None,'result':None}
        try:
            if self.path.stat().st_size>256*1024:raise ValueError()
            state=json.loads(self.path.read_text(encoding='utf-8'))
            if state['version']!=1 or state['binding']!=self.client.binding or state['state'] not in ('idle','prepared','pending','acknowledged','rejected'):raise ValueError()
            if 'transport_attempted' in state and type(state['transport_attempted']) is not bool:raise ValueError()
            review=state.get('review')
            receipt=next((row for row in receipts if review and row['review_id']==review.get('review_id')),None)
            if state['state']=='pending' and receipt and receipt['url']==self.client.url:
                state.update(state=receipt['phase'],result=self._confirmation(receipt)['result'])
            return state
        except (ValueError,KeyError,TypeError,OSError):raise StoreError(503,'Private host claim state is unavailable') from None

    def _save(self,state):
        self._retain(state)
        raw=json.dumps(state,sort_keys=True,ensure_ascii=False,allow_nan=False).encode('utf-8')
        if len(raw)>256*1024:raise StoreError(400,'Host claim review exceeds its bound')
        self._atomic(self.path,raw)

    def status(self):
        with self.client.lock,self.receipt_lock:
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
        with self.client.lock,self.receipt_lock:
            state=self._load()
            if state['state']=='pending':raise StoreError(409,'Replay the uncertain host claim before preparing another')
            self._retain(state)
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

    def review_saved(self):
        """Historical local preview only; never proof of a current host lease."""
        with self.client.lock,self.receipt_lock:
            state=self._load()
            return {'scope':'host','state':state['state'],'review':state['review'],'result':state['result'],'acknowledgment_is_current_lease':False}

    def submit(self,review_id):
        with self.client.lock,self.receipt_lock:
            receipt=next((row for row in self._receipts() if row['review_id']==review_id),None)
            if receipt and receipt['phase']=='acknowledged':return self._confirmation(receipt)
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
            previously_attempted=state.get('transport_attempted') is not False
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

    def confirm(self,review_id):
        """Typed confirmation for browser recovery; never infer a current lease."""
        with self.client.lock,self.receipt_lock:
            row=next((row for row in self._receipts() if row['review_id']==review_id),None)
            if row:return self._confirmation(row)
            state=self._load();review=state['review']
            if review and review['review_id']==review_id and state['state'] in ('acknowledged','rejected'):
                self._retain(state)
                return self._confirmation(next(row for row in self._receipts() if row['review_id']==review_id))
            try:return self.submit(review_id)
            except (ApiError,StoreError) as error:
                state=self._load();review=state['review']
                matching=bool(review and review['review_id']==review_id)
                # The transport-attempt marker is durable before any host POST.
                # Only a never-sent command may turn a definite preflight failure
                # into a not-applied outcome. Attempted requests retain identity.
                status=getattr(error,'status',None)
                if matching and state['state']=='pending' and state.get('transport_attempted') is False and type(status) is int and 400<=status<500:
                    state['state']='rejected';self._save(state)
                if matching and state['state']=='rejected':
                    self._retain(state)
                    return self._confirmation(next(row for row in self._receipts() if row['review_id']==review_id))
                raise

    def read(self,task_id,cursor=None):
        if not isinstance(task_id,str) or not 0<len(task_id)<=200:raise StoreError(400,'Expected host task identity')
        self._caps()
        query={'project_id':self.client.project_id,'task_id':task_id}
        if cursor is not None:query['cursor']=cursor
        result=self.client._call('/v1/claims?'+urlencode(query))
        if not isinstance(result,dict) or result.get('project_id')!=self.client.project_id or not isinstance(result.get('items'),list):
            raise ApiError(0,'Host claim list was not confirmed')
        return {**result,'scope':'host','local_claims_unchanged':True}
