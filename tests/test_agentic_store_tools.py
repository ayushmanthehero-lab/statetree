"""Actual local files/commands; no model is involved in these tests."""
import importlib
import importlib.util
import json
from pathlib import Path
import sys
import time
import pytest


def module(name):
    assert importlib.util.find_spec('statetree.agentic') is not None, 'Durable agent package is missing'
    return importlib.import_module('statetree.agentic.' + name)


def components(tmp_path, allow=True):
    repo = tmp_path / 'project with spaces'
    repo.mkdir()
    store = module('store').TaskStore(repo)
    task = store.submit('Build a tested greeting program', allow_execution=allow)
    tools = module('tools').Toolbox(store, task['id'])
    return repo, store, task, tools


def test_retries_share_id_and_new_task_is_explicit(tmp_path):
    repo, s, t, _ = components(tmp_path)
    assert s.submit(' Build a tested  greeting program ', allow_execution=False)['id'] == t['id']
    assert s.task(t['id'])['allow_execution'] is True
    assert s.submit(t['prompt'], new_task=True)['id'] != t['id']


def test_receipt_is_immutable_and_survives_reopening(tmp_path):
    repo,s,t,_ = components(tmp_path)
    s.prepare(t['id'], 'op1', 'read_file', {'path':'a'})
    s.finish('op1', {'text':'abc'})
    s.finish('op1', {'text':'abc'})
    with pytest.raises(ValueError): s.finish('op1', {'text':'different'})
    with pytest.raises(ValueError): s.prepare(t['id'],'op1','read_file',{'path':'b'})
    assert module('store').TaskStore(repo).operation('op1')['receipt'] == {'text':'abc'}


def test_real_write_edit_read_and_replay(tmp_path):
    repo,s,t,tools = components(tmp_path)
    r=tools.execute('op1','write_file',{'path':'hello.py','content':'print("hi")\n','expected_sha256':'missing'})
    assert r['status']=='ok'
    r2=tools.execute('op1','write_file',{'path':'hello.py','content':'print("hi")\n','expected_sha256':'missing'})
    assert r2==r and len(s.operations(t['id']))==1
    read=tools.execute('op2','read_file',{'path':'hello.py'})
    assert 'print' in read['text']
    tools.execute('op3','edit_file',{'path':'hello.py','old':'hi','new':'hello'})
    assert (repo/'hello.py').read_text()=='print("hello")\n'


@pytest.mark.parametrize('path',['../outside.py','.statetree/a','.env','C:/data.py','a\\b','.git/config'])
def test_private_and_escaping_paths_rejected(tmp_path,path):
    repo,s,t,tools=components(tmp_path)
    r=tools.execute('op','write_file',{'path':path,'content':'x','expected_sha256':'missing'})
    assert r['status']=='error'
    assert not (tmp_path/'outside.py').exists()


def test_symlink_not_followed(tmp_path):
    repo,s,t,tools=components(tmp_path)
    (repo/'link').symlink_to(tmp_path, target_is_directory=True)
    assert tools.execute('op','write_file',{'path':'link/out.txt','content':'x'})['status']=='error'


def test_native_execution_requires_explicit_consent(tmp_path):
    repo,s,t,tools=components(tmp_path,False)
    r=tools.execute('cmd','run_command',{'argv':['python','-c','print(42)']})
    assert r['status']=='error' and 'approval' in r['text'].lower()


def test_python_command_replay_does_not_repeat_side_effect(tmp_path):
    repo,s,t,tools=components(tmp_path)
    args={'argv':['python','-c','from pathlib import Path; p=Path("counter"); p.write_text(str(int(p.read_text())+1) if p.exists() else "1"); print("worked")']}
    r=tools.execute('cmd','run_command',args)
    assert r['exit_code']==0 and 'worked' in r['text']
    assert tools.execute('cmd','run_command',args)==r
    assert (repo/'counter').read_text()=='1'


def test_dispatched_command_without_receipt_is_not_repeated(tmp_path):
    repo,s,t,tools=components(tmp_path)
    args={'argv':['python','-c','print(42)']}
    s.prepare(t['id'],'cmd','run_command',args,{'argv':[sys.executable,'-c','print(42)'],'timeout':30})
    s.mark_dispatched('cmd')
    with pytest.raises(module('tools').UncertainOperation): tools.execute('cmd','run_command',args)


def test_write_reconciles_effect_receipt_gap(tmp_path):
    repo,s,t,tools=components(tmp_path)
    content='hello\n'
    args={'path':'hello.txt','content':content,'expected_sha256':'missing'}
    import hashlib
    s.prepare(t['id'],'op','write_file',args,{'path':'hello.txt','before':None,'after':hashlib.sha256(content.encode()).hexdigest(),'content':content})
    (repo/'hello.txt').write_text(content)
    r=tools.execute('op','write_file',args)
    assert r['status']=='ok' and r['reconciled']


def test_write_conflict_after_crash_requires_review(tmp_path):
    repo,s,t,tools=components(tmp_path)
    args={'path':'hello.txt','content':'planned','expected_sha256':'missing'}
    import hashlib
    s.prepare(t['id'],'op','write_file',args,{'path':'hello.txt','before':None,'after':hashlib.sha256(b'planned').hexdigest(),'content':'planned'})
    (repo/'hello.txt').write_text('external change')
    with pytest.raises(module('tools').UncertainOperation): tools.execute('op','write_file',args)


def test_receipts_paged_not_whole_history(tmp_path):
    repo,s,t,tools=components(tmp_path)
    tools.execute('op','write_file',{'path':'a.txt','content':'a'*5000})
    out=tools.execute('read','read_evidence',{'operation_id':'op','offset':0,'limit':100})
    assert len(out['text'])<=100 and out['next_offset'] is not None


def test_bad_tool_arguments_are_receipted_errors(tmp_path):
    repo,s,t,tools=components(tmp_path)
    assert tools.execute('op','read_file',{'path':'x','bogus':True})['status']=='error'
    assert tools.execute('op2','run_command',{'argv':'rm -rf anything'})['status']=='error'
