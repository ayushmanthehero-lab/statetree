import importlib
import importlib.util
import json
import subprocess
from pathlib import Path
import pytest
from statetree.project import Project


def engine_module():
    assert importlib.util.find_spec('statetree.agentic.engine') is not None, 'Resumable engine is missing'
    return importlib.import_module('statetree.agentic.engine')


def fixture(tmp_path):
    repo=tmp_path/'repo'; repo.mkdir()
    subprocess.run(['git','init','-q',str(repo)],check=True)
    subprocess.run(['git','-C',str(repo),'-c','user.name=test','-c','user.email=test@local','commit','--allow-empty','-qm','start'],check=True)
    Project.init(repo,goal='Test project')
    from statetree.agentic.store import TaskStore
    return repo,TaskStore(repo)


def tool(name,args):
    return {'choices':[{'finish_reason':'tool_calls','message':{'role':'assistant','content':None,
      'tool_calls':[{'id':'provider-id','type':'function','function':{'name':name,'arguments':json.dumps(args)}}]}}],
      'usage':{'prompt_tokens':100,'completion_tokens':20,'total_tokens':120}}


def final(text='Done'):
    return {'choices':[{'finish_reason':'stop','message':{'role':'assistant','content':text}}],
            'usage':{'prompt_tokens':100,'completion_tokens':10,'total_tokens':110}}


class Scripted:
    def __init__(self,*responses): self.responses=list(responses); self.requests=[]
    def complete(self,messages,tools):
        self.requests.append(messages)
        x=self.responses.pop(0)
        if isinstance(x,BaseException): raise x
        return x


def test_file_command_checkpoint_sequence(tmp_path):
    E=engine_module(); repo,s=fixture(tmp_path)
    task=s.submit('Make hello.py print hello, run it, and finish.',allow_execution=True)
    model=Scripted(tool('write_file',{'path':'hello.py','content':'print("hello")\n'}),
                   tool('run_command',{'argv':['python','hello.py']}),final('Created and ran hello.py.'))
    result=E.Engine(repo,transport=model,retries=1).run(task['id'])
    assert result['status']=='completed'
    assert (repo/'hello.py').read_text()=='print("hello")\n'
    ops=s.operations(task['id']); assert len(ops)==2
    assert all(op['checkpoint_id'] for op in ops)
    assert ops[1]['receipt']['exit_code']==0
    assert Project(repo).usage()['requests']==3
    assert len(Project(repo).history()['items'])>=4
    assert all(len(r)==2 for r in model.requests)  # not an ever-growing message transcript


def test_network_failure_resume_does_not_reexecute_completed_write(tmp_path):
    E=engine_module(); repo,s=fixture(tmp_path)
    t=s.submit('Create a file and finish.')
    model=Scripted(tool('write_file',{'path':'x','content':'completed'}),ConnectionError('network gone'))
    assert E.Engine(repo,transport=model,retries=1).run(t['id'])['status']=='paused'
    before=(repo/'x').stat().st_mtime_ns
    retry=s.submit(t['prompt']); assert retry['id']==t['id']
    model2=Scripted(final())
    assert E.Engine(repo,transport=model2,retries=1).run(t['id'])['status']=='completed'
    assert (repo/'x').stat().st_mtime_ns==before
    context=json.dumps(model2.requests[0]); assert 'write_file' in context and 'checkpoint' in context.lower()
    assert len(s.operations(t['id']))==1


def test_crash_after_receipt_before_checkpoint_repaired_without_tool_replay(tmp_path):
    E=engine_module(); repo,s=fixture(tmp_path)
    t=s.submit('Create x then finish.')
    engine=E.Engine(repo,transport=Scripted(tool('write_file',{'path':'x','content':'saved'})),retries=1)
    original=engine.checkpoint
    def crash(*a,**kw): raise SystemExit('injected process death')
    engine.checkpoint=crash
    with pytest.raises(SystemExit): engine.run(t['id'])
    assert (repo/'x').read_text()=='saved'
    before=(repo/'x').stat().st_mtime_ns
    assert s.operations(t['id'])[0]['checkpoint_id'] is None
    assert E.Engine(repo,transport=Scripted(final()),retries=1).run(t['id'])['status']=='completed'
    assert (repo/'x').stat().st_mtime_ns==before
    assert s.operations(t['id'])[0]['checkpoint_id'] is not None


