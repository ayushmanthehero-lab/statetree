"""Real Chromium DOM + Python HTTP bridge; scripted model, actual file effects."""
import http.client
import os
import re
from pathlib import Path
import threading
import pytest
from tests.test_workbench_browser import AVAILABLE,BROWSER
from tests.test_agentic_engine import fixture,Scripted,tool,final
from statetree.web.workbench import make_server


@pytest.mark.skipif(not AVAILABLE,reason='Optional Playwright and Chromium are unavailable')
def test_agent_form_tools_checkpoints_completed_resend_and_lock(tmp_path):
    from playwright.sync_api import sync_playwright,expect
    repo,s=fixture(tmp_path); server=make_server(repo,port=0,token='test-only-agent-browser-token')
    server.task_manager.transport_factory=lambda:Scripted(tool('write_file',{'path':'hello.py','content':'print("hello")\n'}),
         tool('run_command',{'argv':['python','hello.py']}),final('Created and ran hello.py.'))
    thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
    try:
        with sync_playwright() as p:
            browser=p.chromium.launch(executable_path=BROWSER,headless=True,args=['--no-sandbox'])
            page=browser.new_page(viewport={'width':1440,'height':1024}); errors=[]
            page.on('pageerror',lambda error:errors.append(str(error)))
            def bridge(r):
                if not r['path'].startswith('/api/'): raise ValueError('Only local API')
                c=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=15)
                c.request(r.get('method','GET'),r['path'],r.get('body'),r.get('headers',{}))
                v=c.getresponse(); result={'status':v.status,'body':v.read().decode()}; c.close(); return result
            assets=Path(__file__).resolve().parents[1]/'statetree/web/static'
            page.set_content(re.sub(r'<link[^>]+>|<script[^>]*>.*?</script>','',(assets/'workbench.html').read_text()))
            page.add_style_tag(content=(assets/'workbench.css').read_text())
            page.expose_function('testRequest',bridge)
            page.evaluate('''() => {window.fetch=async (path,options={})=>{const r=await window.testRequest({path,...options});return {ok:r.status>=200&&r.status<300,status:r.status,json:async()=>JSON.parse(r.body)}}}''')
            for name in ('workbench.js','agentic.js'): page.add_script_tag(content=(assets/name).read_text())
            page.get_by_label('Local access token',exact=True).fill(server.token)
            page.get_by_role('button',name='Open project',exact=True).click()
            expect(page.locator('#app')).to_be_visible()
            page.get_by_role('button',name='Agent tasks',exact=True).click()
            page.get_by_label('Task instructions',exact=True).fill('Create hello.py, run it, and finish.')
            page.locator('#agent-execute').check()
            page.get_by_role('button',name='Run / resume automatically',exact=True).click()
            expect(page.locator('#agent-status')).to_have_text('completed',timeout=20000)
            assert (repo/'hello.py').exists()
            expect(page.locator('#metric-requests')).to_have_text('3')
            expect(page.locator('#agent-final')).to_contain_text('ran hello.py')
            expect(page.locator('#agent-events')).to_contain_text('checkpoint')
            before=(repo/'hello.py').stat().st_mtime_ns
            page.get_by_role('button',name='Run / resume automatically',exact=True).click()
            expect(page.locator('#agent-status')).to_have_text('completed')
            assert len(s.tasks())==1 and (repo/'hello.py').stat().st_mtime_ns==before
            assert page.evaluate('document.documentElement.scrollWidth')<=1440
            directory=os.environ.get('STATETREE_BROWSER_ARTIFACTS')
            if directory:
                Path(directory).mkdir(parents=True,exist_ok=True)
                page.screenshot(path=str(Path(directory)/'agentic-desktop.png'),full_page=True)
            page.set_viewport_size({'width':390,'height':844})
            assert page.evaluate('document.documentElement.scrollWidth')<=390
            if directory: page.screenshot(path=str(Path(directory)/'agentic-mobile.png'),full_page=True)
            page.get_by_role('button',name='Lock',exact=True).click()
            expect(page.locator('#auth-screen')).to_be_visible()
            assert page.locator('#agent-events').text_content()==''
            assert page.locator('#agent-prompt').input_value()==''
            assert not page.locator('#agent-execute').is_checked()
            assert errors==[]
            browser.close()
    finally: server.shutdown(); server.server_close(); thread.join(5)
