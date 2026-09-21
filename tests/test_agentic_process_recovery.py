"""Actual coordinator process death with an independently durable command receipt."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import pytest
from tests.test_agentic_engine import fixture, Scripted, final
from statetree.agentic.engine import Engine
from statetree.agentic.manager import TaskManager


def wait_for(predicate,seconds=12):
    end=time.monotonic()+seconds
    while time.monotonic()<end:
        value=predicate()
        if value: return value
        time.sleep(.05)
    pytest.fail('Timed out waiting for durable evidence')


def test_killed_coordinator_recovers_command_without_duplicate_effect(tmp_path):
    repo,s=fixture(tmp_path); task=s.submit('Increment a counter and finish',allow_execution=True)
    code='from pathlib import Path; import time; p=Path("counter"); p.write_text(str(int(p.read_text())+1) if p.exists() else "1"); time.sleep(2)'
    program='''import sys,json
from tests.test_agentic_engine import Scripted,tool,final
from statetree.agentic.engine import Engine
Engine(sys.argv[1],transport=Scripted(tool('run_command',{'argv':['python','-c',sys.argv[3]],'timeout':15}),final())).run(sys.argv[2])
'''
    env=dict(os.environ); env['PYTHONPATH']=str(Path(__file__).resolve().parents[1])
    proc=subprocess.Popen([sys.executable,'-c',program,str(repo),task['id'],code],env=env,
                          stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    try:
        wait_for(lambda:(repo/'counter').exists())
        proc.kill(); proc.wait(5)
        # The independent worker still writes its receipt. Fresh engine may safely wait for it.
        r=Engine(repo,transport=Scripted(final('Recovered')),retries=1).run(task['id'])
        assert r['status']=='completed',r['error']
        assert (repo/'counter').read_text()=='1'
        ops=s.operations(task['id']); assert len(ops)==1 and ops[0]['receipt']['exit_code']==0
        assert ops[0]['checkpoint_id']
    finally:
        if proc.poll() is None: proc.kill(); proc.wait(5)
        proc.communicate(timeout=5)


def test_operator_resolution_is_required_and_preserves_original_receipt(tmp_path):
    repo,s=fixture(tmp_path); t=s.submit('Inspect stopped work',allow_execution=True)
    s.prepare(t['id'],'interrupted:0','run_command',{'argv':['python','-c','pass']},{'argv':['python','-c','pass'],'timeout':10})
    receipt={'status':'interrupted','partial_effects_possible':True,'text':'Stopped after partial output','exit_code':None}
    s.finish('interrupted:0',receipt)
    assert Engine(repo,transport=Scripted(final())).run(t['id'])['status']=='needs_review'
    manager=TaskManager(repo)
    with pytest.raises(ValueError): manager.resolve(t['id'],'interrupted:0','I inspected files.',confirm=False)
    manager.resolve(t['id'],'interrupted:0','I inspected all affected files. No further effects are pending.',confirm=True)
    assert s.operation('interrupted:0')['receipt']==receipt
    assert Engine(repo,transport=Scripted(final())).run(t['id'])['status']=='completed'
