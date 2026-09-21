"""Workbench task lifecycle; HTTP requests never own a long-running model loop."""
import threading
from statetree.agent.files import project_lock, ProjectBusy
from .store import TaskStore
from .engine import Engine


class TaskManager:
    def __init__(self,repo,*,transport_factory=None):
        self.repo=repo; self.store=TaskStore(repo); self.transport_factory=transport_factory
        self.guard=threading.RLock(); self.thread=None; self.active_id=None; self.retries=3

    def active(self):
        with self.guard:
            return self.active_id if self.thread and self.thread.is_alive() else None

    def busy(self):
        if self.active(): return True
        try:
            with project_lock(self.store.root/'run.lock'): return False
        except ProjectBusy: return True

    def unresolved(self):
        return self.store.unresolved_command()

    def view(self,task):
        keep=('id','prompt','status','allow_execution','created_at','updated_at','steps','model_calls',
              'checkpoint_id','final','error','note','pause_requested')
        result={k:task[k] for k in keep}
        if task['status']=='running' and self.active()!=task['id'] and not self.busy():
            result.update(status='interrupted',error='Previous coordinator stopped. Resume this task to reconcile its saved work.')
        result['active']=self.active()==task['id']
        return result

    def list(self):
        return {'items':[self.view(t) for t in self.store.tasks()], 'active_task_id':self.active(), 'repo':str(self.repo)}

    def get(self,task_id,after=0):
        task=self.store.task(task_id); events=self.store.events(task_id,after=after)
        ops=self.store.operations(task_id)
        unresolved=[{'id':o['id'],'tool':o['tool'],'arguments':o['arguments'],'receipt':o['receipt']} for o in ops
                    if not o.get('resolution') and (o['receipt'] is None or (o['receipt'] or {}).get('partial_effects_possible'))]
        return {'task':self.view(task),'events':events,'next_event':events[-1]['seq'] if events else after,
                'unresolved':unresolved}

    def submit(self,prompt,*,new_task=False,allow_execution=False):
        with self.guard:
            task=self.store.submit(prompt,new_task=new_task,allow_execution=allow_execution)
            return self.resume(task['id'])

    def resume(self,task_id):
        with self.guard:
            task=self.store.task(task_id)
            if task['status']=='completed' or self.active()==task_id: return self.view(task)
            if self.busy(): raise ProjectBusy('Another task is executing. Pause it before starting another task.')
            uncertain=self.unresolved()
            if uncertain and uncertain['task_id']!=task_id:
                raise ProjectBusy('An earlier command needs reconciliation. Open task '+uncertain['task_id']+' first.')
            self.store.patch(task_id,pause_requested=False,status='queued',error='')
            self.active_id=task_id
            def run():
                transport=self.transport_factory() if self.transport_factory else None
                Engine(self.repo,transport=transport,retries=self.retries).run(task_id)
            self.thread=threading.Thread(target=run,name='StateTree-task-'+task_id[:8],daemon=True)
            self.thread.start()
            return self.view(self.store.task(task_id))

    def pause(self,task_id):
        t=self.store.task(task_id)
        if t['status']=='completed': return self.view(t)
        value=self.store.patch(task_id,pause_requested=True)
        self.store.event(task_id,'pause_requested',{'message':'Stop requested. An in-flight model call may finish, but no subsequent tool will start.'})
        if self.active()!=task_id:
            value=self.store.patch(task_id,status='paused')
        return self.view(value)

    def resolve(self,task_id,op_id,note,*,confirm):
        if confirm is not True: raise ValueError('Explicit confirmation of the inspected outcome is required')
        if self.busy(): raise ProjectBusy('Pause the task and wait for its worker before reconciliation')
        op=self.store.operation(op_id)
        if op is None or op['task_id']!=task_id: raise ValueError('Operation does not belong to this task')
        if op['receipt'] is not None and not op['receipt'].get('partial_effects_possible'):
            raise ValueError('This operation already has a completed receipt')
        # Never accept operator reconciliation while a receipt-writing child is still alive.
        with project_lock(self.store.root/(op_id.replace(':','_')+'.lock')):
            resolution=self.store.resolve(op_id,note)
        self.store.event(task_id,'operator_resolution',{'operation_id':op_id,**resolution})
        self.store.patch(task_id,status='paused',error='Outcome recorded by the operator. Resume to continue.',note=Engine(self.repo)._note(task_id))
        return self.view(self.store.task(task_id))

    def close(self):
        active=self.active()
        if active:
            self.pause(active)
            self.thread.join(timeout=1)
