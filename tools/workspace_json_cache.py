"""Content-addressed private JSON documents with an unambiguous typed state tree."""
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
try:
    from . import workspace_bundle_chunks as codec
except ImportError:
    import workspace_bundle_chunks as codec

class PrivateJsonCache:
    def __init__(self,directory,binding,*,threshold=1024*1024):
        self.directory=Path(directory);self.binding=binding;self.threshold=threshold
        self.directory.mkdir(parents=True,exist_ok=True)

    def _put(self,value,raw):
        digest=hashlib.sha256(raw).hexdigest();path=self.directory/(digest+'.json')
        name=None
        try:
            # Atomic replacement also repairs a damaged cache blob from exact source bytes.
            with tempfile.NamedTemporaryFile(dir=self.directory,delete=False) as output:
                name=output.name;output.write(raw);output.flush();os.fsync(output.fileno())
            os.replace(name,path)
        finally:
            if name and os.path.exists(name):os.unlink(name)
        return {'sha256':digest,'byte_length':len(raw)}

    def freeze(self,state):
        def walk(value,depth=0):
            if depth>64:raise ValueError('Private state nesting exceeds bound')
            if isinstance(value,dict):
                if value.get('format')=='workbench-exchange' or isinstance(value.get('changes'),list):
                    raw=codec.canonical(value)
                    if len(raw)>=self.threshold:return [3,self._put(value,raw)]
                return [1,{key:walk(child,depth+1) for key,child in value.items()}]
            if isinstance(value,list):return [2,[walk(child,depth+1) for child in value]]
            return [0,value]
        return {'format':'workbench-private-json-state','version':1,'binding':self.binding,'tree':walk(state)}

    def thaw(self,envelope):
        if (not isinstance(envelope,dict) or set(envelope)!={'format','version','binding','tree'} or envelope['format']!='workbench-private-json-state' or type(envelope['version']) is not int or envelope['version']!=1 or envelope['binding']!=self.binding):
            raise ValueError('Private state binding or format mismatch')
        expanded=0
        def walk(node,depth=0):
            nonlocal expanded
            if depth>64 or not isinstance(node,list) or len(node)!=2 or type(node[0]) is not int:
                raise ValueError('Invalid private state tree')
            tag,value=node
            if tag==0:
                if isinstance(value,(dict,list)):raise ValueError('Invalid scalar state')
                return value
            if tag==1 and isinstance(value,dict):return {key:walk(child,depth+1) for key,child in value.items()}
            if tag==2 and isinstance(value,list):return [walk(child,depth+1) for child in value]
            if tag!=3 or not isinstance(value,dict) or set(value)!={'sha256','byte_length'}:
                raise ValueError('Invalid private state reference')
            digest,size=value['sha256'],value['byte_length']
            if not isinstance(digest,str) or not re.fullmatch('[a-f0-9]{64}',digest) or type(size) is not int or not 2<=size<=codec.DEFAULT_MAX_BYTES:
                raise ValueError('Invalid private state digest or length')
            expanded+=size
            if expanded>1024*1024*1024:raise ValueError('Expanded private state exceeds bound')
            path=self.directory/(digest+'.json')
            if path.is_symlink() or path.stat().st_size!=size:raise ValueError('Private state blob size mismatch')
            with path.open('rb') as source:raw=source.read(size+1)
            if len(raw)!=size or hashlib.sha256(raw).hexdigest()!=digest:raise ValueError('Private state blob integrity mismatch')
            value=json.loads(raw.decode('utf-8'))
            if codec.canonical(value)!=raw:raise ValueError('Private state blob is not canonical')
            # A cached document is opaque; authored data can never become cache instructions.
            return value
        return walk(envelope['tree'])

    def prune(self,envelope):
        """Called only after its state manifest has been atomically published."""
        keep=set()
        def walk(node):
            tag,value=node
            if tag==3:keep.add(value['sha256'])
            elif tag==1:
                for child in value.values():walk(child)
            elif tag==2:
                for child in value:walk(child)
        walk(envelope['tree'])
        for path in self.directory.glob('*.json'):
            if re.fullmatch('[a-f0-9]{64}',path.stem) and path.stem not in keep:
                try:path.unlink()
                except OSError:pass  # A failed cleanup never invalidates the published request.
