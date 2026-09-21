"""One receipt-writing command worker. Never replays an unreceipted dispatch."""
import os
import signal
import subprocess
import sys
import threading
import time
from statetree.agent.files import project_lock, ProjectBusy
from .store import TaskStore


def terminate_tree(process):
    if os.name=='nt':
        stopped=subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],capture_output=True,timeout=15)
        if process.poll() is None and stopped.returncode: return False
    else:
        try: os.killpg(process.pid,signal.SIGKILL)
        except ProcessLookupError: pass
    try: process.wait(timeout=10)
    except subprocess.TimeoutExpired: return False
    return True


def command_environment():
    # This is minimization, not isolation: executed code can still access the user's files/network.
    allowed={'PATH','PATHEXT','SYSTEMROOT','WINDIR','COMSPEC','TEMP','TMP','TMPDIR','HOME','USERPROFILE',
             'LOCALAPPDATA','APPDATA','PROGRAMFILES','PROGRAMFILES(X86)','NUMBER_OF_PROCESSORS',
             'LANG','LC_ALL','VIRTUAL_ENV'}
    env={k:v for k,v in os.environ.items() if k.upper() in allowed}
    env.update(PYTHONUNBUFFERED='1',PYTHONIOENCODING='utf-8',PYTHONDONTWRITEBYTECODE='1')
    return env


def run(repo,op_id):
    s=TaskStore(repo)
    with project_lock(s.root/(op_id.replace(':','_')+'.lock')):
        op=s.operation(op_id)
        if not op or op['tool']!='run_command': raise ValueError('Not a command operation')
        if op['receipt'] is not None: return
        if op['dispatched']: return  # Uncertain prior execution: the operator must reconcile it.
        task=s.task(op['task_id'])
        if not task['allow_execution']: raise ValueError('Native execution not approved')
        if task['pause_requested']:
            return  # Still undispatched. Resume can safely dispatch this same intent.
        if os.name=='nt':
            from .windows_job import attach_worker_to_job
            try: attach_worker_to_job()
            except OSError as error:
                s.finish(op_id,{'status':'error','not_started':True,'exit_code':None,'text':str(error)[:1500]})
                return
        s.mark_dispatched(op_id)
        intent=op['intent']
        args=intent['argv']
        options={'creationflags':subprocess.CREATE_NEW_PROCESS_GROUP|subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {'start_new_session':True}
        try:
            p=subprocess.Popen(args,cwd=s.repo,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT,env=command_environment(),shell=False,**options)
        except OSError as error:
            s.finish(op_id,{'status':'error','exit_code':None,'not_started':True,'text':str(error)[:1500]})
            return
        chunks=[]; count=[0]
        def read():
            try:
                while True:
                    chunk=p.stdout.read(4096)
                    if not chunk: break
                    remaining=max(0,32768-count[0])
                    if remaining: chunks.append(chunk[:remaining])
                    count[0]+=len(chunk)
            except (OSError,ValueError): pass
        reader=threading.Thread(target=read,daemon=True); reader.start()
        deadline=time.monotonic()+intent['timeout']; reason=None
        try:
            while p.poll() is None:
                if s.task(op['task_id'])['pause_requested']: reason='paused'; break
                if time.monotonic()>=deadline: reason='timeout'; break
                time.sleep(.1)
            if reason and not terminate_tree(p): return  # No receipt without confirmed termination.
            p.wait(timeout=5); reader.join(timeout=3)
            # Do not allow background descendants to outlive a supposedly finished command.
            if os.name!='nt':
                try: os.killpg(p.pid,signal.SIGKILL)
                except ProcessLookupError: pass
            text=b''.join(chunks).decode('utf-8',errors='replace')
            if count[0]>32768: text+='\n[Output clipped to 32 KiB; total bytes '+str(count[0])+']'
            receipt={'status':'interrupted' if reason else 'ok' if p.returncode==0 else 'error',
                     'exit_code':p.returncode,'text':text,'output_bytes':count[0],
                     'partial_effects_possible':bool(reason),'stop_reason':reason}
            s.finish(op_id,receipt)
        finally:
            if p.poll() is None: terminate_tree(p)
            if p.stdout: p.stdout.close()


if __name__=='__main__':
    try: run(sys.argv[1],sys.argv[2])
    except ProjectBusy: pass