def test_saved_pending_batch_continues_from_second_tool(tmp_path):
    E=engine_module(); repo,s=fixture(tmp_path)
    t=s.submit('Write two files.')
    response=tool('write_file',{'path':'a','content':'A'})
    response['choices'][0]['message']['tool_calls'].append(tool('write_file',{'path':'b','content':'B'})['choices'][0]['message']['tool_calls'][0])
    # Provider IDs may collide; StateTree's IDs are per response position.
    engine=E.Engine(repo,transport=Scripted(response),retries=1)
    original=engine.checkpoint
    def pause_after_first(*args,**kwargs):
        value=original(*args,**kwargs)
        s.patch(t['id'],pause_requested=True)
        return value
    engine.checkpoint=pause_after_first
    assert engine.run(t['id'])['status']=='paused'
    assert (repo/'a').exists() and not (repo/'b').exists()
    s.patch(t['id'],pause_requested=False)
    assert E.Engine(repo,transport=Scripted(final()),retries=1).run(t['id'])['status']=='completed'
    assert (repo/'b').read_text()=='B'


def test_truncated_tool_call_never_executes(tmp_path):
    E=engine_module(); repo,s=fixture(tmp_path)
    t=s.submit('Write something.')
    response=tool('write_file',{'path':'x','content':'not saved'})
    response['choices'][0]['finish_reason']='length'
    r=E.Engine(repo,transport=Scripted(response),retries=1).run(t['id'])
    assert r['status']=='paused' and not (repo/'x').exists()
    assert 'output' in r['error'].lower()


def test_rolling_note_retains_plan_not_full_transcript(tmp_path):
    E=engine_module(); repo,s=fixture(tmp_path)
    t=s.submit('Remember a plan and inspect files.')
    model=Scripted(tool('update_plan',{'summary':'Build parser then test it','next_step':'Inspect sources'}),
                   tool('list_files',{}),final())
    E.Engine(repo,transport=model,retries=1).run(t['id'])
    assert 'Build parser then test it' in json.dumps(model.requests[-1])
    assert 'tool_calls' not in json.dumps(model.requests[-1])


def test_restored_project_checkpoint_blocks_old_task_resume(tmp_path):
    E=engine_module(); repo,s=fixture(tmp_path)
    head=Project(repo).runtime._parent
    t=s.submit('Write x.')
    E.Engine(repo,transport=Scripted(tool('write_file',{'path':'x','content':'X'}),ConnectionError()),retries=1).run(t['id'])
    Project(repo).restore(head)
    r=E.Engine(repo,transport=Scripted(final()),retries=1).run(t['id'])
    assert r['status']=='needs_review'
    assert 'checkpoint' in r['error'].lower()


def test_interrupted_command_needs_review_even_after_resume(tmp_path):
    E=engine_module(); repo,s=fixture(tmp_path)
    t=s.submit('Run a check.',allow_execution=True)
    response=tool('run_command',{'argv':['python','-c','pass']})
    s.patch(t['id'],pending={'id':'saved-turn','raw':response,'calls':None,'text':None})
    s.prepare(t['id'],'saved-turn:0','run_command',{'argv':['python','-c','pass']},{'argv':['python','-c','pass'],'timeout':30})
    s.finish('saved-turn:0',{'status':'interrupted','text':'Stopped','partial_effects_possible':True,'exit_code':None})
    first=E.Engine(repo,transport=Scripted(final()),retries=1).run(t['id'])
    assert first['status']=='needs_review'
    second=E.Engine(repo,transport=Scripted(final()),retries=1).run(t['id'])
    assert second['status']=='needs_review', 'Resume must not bypass uncertain-effect reconciliation'


def test_old_unknown_command_cannot_be_hidden_by_recent_tasks(tmp_path):
    from statetree.agentic.manager import TaskManager
    repo,s=fixture(tmp_path); t=s.submit('Old uncertain command',allow_execution=True)
    s.prepare(t['id'],'old:0','run_command',{'argv':['python','x.py']},{'argv':['python','x.py'],'timeout':30})
    s.mark_dispatched('old:0')
    for i in range(205): s.submit(f'Another task {i}')
    assert TaskManager(repo).unresolved() is not None
