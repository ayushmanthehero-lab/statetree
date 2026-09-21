"""Real HTTP task control against scripted model responses and real local effects."""
import http.client
import json
import threading
import time
import pytest
from statetree.web.workbench import make_server
from tests.test_agentic_engine import fixture, Scripted, tool, final


def setup(tmp_path, responses):
    repo,s=fixture(tmp_path)
    server=make_server(repo,port=0,token='a-test-token-long-enough-for-local')
    assert hasattr(server,'task_manager'), 'Workbench has no durable task controller'
    server.task_manager.transport_factory=lambda:Scripted(*responses)
    thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
    return repo,s,server,thread


def req(server,method,path,data=None,auth=True):
    c=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=5)
    headers={'Content-Type':'application/json'}
    if auth: headers['Authorization']='Bearer '+server.token
    c.request(method,path,json.dumps(data) if data is not None else None,headers)
    r=c.getresponse(); status=r.status; body=json.loads(r.read()); c.close()
    return status,body


def stop(server,thread):
    server.shutdown(); server.server_close(); thread.join(5)


def wait(server,tid):
    for _ in range(100):
        _,v=req(server,'GET','/api/task?id='+tid)
        if v['task']['status'] in ('completed','paused','needs_review'): return v
        time.sleep(.05)
    pytest.fail('Task did not settle')


def test_tasks_submit_poll_and_completed_resend(tmp_path):
    repo,s,server,thread=setup(tmp_path,[tool('write_file',{'path':'x.py','content':'print(1)'}),final()])
    try:
        code,t=req(server,'POST','/api/tasks',{'prompt':'Create x.py','allow_execution':False})
        assert code==202
        value=wait(server,t['id']); assert value['task']['status']=='completed'
        assert (repo/'x.py').read_text()=='print(1)'
        assert any(e['kind']=='checkpoint' for e in value['events'])
        code,t2=req(server,'POST','/api/tasks',{'prompt':' Create  x.py ','allow_execution':False})
        assert code==202 and t2['id']==t['id']
        assert req(server,'GET','/api/tasks')[1]['items'][0]['id']==t['id']
    finally: stop(server,thread)


def test_task_endpoints_require_auth_and_explicit_fields(tmp_path):
    repo,s,server,thread=setup(tmp_path,[final()])
    try:
        assert req(server,'GET','/api/tasks',auth=False)[0]==401
        assert req(server,'POST','/api/tasks',{'prompt':'hi','url':'https://example.com'})[0]==400
        assert req(server,'POST','/api/tasks',{'prompt':'hi','allow_execution':'yes'})[0]==400
        assert req(server,'POST','/api/task/resolve',{'task_id':'x','operation_id':'y','note':'test'})[0]==400
    finally: stop(server,thread)


def test_slow_inference_does_not_block_status_or_pause(tmp_path):
    repo,s,server,thread=setup(tmp_path,[])
    entered=threading.Event(); release=threading.Event()
    class Slow:
        def complete(self,*args):
            entered.set(); release.wait(10)
            return tool('write_file',{'path':'should_not_exist','content':'x'})
    server.task_manager.transport_factory=Slow
    try:
        _,t=req(server,'POST','/api/tasks',{'prompt':'Wait for model'})
        assert entered.wait(2)
        started=time.monotonic()
        assert req(server,'GET','/api/tasks')[0]==200
        assert req(server,'GET','/api/status')[0]==200
        assert req(server,'POST','/api/task/pause',{'task_id':t['id']})[0]==200
        assert time.monotonic()-started<2
        release.set(); assert wait(server,t['id'])['task']['status']=='paused'
        assert not (repo/'should_not_exist').exists()
    finally: release.set(); stop(server,thread)


def test_restart_can_resume_persisted_task(tmp_path):
    repo,s,server,thread=setup(tmp_path,[tool('write_file',{'path':'x','content':'one'}),ConnectionError('offline')])
    server.task_manager.retries=1
    try:
        _,t=req(server,'POST','/api/tasks',{'prompt':'Create x then finish'})
        assert wait(server,t['id'])['task']['status']=='paused'
    finally: stop(server,thread)
    server=make_server(repo,port=0,token='another-long-test-token-for-local')
    server.task_manager.transport_factory=lambda:Scripted(final())
    thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
    try:
        before=(repo/'x').stat().st_mtime_ns
        assert req(server,'POST','/api/task/resume',{'task_id':t['id']})[0]==202
        assert wait(server,t['id'])['task']['status']=='completed'
        assert (repo/'x').stat().st_mtime_ns==before
    finally: stop(server,thread)
