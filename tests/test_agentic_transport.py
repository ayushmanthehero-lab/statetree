"""Real HTTP protocol checks, using fixtures rather than model inference."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import pytest
from statetree.agentic.transport import LocalTransport, ContextTooLarge
from statetree.agentic.tools import TOOL_SPECS
from tests.test_agentic_engine import fixture, final
from statetree.agentic.engine import Engine
from statetree.project import Project


def server_at(count=300,redirect=False):
    seen=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def do_POST(self):
            body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            seen.append((self.path,body,self.headers.get('Authorization')))
            if redirect:
                self.send_response(302); self.send_header('Location','http://example.invalid/'); self.end_headers(); return
            data={'prompt':'<formatted prompt with tools>'} if self.path=='/apply-template' else {'tokens':list(range(count))} if self.path=='/tokenize' else final()
            payload=json.dumps(data).encode(); self.send_response(200); self.send_header('Content-Length',str(len(payload))); self.end_headers(); self.wfile.write(payload)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
    config={'model_id':'Qwen3.5-4B','url':f'http://127.0.0.1:{server.server_port}/v1/chat/completions','max_tokens':512,'context_window_limit':4096}
    return server,thread,seen,config


def test_real_http_includes_tools_and_tokenizes_formatted_prompt(monkeypatch):
    server,thread,seen,config=server_at()
    monkeypatch.setenv('STATETREE_LOCAL_API_KEY','private-test-key')
    try:
        m=LocalTransport(config); messages=[{'role':'user','content':'Build a file'}]
        value=m.complete(messages,TOOL_SPECS)
        assert value['choices'][0]['message']['content']=='Done'
        assert [x[0] for x in seen]==['/apply-template','/tokenize','/v1/chat/completions']
        assert seen[0][1]['tools']==seen[2][1]['tools']==TOOL_SPECS
        assert seen[0][1]['chat_template_kwargs']=={'enable_thinking':False}
        assert seen[1][1]['parse_special'] is True
        assert all(x[2]=='Bearer private-test-key' for x in seen)
        assert m.last_preflight['input_tokens']==300
    finally: server.shutdown(); server.server_close(); thread.join(3)


def test_context_overflow_never_dispatches_generation():
    server,thread,seen,config=server_at(4000)
    try:
        with pytest.raises(ContextTooLarge): LocalTransport(config).complete([],TOOL_SPECS)
        assert not any(x[0]=='/v1/chat/completions' for x in seen)
    finally: server.shutdown(); server.server_close(); thread.join(3)


def test_no_redirect_to_external_host():
    server,thread,seen,config=server_at(redirect=True)
    try:
        with pytest.raises(ValueError,match='redirect'): LocalTransport(config).complete([],TOOL_SPECS)
        assert len(seen)==1
    finally: server.shutdown(); server.server_close(); thread.join(3)


def test_preflight_rejection_is_not_recorded_as_provider_usage(tmp_path):
    repo,s=fixture(tmp_path); t=s.submit('Build something')
    server,thread,seen,config=server_at(4000)
    try:
        r=Engine(repo,transport=LocalTransport(config),retries=1).run(t['id'])
        assert r['status']=='paused'
        assert Project(repo).usage()['requests']==0, 'No generation was sent: tokenizer requests are not model usage'
    finally: server.shutdown(); server.server_close(); thread.join(3)
