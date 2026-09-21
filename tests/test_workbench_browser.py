"""Optional real Chromium UI checks; no external URLs or model calls.

Install playwright and Chromium to run. Set STATETREE_BROWSER_ARTIFACTS to retain
screenshots. No token is written into a screenshot or a URL.
"""
import importlib.util
import http.client
import re
import os
from pathlib import Path
import shutil
import tempfile
import threading
import unittest

from statetree.project import Project
from statetree.web.workbench import make_server
from tests.test_project import repo_at

BROWSER = shutil.which('chromium') or shutil.which('google-chrome')
AVAILABLE = bool(importlib.util.find_spec('playwright') and BROWSER)


@unittest.skipUnless(AVAILABLE, 'Optional Playwright + Chromium browser dependencies are not installed')
class BrowserTests(unittest.TestCase):
    def test_desktop_and_mobile_real_forms_and_no_script_errors(self):
        from playwright.sync_api import sync_playwright, expect
        with tempfile.TemporaryDirectory() as td:
            repo = repo_at(Path(td) / 'export-service')
            project = Project.init(repo, goal='Build a reliable export service')
            project.remember('export.retention_days', 7, evidence=['caller:approved-retention'])
            project.checkpoint('Export retention is 7 days. Completed files are deleted after 7 days.')
            token = 'private-browser-test-token-not-in-url'
            server = make_server(repo, port=0, token=token)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with sync_playwright() as p:
                    browser = p.chromium.launch(executable_path=BROWSER, headless=True, args=['--no-sandbox'])
                    page = browser.new_page(viewport={'width': 1440, 'height': 1040})
                    errors = []
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    # This runtime's managed Chromium blocks direct loopback
                    # navigation. Render local assets in memory and bridge only
                    # this project's API calls to the real HTTP server. HTTP
                    # headers/CSP are tested independently in test_workbench.py.
                    # No browser policy is modified or disabled.
                    def local_request(request):
                        if not request['path'].startswith('/api/'):
                            raise ValueError('Only local project API paths are allowed')
                        connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=15)
                        connection.request(request.get('method', 'GET'), request['path'],
                                           request.get('body'), request.get('headers', {}))
                        response = connection.getresponse()
                        value = {'status': response.status, 'body': response.read().decode('utf-8')}
                        connection.close()
                        return value
                    assets = Path(__file__).resolve().parents[1] / 'statetree/web/static'
                    html = (assets / 'workbench.html').read_text()
                    html = re.sub(r'<link[^>]+>|<script[^>]*>.*?</script>', '', html)
                    page.set_content(html)
                    page.add_style_tag(content=(assets / 'workbench.css').read_text())
                    page.expose_function('statetreeTestRequest', local_request)
                    page.evaluate('''() => { window.fetch = async (path, options = {}) => {
                        const result = await window.statetreeTestRequest({path, ...options});
                        return {ok: result.status >= 200 && result.status < 300,
                                status: result.status, json: async () => JSON.parse(result.body)};
                    }; }''')
                    page.add_script_tag(content=(assets / 'workbench.js').read_text())
                    page.get_by_label('Local access token', exact=True).fill(token)
                    page.get_by_role('button', name='Open project').click()
                    expect(page.locator('#app')).to_be_visible()
                    expect(page.locator('#metric-facts')).to_have_text('1')
                    page.get_by_label('Commit note', exact=True).fill('Browser checkpoint: preserve export retention')
                    page.get_by_role('button', name='Save checkpoint', exact=True).click()
                    expect(page.locator('#notice')).to_contain_text('saved')
                    self.assertEqual(Project(repo).history()['items'][0]['note']['summary'],
                                     'Browser checkpoint: preserve export retention')
                    page.get_by_role('button', name='Context lab', exact=True).click()
                    page.get_by_label('Next question', exact=True).fill('What is the agreed export retention period?')
                    page.get_by_role('button', name='Preview context · no model call', exact=True).click()
                    expect(page.locator('#context-summary')).to_contain_text('No model call')
                    self.assertEqual(Project(repo).usage()['requests'], 0)
                    for tab in ['Memory', 'Branches', 'Local chat', 'Settings & portability', 'Timeline']:
                        page.get_by_role('button', name=tab, exact=True).click()
                    directory = os.environ.get('STATETREE_BROWSER_ARTIFACTS')
                    if directory:
                        Path(directory).mkdir(parents=True, exist_ok=True)
                        page.screenshot(path=str(Path(directory) / 'workbench-desktop.png'), full_page=True)
                    self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), 1440)
                    page.set_viewport_size({'width': 390, 'height': 844})
                    for tab in ['Memory', 'Branches', 'Context lab', 'Local chat', 'Settings & portability', 'Timeline']:
                        page.get_by_role('button', name=tab, exact=True).click()
                        self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), 390)
                    if directory:
                        page.screenshot(path=str(Path(directory) / 'workbench-mobile.png'), full_page=True)
                    page.get_by_role('button', name='Lock', exact=True).click()
                    expect(page.locator('#auth-screen')).to_be_visible()
                    self.assertEqual(page.locator('#history-list').text_content(), '')
                    self.assertEqual(errors, [])
                    browser.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
