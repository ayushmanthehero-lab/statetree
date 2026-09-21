"""Project-scoped file tools plus explicitly approved native execution.

Native programs have the user's OS permissions. SafeFiles is not a code sandbox.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from statetree.agent.files import SafeFiles, safe_name, digest, project_lock, ProjectBusy
from .store import encode


class TaskPaused(RuntimeError):
    pass


class UncertainOperation(RuntimeError):
    pass


# Compact schemas matter on the user's 4096-token model context.
def spec(name, description, properties, required=()):
    return {'type':'function','function':{'name':name,'description':description,'parameters':{
        'type':'object','properties':properties,'required':list(required),'additionalProperties':False}}}
S={'type':'string'}
I={'type':'integer'}
TOOL_SPECS=[
 spec('list_files','List safe project paths, 80 per page.',{'offset':I}),
 spec('read_file','Read UTF-8 text with SHA256; page via next_offset.',{'path':S,'offset':I,'limit':I},['path']),
 spec('search_files','Literal text search, up to 20 matching lines.',{'text':S},['text']),
 spec('write_file','Create file (hash missing) or replace using the hash from read_file. Use small files/patches.',
      {'path':S,'content':S,'expected_sha256':S},['path','content']),
 spec('edit_file','Replace one exact nonempty occurrence. Read first. Use small patches.',{'path':S,'old':S,'new':S},['path','old','new']),
 spec('run_command','Run argv in project, not a shell string. python maps to this venv. Requires task execution approval.',
      {'argv':{'type':'array','items':S},'timeout':I},['argv']),
 spec('update_plan','Save compact plan/decisions and the next action. Progress is otherwise saved automatically.',{'summary':S,'next_step':S},['summary','next_step']),
 spec('read_evidence','Read a previous operation by ID, including its arguments and receipt. Page via next_offset.',
      {'operation_id':S,'offset':I,'limit':I},['operation_id']),
]
DEFINITIONS={s['function']['name']:s['function']['parameters'] for s in TOOL_SPECS}


def page_int(args,key,default,lo,hi):
    x=args.get(key,default)
    if type(x) is not int or not lo<=x<=hi: raise ValueError(f'{key} must be {lo}-{hi}')
    return x


def clip(text,n=1800):
    return str(text)[:n]


class Toolbox:
    def __init__(self,store,task_id):
        self.store,self.task_id=store,task_id
        self.files=SafeFiles(store.repo)

    def _validate(self,name,args):
        if name not in DEFINITIONS or type(args) is not dict: raise ValueError('Unknown tool or invalid arguments')
        schema=DEFINITIONS[name]
        if set(args)-set(schema['properties']) or set(schema['required'])-set(args):
            raise ValueError('Missing or unsupported tool arguments')
        for k,v in args.items():
            kind=schema['properties'][k]['type']
            expected={'string':str,'integer':int,'array':list}[kind]
            if type(v) is not expected: raise ValueError(f'{k} must be {kind}')
        if 'path' in args: safe_name(args['path'])
        if len(encode(args).encode())>65536: raise ValueError('Tool arguments exceed 64 KiB')

    def _intent(self,name,args):
        self._validate(name,args)
        if name in ('write_file','edit_file'):
            path=args['path']
            raw=self.files.maybe_read(path)
            before=digest(raw) if raw is not None else None
            if name=='write_file':
                expected=args.get('expected_sha256','missing')
                if expected!='missing' and not re.fullmatch('[0-9a-f]{64}',expected): raise ValueError('Invalid expected SHA256')
                if before!=(None if expected=='missing' else expected): raise ValueError('File changed/already exists; read it and supply its current SHA256')
                content=args['content']
            else:
                if raw is None: raise FileNotFoundError(path)
                text=raw.decode('utf-8')
                if not args['old'] or text.count(args['old'])!=1: raise ValueError('Old text must match exactly once; read the file again')
                content=text.replace(args['old'],args['new'],1)
            if len(content.encode())>1024*1024: raise ValueError('File exceeds 1 MiB')
            return {'path':path,'before':before,'after':digest(content.encode()),'content':content}
        if name=='run_command':
            if not self.store.task(self.task_id)['allow_execution']:
                raise ValueError('Native execution approval is required. Start a new task with the trust checkbox selected.')
            argv=args['argv']
            if not argv or len(argv)>100 or any(type(x) is not str or '\x00' in x or len(x)>12000 for x in argv):
                raise ValueError('argv must be a nonempty list of bounded strings')
            argv=list(argv)
            if argv[0] in ('python','python3'): argv[0]=sys.executable
            if not argv[0]: raise ValueError('Missing executable')
            timeout=page_int(args,'timeout',60,1,600)
            return {'argv':argv,'timeout':timeout}
        if name=='update_plan':
            if len(args['summary'])>1600 or len(args['next_step'])>500:
                raise ValueError('Use a compact plan (summary <=1600, next_step <=500 characters)')
        return {}

    def execute(self,op_id,name,args):
        old=self.store.operation(op_id)
        if old:
            op=self.store.prepare(self.task_id,op_id,name,args)
        else:
            try:
                intent=self._intent(name,args)
            except (ValueError,OSError,UnicodeError) as e:
                self.store.prepare(self.task_id,op_id,name,args)
                return self.store.finish(op_id,{'status':'error','text':clip(str(e))})
            op=self.store.prepare(self.task_id,op_id,name,args,intent)
        if op['receipt'] is not None: return op['receipt']
        if op.get('resolution'):
            return self.store.finish(op_id,{'status':'operator_resolved','text':op['resolution']['note'],
                                           'operator_reconciled':True,'source':'operator; execution was not independently observed'})
        if name=='run_command':
            return self._command(op)
        if name in ('write_file','edit_file'):
            intent=op['intent']
            current=self.files.maybe_read(intent['path'])
            actual=digest(current) if current is not None else None
            if actual==intent['after']:
                return self.store.finish(op_id,{'status':'ok','path':intent['path'],'sha256':actual,'reconciled':True,'text':'Confirmed saved content by SHA256.'})
            if actual!=intent['before']:
                raise UncertainOperation('File differs from both before/after hashes: '+intent['path']+'. Review it before continuing.')
            # A partial write on hard process death is detected by these hashes on restart.
            self.files.write(intent['path'],intent['content'].encode(),intent['before'])
            receipt={'status':'ok','path':intent['path'],'sha256':intent['after'],'text':'Saved file.'}
        else:
            try: receipt=self._read(name,args)
            except (ValueError,OSError,UnicodeError) as e: receipt={'status':'error','text':clip(str(e))}
        return self.store.finish(op_id,receipt)

    def _read(self,name,args):
        if name=='list_files':
            names=self.files.names(); offset=page_int(args,'offset',0,0,1000000)
            return {'status':'ok','files':names[offset:offset+80],'next_offset':offset+80 if len(names)>offset+80 else None}
        if name=='read_file':
            raw=self.files.read(args['path']); text=raw.decode('utf-8')
            off=page_int(args,'offset',0,0,10000000); limit=page_int(args,'limit',1600,1,3000)
            return {'status':'ok','path':args['path'],'sha256':digest(raw),'text':text[off:off+limit],
                    'next_offset':off+limit if len(text)>off+limit else None}
        if name=='search_files':
            text=args['text']
            if not text or len(text)>200: raise ValueError('Search text must have 1-200 characters')
            found=[]
            for path in self.files.names():
                try: lines=self.files.read(path).decode('utf-8').splitlines()
                except (ValueError,UnicodeError): continue
                for i,line in enumerate(lines):
                    if text in line: found.append({'path':path,'line':i+1,'text':line[:240]})
                    if len(found)>=20: return {'status':'ok','matches':found,'truncated':True}
            return {'status':'ok','matches':found}
        if name=='update_plan':
            return {'status':'ok','summary':args['summary'],'next_step':args['next_step'],'source':'model-authored plan; not verification'}
        if name=='read_evidence':
            if args['operation_id']=='index':
                text=encode([{'id':r['id'],'tool':r['tool'],'status':(r['receipt'] or {}).get('status'),
                              'path':r['arguments'].get('path')} for r in self.store.operations(self.task_id)])
                off=page_int(args,'offset',0,0,10000000); n=page_int(args,'limit',1600,1,3000)
                return {'status':'ok','text':text[off:off+n],'next_offset':off+n if len(text)>off+n else None}
            op=self.store.operation(args['operation_id'])
            if op is None or op['task_id']!=self.task_id: raise ValueError('Unknown evidence for this task')
            text=encode(op); off=page_int(args,'offset',0,0,10000000); n=page_int(args,'limit',1600,1,3000)
            return {'status':'ok','text':text[off:off+n],'next_offset':off+n if len(text)>off+n else None}
        raise ValueError('Unknown tool')

    def _command(self,op):
        # A separate worker writes its receipt independently of the web request.
        # Re-launching the *worker* is safe; a dispatched command itself is never retried.
        cmd=[sys.executable,'-m','statetree.agentic.command_worker',str(self.store.repo),op['id']]
        options={'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {'start_new_session':True}
        env=dict(os.environ)
        package_root=str(Path(__file__).resolve().parents[2])
        env['PYTHONPATH']=package_root+os.pathsep+env.get('PYTHONPATH','')
        proc=subprocess.Popen(cmd,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,env=env,**options)
        deadline=time.monotonic()+op['intent'].get('timeout',60)+30
        try:
            while time.monotonic()<deadline:
                current=self.store.operation(op['id'])
                if current['receipt'] is not None: return current['receipt']
                if proc.poll() is not None:
                    if not current['dispatched'] and self.store.task(self.task_id)['pause_requested']:
                        raise TaskPaused('Paused before command dispatch; it can safely resume.')
                    # Another copy of the receipt worker can still own this operation.
                    try:
                        with project_lock(self.store.root / (op['id'].replace(':','_')+'.lock')):
                            raise UncertainOperation('Command has no durable receipt. It was not repeated. Inspect the workspace and reconcile operation '+op['id'])
                    except ProjectBusy:
                        pass
                time.sleep(.1)
            raise UncertainOperation('Command worker has not confirmed completion; no automatic replay: '+op['id'])
        finally:
            if proc.poll() is not None: proc.wait()
