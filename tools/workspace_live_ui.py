"""Owner-bound compact views over private durable live review commands."""
import hashlib
import json
import os
import re
import tempfile
import uuid
from datetime import datetime, timezone
try:
    from . import roadmap_research as research, roadmap_exchange as exchange
    from .roadmap_store import StoreError
    from .roadmap_cli import ApiError
    from .workspace_live_client import LiveConflict
    from .workspace_json_cache import PrivateJsonCache
except ImportError:
    import roadmap_research as research, roadmap_exchange as exchange
    from roadmap_store import StoreError
    from roadmap_cli import ApiError
    from workspace_live_client import LiveConflict
    from workspace_json_cache import PrivateJsonCache

PAGE=20
KINDS=('join','host','local')
PHASES=('reviewing','prepared','pending','acknowledged','rejected')

def _error(code,message):raise StoreError(code,message)
def _digest_text(value):return hashlib.sha256(value.encode('utf-8')).hexdigest()
def _offset(value,total,*,paged=True):
    if type(value) is not int or value<0 or value>total or (paged and value%PAGE):_error(400,'Invalid live review offset')
    return value

class WorkspaceLiveUI:
    def __init__(self,service,project,caller):
        self.service=service;self.project=project;self.caller=caller
        service._project(project)
        with service.store._connection() as db:self.local_store=db.execute('SELECT store_id FROM store_meta').fetchone()[0]
        self.path=service._path(project).parent/'ui-review.json'
        self.receipts_path=self.path.parent/'ui-outcomes.json'
        self.documents=PrivateJsonCache(self.path.parent/'ui-documents',research.digest([self.local_store,project]))

    def _outcomes(self):
        if not self.receipts_path.exists():return []
        try:
            if self.receipts_path.is_symlink() or self.receipts_path.stat().st_size>2*1024*1024:raise ValueError()
            data=json.loads(self.receipts_path.read_text(encoding='utf-8'))
            if set(data)!={'version','project_id','local_store','items'} or data['version']!=1 or data['project_id']!=self.project or data['local_store']!=self.local_store or not isinstance(data['items'],list) or len(data['items'])>256:raise ValueError()
            seen=set()
            for row in data['items']:
                if set(row)!={'caller','review_id','command_id','kind','phase','binding','result','error','recorded_at'} or row['kind'] not in KINDS or row['phase'] not in ('acknowledged','rejected'):raise ValueError()
                if any(not isinstance(row[key],str) or not 0<len(row[key])<=200 for key in ('caller','review_id')) or not isinstance(row['command_id'],str) or not re.fullmatch('[0-9a-f]{64}',row['command_id']):raise ValueError()
                identity=(row['caller'],row['review_id'],row['command_id'])
                if identity in seen:raise ValueError()
                seen.add(identity)
                binding=row['binding']
                if not isinstance(binding,dict) or set(binding)!={'url','remote_actor','remote_store','actor_id','local_store'} or binding['local_store']!=self.local_store or any(not isinstance(v,str) or not 0<len(v)<=4000 for v in binding.values()):raise ValueError()
                if row['result'] is not None and not isinstance(row['result'],dict) or row['error'] is not None and not isinstance(row['error'],str) or not isinstance(row['recorded_at'],str):raise ValueError()
                if row['phase']=='acknowledged' and (row['result'] or {}).get('reviewed_command')!={'id':row['command_id'],'kind':row['kind'],'state':'acknowledged'}:raise ValueError()
            return data['items']
        except (ValueError,KeyError,TypeError,OSError):_error(503,'Private live outcome receipts are unavailable; no outcome was discarded')

    def _find_outcome(self,review_id,command_id):
        row=next((r for r in self._outcomes() if r['review_id']==review_id and r['command_id']==command_id),None)
        if row is not None and row['caller']!=self.caller:_error(403,'This recorded live outcome belongs to another local owner')
        return row

    def _retain_outcome(self,state):
        if state['phase'] not in ('acknowledged','rejected'):return
        rows=self._outcomes();prior=next((r for r in rows if r['review_id']==state['review_id'] and r['command_id']==state['command_id']),None)
        if prior:
            if prior['caller']!=state['caller'] or prior['binding']!=state['binding'] or prior['phase']!=state['phase']:_error(409,'Recorded outcome conflicts with the current command; retain state for review')
            return
        result=None
        if state['phase']=='acknowledged':
            result={key:value for key,value in (state['result'] or {}).items() if key in ('state','pending','phase','base_id','operation_id','claim_scope')}
            result['reviewed_command']={'id':state['command_id'],'kind':state['kind'],'state':'acknowledged'}
        row={key:state[key] for key in ('caller','review_id','command_id','kind','phase','binding','error')}
        row.update(result=result,recorded_at=datetime.now(timezone.utc).isoformat())
        data={'version':1,'project_id':self.project,'local_store':self.local_store,'items':(rows+[row])[-256:]}
        raw=research.canonical(data).encode('utf-8')
        if len(raw)>2*1024*1024:_error(413,'Live outcome ledger exceeds its bound')
        self.receipts_path.parent.mkdir(parents=True,exist_ok=True);name=None
        try:
            with tempfile.NamedTemporaryFile(dir=self.receipts_path.parent,delete=False) as out:name=out.name;out.write(raw);out.flush();os.fsync(out.fileno())
            os.replace(name,self.receipts_path)
        finally:
            if name and os.path.exists(name):os.unlink(name)

    def _historical_page(self,row):
        return {'project_id':self.project,'review_id':row['review_id'],'command_id':row['command_id'],'kind':row['kind'],'phase':row['phase'],
                'draft_revision':0,'selected_count':0,'total_conflicts':0,'summary':{},'offset':0,'next_offset':None,'items':[],
                'result':row['result'],'error':row['error'],'host_url':row['binding']['url'],'remote_store':row['binding']['remote_store'],
                'historical_only':True,'current_access_checked':False,'recorded_at':row['recorded_at']}

    def _load(self):
        outcomes=self._outcomes()
        if not self.path.exists():return None
        try:
            if self.path.is_symlink() or self.path.stat().st_size>32*1024*1024:raise ValueError()
            state=self.documents.thaw(json.loads(self.path.read_text(encoding='utf-8')))
            expected={'version','project_id','caller','review_id','kind','binding','review','choices','draft_revision','phase','command_id','prepared_from_revision','choice_receipts','result','error'}
            if (not isinstance(state,dict) or set(state)!=expected or type(state['version']) is not int or state['version']!=1 or state['project_id']!=self.project
                    or not isinstance(state['caller'],str) or not 0<len(state['caller'])<=200
                    or not isinstance(state['review_id'],str) or not 0<len(state['review_id'])<=200
                    or state['kind'] not in KINDS or state['phase'] not in PHASES
                    or type(state['draft_revision']) is not int or state['draft_revision']<0
                    or not isinstance(state['review'],dict) or not isinstance(state['choices'],dict)
                    or any(not isinstance(key,str) or not re.fullmatch('[0-9a-f]{64}',key) or value not in ('local','remote') for key,value in state['choices'].items())
                    or not isinstance(state['choice_receipts'],list) or len(state['choice_receipts'])>32):raise ValueError()
            preview=state['review'].get('preview')
            if not isinstance(preview,dict) or not isinstance(preview.get('changes'),list):raise ValueError()
            for change in preview['changes']:
                if not isinstance(change,dict) or not isinstance(change.get('fields'),dict) or any(not isinstance(change.get(key),str) for key in ('table','key','status')):raise ValueError()
                if any(not isinstance(field,str) or not isinstance(value,dict) or type(value.get('conflict')) is not bool for field,value in change['fields'].items()):raise ValueError()
            if state['result'] is not None and not isinstance(state['result'],dict) or state['error'] is not None and not isinstance(state['error'],str):raise ValueError()
            if state['phase']=='reviewing':
                if state['command_id'] is not None or state['prepared_from_revision'] is not None:raise ValueError()
            elif type(state['prepared_from_revision']) is not int or state['prepared_from_revision']<0 or state['command_id'] is None:raise ValueError()
            if state['phase']=='acknowledged' and (state['result'] or {}).get('reviewed_command')!={'id':state['command_id'],'kind':state['kind'],'state':'acknowledged'}:raise ValueError()
            binding=state['binding']
            if not isinstance(binding,dict) or set(binding)!={'url','remote_actor','remote_store','actor_id','local_store'} or binding['local_store']!=self.local_store:raise ValueError()
            if any(not isinstance(value,str) or not 0<len(value)<=4000 for value in binding.values()):raise ValueError()
            if state['command_id'] is not None and (not isinstance(state['command_id'],str) or not re.fullmatch('[0-9a-f]{64}',state['command_id'])):raise ValueError()
            seen=set()
            for receipt in state['choice_receipts']:
                if (not isinstance(receipt,dict) or set(receipt)!={'request_id','digest'} or not isinstance(receipt['request_id'],str)
                        or not 0<len(receipt['request_id'])<=200 or receipt['request_id'] in seen or not isinstance(receipt['digest'],str) or not re.fullmatch('[0-9a-f]{64}',receipt['digest'])):raise ValueError()
                seen.add(receipt['request_id'])
            terminal=next((r for r in outcomes if r['review_id']==state['review_id'] and r['command_id']==state['command_id'] and r['caller']==state['caller']),None)
            if state['phase']=='pending' and terminal and terminal['binding']==state['binding']:
                state.update(phase=terminal['phase'],result=terminal['result'],error=terminal['error'])
            return state
        except (ValueError,KeyError,TypeError,OSError):_error(503,'Private live review state is unavailable; no review was discarded')

    def _owned(self,state,review_id=None):
        if state is None:_error(409,'Load a live review first')
        if state['caller']!=self.caller:_error(403,'This live review belongs to another local owner')
        if review_id is not None and state['review_id']!=review_id:_error(409,'Live review handle changed; reread explicitly')
        return state

    def _save(self,state):
        self._retain_outcome(state)
        frozen=self.documents.freeze(state)
        try:self.documents.thaw(frozen)
        except ValueError:_error(413,'Live review exceeds the private cache bound')
        raw=research.canonical(frozen).encode('utf-8')
        if len(raw)>32*1024*1024:_error(413,'Live review manifest exceeds its bound')
        self.path.parent.mkdir(parents=True,exist_ok=True);name=None
        try:
            with tempfile.NamedTemporaryFile(dir=self.path.parent,delete=False) as out:name=out.name;out.write(raw);out.flush();os.fsync(out.fileno())
            os.replace(name,self.path);self.documents.prune(frozen)
        finally:
            if name and os.path.exists(name):os.unlink(name)

    def _client(self,state=None,*,enabled=True):
        config=self.service._load(self.project)
        if enabled and not config['enabled']:_error(409,'Resume the existing live connection before preparing or confirming a command')
        if not config.get('url'):_error(409,'Configure a live connection first')
        if state and (config['url']!=state['binding']['url'] or config.get('actor_id')!=state['binding']['actor_id']):_error(409,'Live host or local actor changed; retain or explicitly discard the old review')
        self.service._bound(self.project)
        return self.service._client(self.project,config)

    def _binding(self,client,kind,review,choices):
        command=client._review_command(kind,review,choices)
        return command,{'url':client.url,'actor_id':client.actor_id,'local_store':self.local_store,'remote_actor':command['remote_actor'],'remote_store':command['remote_store']}

    @staticmethod
    def _rows(state):
        rows=[];seen=set()
        for change in state['review']['preview']['changes']:
            for field,alternative in change['fields'].items():
                if not alternative['conflict']:continue
                token=research.canonical([change['table'],change['key'],field]);key=_digest_text(token)
                if key in seen:_error(409,'Duplicate live conflict identity')
                seen.add(key);rows.append({'key':key,'token':token,'table':change['table'],'field':field,'entity_key':change['key'],'alternative':alternative,'remote_allowed':change['table'] not in exchange.IMMUTABLE})
        return rows

    @staticmethod
    def _side(alternative,side):
        present=alternative.get(side+'_present',alternative.get(side) is not None)
        text=research.canonical(alternative.get(side)) if present else ''
        return {'present':present,'preview':text[:2048],'length':len(text),'digest':_digest_text(text),'truncated':len(text)>2048},text

    def _page(self,state,offset=0):
        rows=self._rows(state);_offset(offset,len(rows));summary={}
        for change in state['review']['preview']['changes']:
            counts=summary.setdefault(change['table'],{});counts[change['status']]=counts.get(change['status'],0)+1
        items=[]
        for row in rows[offset:offset+PAGE]:
            items.append({key:row[key] for key in ('key','table','field','entity_key','remote_allowed')})
            items[-1]['label_truncated']={key:len(row[key])>512 for key in ('field','entity_key')}
            for key in ('field','entity_key'):items[-1][key]=row[key][:512]
            for side in ('local','remote','base'):items[-1][side]=self._side(row['alternative'],side)[0]
            items[-1]['choice']=state['choices'].get(row['key'],'')
        return {'project_id':self.project,'review_id':state['review_id'],'kind':state['kind'],'phase':state['phase'],'draft_revision':state['draft_revision'],
                'command_id':state['command_id'],'selected_count':len(state['choices']),'total_conflicts':len(rows),'summary':summary,'offset':offset,
                'next_offset':offset+PAGE if offset+PAGE<len(rows) else None,'items':items,'result':state['result'],'error':state['error'],
                'host_url':state['binding']['url'],'remote_store':state['binding']['remote_store']}

    def read(self,offset=0):
        with self.service._locked(self.project):
            state=self._load()
            if state is None:
                _offset(offset,0);return {'project_id':self.project,'phase':'idle','items':[]}
            return self._page(self._owned(state),offset)

    def preview(self,kind):
        if kind not in KINDS:_error(400,'Live review kind must be join, host or local')
        with self.service._locked(self.project):
            previous=self._load()
            if previous:
                self._owned(previous)
                if previous['phase'] in ('prepared','pending') or previous['phase']=='reviewing' and previous['choices']:_error(409,'Confirm or explicitly discard the existing live review first')
            client=self._client()
            review=self.service.preview_join(self.project) if kind=='join' else self.service.review(self.project,kind)
            command,binding=self._binding(client,kind,review,{})
            state={'version':1,'project_id':self.project,'caller':self.caller,'review_id':str(uuid.uuid4()),'kind':kind,'binding':binding,'review':review,
                   'choices':{},'draft_revision':0,'phase':'reviewing','command_id':None,'prepared_from_revision':None,'choice_receipts':[],'result':None,'error':None}
            self._rows(state);self._save(state);return self._page(state)

    def value(self,review_id,key,side,offset=0):
        if side not in ('local','remote','base','field','entity') or not isinstance(key,str):_error(400,'Invalid live value selector')
        with self.service._locked(self.project):
            state=self._owned(self._load(),review_id);row=next((r for r in self._rows(state) if r['key']==key),None)
            if row is None:_error(404,'Conflict field not found in this review')
            if side in ('field','entity'):
                text=research.canonical(row['field' if side=='field' else 'entity_key']);info={'digest':_digest_text(text)}
            else:info,text=self._side(row['alternative'],side)
            _offset(offset,len(text),paged=False)
            return {'project_id':self.project,'review_id':review_id,'key':key,'side':side,'offset':offset,'text':text[offset:offset+8192],
                    'next_offset':offset+8192 if offset+8192<len(text) else None,'length':len(text),'digest':info['digest']}

    def choose(self,review_id,offset,expected_draft_revision,request_id,choices):
        if not isinstance(request_id,str) or not 0<len(request_id)<=200:_error(400,'Page save requires a request ID')
        with self.service._locked(self.project):
            state=self._owned(self._load(),review_id);rows=self._rows(state);_offset(offset,len(rows));page=rows[offset:offset+PAGE]
            if not isinstance(choices,dict) or set(choices)!={r['key'] for r in page}:_error(400,'Choose each page field explicitly')
            for row in page:
                mode=choices[row['key']]
                if mode not in ('','local','remote') or mode=='remote' and not row['remote_allowed']:_error(400,'Unsupported field choice; immutable history cannot be replaced')
            digest=research.digest({'review_id':review_id,'offset':offset,'expected_draft_revision':expected_draft_revision,'choices':choices})
            prior=next((r for r in state['choice_receipts'] if r['request_id']==request_id),None)
            if prior:
                if prior['digest']!=digest:_error(409,'Page request ID already names different choices')
                return {**self._page(state,offset),'choice_ack_request_id':request_id}
            if state['phase']!='reviewing' or type(expected_draft_revision) is not int or expected_draft_revision!=state['draft_revision']:_error(409,'Live choices changed; reread and reconcile explicitly')
            for key,mode in choices.items():
                if mode:state['choices'][key]=mode
                else:state['choices'].pop(key,None)
            state['draft_revision']+=1;state['choice_receipts']=(state['choice_receipts']+[{'request_id':request_id,'digest':digest}])[-32:]
            self._save(state);return {**self._page(state,offset),'choice_ack_request_id':request_id}

    def _choices(self,state):
        rows=self._rows(state)
        if set(state['choices'])!={r['key'] for r in rows}:_error(400,'Save an explicit choice for every conflicting field')
        for row in rows:
            if state['choices'][row['key']]=='remote' and not row['remote_allowed']:_error(400,'Immutable source history cannot be replaced')
        return {r['token']:state['choices'][r['key']] for r in rows}

    def prepare(self,review_id,expected_draft_revision,acknowledged):
        if acknowledged is not True or type(expected_draft_revision) is not int:_error(400,'Explicit reviewed-command acknowledgment required')
        with self.service._locked(self.project):
            state=self._owned(self._load(),review_id)
            if state['phase']!='reviewing':
                if state['prepared_from_revision']==expected_draft_revision:return self._page(state)
                _error(409,'Live command already prepared against another review')
            if expected_draft_revision!=state['draft_revision']:_error(409,'Live choices changed; reread before preparing')
            choices=self._choices(state);client=self._client(state)
            fresh=self.service.preview_join(self.project) if state['kind']=='join' else self.service.review(self.project,state['kind'])
            if fresh!=state['review']:_error(409,'Live data changed since review; discard and load a fresh explicit review')
            command,binding=self._binding(client,state['kind'],state['review'],choices)
            if binding!=state['binding']:_error(409,'Authenticated host identity changed since review')
            state.update(phase='prepared',command_id=command['id'],prepared_from_revision=expected_draft_revision,error=None)
            self._save(state);return self._page(state)

    @staticmethod
    def _known_rejected(raw,state):
        job=raw.get('outbox') or {};command=job.get('reviewed_command') or {}
        if command.get('id')!=state['command_id']:return False
        return (state['kind']=='host' and job.get('phase')=='post' and raw.get('last_http_status')==409
                or state['kind'] in ('join','local') and job.get('phase')=='apply-local' and raw.get('last_local_conflict') is True)

    def submit(self,review_id,command_id):
        with self.service._locked(self.project):
            historical=self._find_outcome(review_id,command_id)
            if historical:return self._historical_page(historical)
            state=self._owned(self._load(),review_id)
            if not isinstance(command_id,str) or state['command_id']!=command_id or state['phase'] not in ('prepared','pending','acknowledged','rejected'):_error(409,'Use the exact prepared command handle')
            if state['phase']=='rejected':return self._page(state)
            client=self._client(state);choices=self._choices(state);command,binding=self._binding(client,state['kind'],state['review'],choices)
            if binding!=state['binding'] or command['id']!=command_id:_error(409,'Prepared command belongs to another host, principal or review')
            if state['phase']=='acknowledged':
                state['result']={**client.status(),'reviewed_command':{'id':command_id,'kind':state['kind'],'state':'acknowledged'}}
                return self._page(state)
            was_pending=state['phase']=='pending';state.update(phase='pending',error=None);self._save(state)
            try:
                result=self.service.join(self.project,state['review'],choices) if state['kind']=='join' else self.service.resolve(self.project,state['kind'],state['review'],choices)
            except (LiveConflict,StoreError,ApiError) as error:
                raw=client._load();job=raw.get('outbox') or {};tracked=(job.get('reviewed_command') or {}).get('id')==command_id
                receipt=next((r for r in raw.get('review_receipts',[]) if r['id']==command_id),None)
                status=getattr(error,'status',None)
                rejected=self._known_rejected(raw,state) or (not was_pending and not tracked and receipt is None and (isinstance(error,LiveConflict) or status is not None and 400<=status<500))
                state.update(phase='rejected' if rejected else 'pending',error='Command was not applied; load a fresh explicit review.' if rejected else 'Outcome remains unconfirmed. Restore access and retry this exact command.')
                self._save(state);return self._page(state)
            receipt=result.get('reviewed_command') if isinstance(result,dict) else None
            if receipt=={'id':command_id,'kind':state['kind'],'state':'acknowledged'}:state.update(phase='acknowledged',result=result,error=None)
            else:
                raw=client._load();rejected=self._known_rejected(raw,state)
                state.update(phase='rejected' if rejected else 'pending',result=result if isinstance(result,dict) else None,error='A fresh explicit review is required.' if rejected else 'Command remains pending; retry keeps its original identity.')
            self._save(state);return self._page(state)

    def discard(self,review_id):
        with self.service._locked(self.project):
            state=self._owned(self._load(),review_id)
            if state['phase']=='pending':_error(409,'Confirm the uncertain command before discarding its review')
            self.path.unlink();self.documents.prune(self.documents.freeze({'empty':True}))
            return {'project_id':self.project,'phase':'idle','items':[]}

def pending_ui(service,project):
    state=WorkspaceLiveUI(service,project,None)._load()
    return bool(state and state['phase']=='pending')
