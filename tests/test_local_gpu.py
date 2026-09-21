"""GPU launcher tests. Fixtures are not actual GPU/model execution."""
import hashlib
import importlib
import importlib.util
import io
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile


class GPUFeatureTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('statetree.local_gpu'), 'GPU launcher is not implemented')
        self.gpu = importlib.import_module('statetree.local_gpu')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_profile_uses_quantized_text_model_and_small_gpu_buffers(self):
        p = self.gpu.GPUProfile()
        self.assertEqual(p.context_size, 4096)
        command = self.gpu.server_command(Path('C:/model server/llama-server.exe'), Path('C:/models/qwen.gguf'), p)
        for flag, value in [('--gpu-layers', 'auto'), ('--fit-target','768'), ('--parallel','1'),
                            ('--ubatch-size','64'), ('--batch-size','256'), ('--host','127.0.0.1'),
                            ('--ctx-size','4096'), ('--flash-attn','off')]:
            self.assertEqual(command[command.index(flag)+1],value)
        self.assertIn('--no-mmproj',command)
        self.assertIn('--jinja',command)
        self.assertIn('--offline',command)
        self.assertNotIn('--hf-repo',command)
        self.assertIn('C:/models/qwen.gguf',command)

    def test_layer_override_never_permits_cpu_only(self):
        for layers in (0, -1, True, 'none', '0'):
            with self.subTest(layers=layers), self.assertRaises(ValueError):
                self.gpu.GPUProfile(gpu_layers=layers)
        self.assertEqual(self.gpu.GPUProfile(gpu_layers=20).gpu_layers,20)

    def test_profile_bounds_and_device_validation(self):
        for kwargs in ({'context_size':128}, {'context_size':True}, {'port':0}, {'port':8765,'web_port':8765},
                       {'max_tokens':4096}, {'reserve_mib':-1}, {'device':'none'}, {'device':'CUDA0,CPU'},
                       {'timeout':0}, {'threads':0}, {'web_port':65536}):
            with self.subTest(kwargs=kwargs),self.assertRaises(ValueError): self.gpu.GPUProfile(**kwargs)

    def test_device_parser_prefers_gtx1650_and_ignores_cuda_log_noise(self):
        text = 'ggml_cuda_init: found 2 CUDA devices\nAvailable devices:\n  CUDA0: NVIDIA RTX 4090 (24564 MiB, 24000 MiB free)\n  CUDA1: NVIDIA GeForce GTX 1650 (4096 MiB, 3250 MiB free)\n'
        self.assertEqual(self.gpu.choose_cuda_device(text), 'CUDA1')
        with self.assertRaisesRegex(RuntimeError,'CUDA'): self.gpu.choose_cuda_device('ggml_cuda_init: CUDA error\nCPU: AVX2')
        with self.assertRaises(RuntimeError): self.gpu.choose_cuda_device(text,'CUDA2')

    def test_gpu_offload_requires_actual_positive_layer_report(self):
        self.assertEqual(self.gpu.offload_evidence('load_tensors: offloaded 24/33 layers to GPU\n')['offloaded_layers'],24)
        for text in ('CUDA backend loaded','load_tensors: offloaded 0/33 layers to GPU','CPU only'):
            with self.subTest(text=text),self.assertRaises(RuntimeError): self.gpu.offload_evidence(text)

    def test_child_environment_does_not_inherit_host_or_gpu_overrides(self):
        with patch.dict('os.environ', {'LLAMA_ARG_HOST':'0.0.0.0', 'LLAMA_ARG_N_GPU_LAYERS':'0', 'LLAMA_API_KEY':'old', 'NORMAL_VAR':'keep'}):
            env = self.gpu.server_environment('new-secret')
        self.assertNotIn('LLAMA_ARG_HOST',env)
        self.assertNotIn('LLAMA_ARG_N_GPU_LAYERS',env)
        self.assertEqual(env['LLAMA_API_KEY'],'new-secret')
        self.assertEqual(env['NORMAL_VAR'],'keep')

    def test_model_header_rejects_lfs_pointer_and_empty_file(self):
        f = self.root/'model.gguf'
        for content in (b'',b'version https://git-lfs.github.com/spec/v1'):
            f.write_bytes(content)
            with self.assertRaises(ValueError): self.gpu.validate_model_file(f)
        f.write_bytes(b'GGUF'+b'\x03\x00\x00\x00'+b'x'*24)
        self.assertEqual(self.gpu.validate_model_file(f),f.resolve())

    def test_busy_port_is_not_reused_or_killed(self):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0)); sock.listen()
            with self.assertRaises(RuntimeError): self.gpu.require_free_port(sock.getsockname()[1])

    def test_profile_initializes_and_reopens_project_without_erasing_memory(self):
        from tests.test_project import repo_at
        from statetree.project import Project
        repo = repo_at(self.root/'repo')
        profile = self.gpu.GPUProfile()
        project = self.gpu.configure_project(repo,profile)
        project.remember('retention',7,evidence=['caller'])
        run_id=project.config['run_id']; old=project.status()['head']
        project = self.gpu.configure_project(repo,profile)
        self.assertEqual(project.config['run_id'],run_id)
        self.assertEqual(project.state.facts['retention']['value'],7)
        self.assertEqual(project.status()['head'],old)
        self.assertEqual(project.model_config['context_window_limit'],4096)
        self.assertLessEqual(project.config['context_budget'],4096-256-256)
        self.assertEqual(Project(repo).model_config['url'],'http://127.0.0.1:8080/v1/chat/completions')

    def test_existing_budget_verifier_and_goal_preserved(self):
        from tests.test_project import repo_at
        from statetree.project import Project
        repo = repo_at(self.root/'repo')
        p = Project.init(repo,goal='My existing project',config={'total_token_limit':1234,'verification_commands':[['python','--version']]})
        p.remember('retention',7,evidence=['caller'])
        p = self.gpu.configure_project(repo,self.gpu.GPUProfile())
        self.assertEqual(p.config['total_token_limit'],1234)
        self.assertEqual(p.config['verification_commands'],[['python','--version']])
        self.assertEqual(p.state.goal,'My existing project')
        self.assertEqual(p.state.facts['retention']['value'],7)

    def test_dry_run_uses_no_server_and_no_project_mutation(self):
        with patch.object(self.gpu,'launch') as launch:
            self.assertEqual(self.gpu.main(['start','--repo',str(self.root),'--dry-run']),0)
        launch.assert_not_called()
        self.assertFalse((self.root/'.statetree').exists())


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('statetree.gpu_download'), 'GPU asset installer is not implemented')
        self.dl = importlib.import_module('statetree.gpu_download')
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)

    def test_all_downloads_have_pinned_https_sources_and_sha256(self):
        for asset in self.dl.ASSETS:
            self.assertTrue(asset['url'].startswith('https://'))
            self.assertNotIn('/main/',asset['url'])
            self.assertNotIn('/latest/',asset['url'])
            self.assertRegex(asset['sha256'],r'^[0-9a-f]{64}$')
        self.assertEqual(len(self.dl.ASSETS),3)
        self.assertIn('Q4_K_M',self.dl.ASSETS[-1]['name'])

    def test_verified_existing_file_is_reused_without_network(self):
        data=b'preexisting';f=self.root/'test';f.write_bytes(data)
        with patch.object(self.dl,'urlopen',side_effect=AssertionError('network')):
            self.dl.download_verified('https://example.invalid/test',f,hashlib.sha256(data).hexdigest())
        self.assertEqual(f.read_bytes(),data)

    def test_wrong_checksum_is_not_published(self):
        class Response(io.BytesIO):
            status=200
            headers={}
        with patch.object(self.dl,'urlopen',return_value=Response(b'incorrect')):
            with self.assertRaisesRegex(ValueError,'SHA-256'):
                self.dl.download_verified('https://example.invalid/test',self.root/'test','0'*64)
        self.assertFalse((self.root/'test').exists())

    def test_zip_extract_rejects_traversal_before_writing_any_file(self):
        z=self.root/'bad.zip'
        with ZipFile(z,'w') as archive:
            archive.writestr('good.txt','ok');archive.writestr('../escape.txt','bad')
        with self.assertRaises(ValueError): self.dl.safe_extract(z,self.root/'out')
        self.assertFalse((self.root/'escape.txt').exists())
        self.assertFalse((self.root/'out/good.txt').exists())

    def test_zip_extraction_keeps_dll_layout(self):
        z=self.root/'good.zip'
        with ZipFile(z,'w') as archive:
            archive.writestr('bin/llama-server.exe','exe');archive.writestr('bin/ggml-cuda.dll','dll')
        self.dl.safe_extract(z,self.root/'out')
        self.assertEqual((self.root/'out/bin/ggml-cuda.dll').read_text(),'dll')


class TimeoutTests(unittest.TestCase):
    def test_local_transport_supports_a_bounded_slow_gpu_timeout(self):
        from statetree.web.app import LocalClient
        with self.assertRaises(ValueError): LocalClient('http://127.0.0.1:8080/v1/chat/completions','',timeout=0)
        client=LocalClient('http://127.0.0.1:8080/v1/chat/completions','',timeout=300)
        response=io.BytesIO(b'{"choices":[]}')
        with patch.object(client._opener,'open',return_value=response) as request:
            client.invoke_endpoint(Body=b'{}')
        self.assertEqual(request.call_args.kwargs['timeout'],300)


class LifecycleTests(unittest.TestCase):
    """A deterministic HTTP subprocess fixture, NOT a model/GPU substitute."""
    def setUp(self):
        import os
        import sys
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name); self.gpu=importlib.import_module('statetree.local_gpu')
        self.server=self.root/'fixture_server.py'
        self.server.write_text('''#!/usr/bin/env python3
import json, os, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
if '--list-devices' in sys.argv:
    print('  CUDA0: NVIDIA GeForce GTX 1650 (fixture, no GPU)'); sys.exit(0)
port=int(sys.argv[sys.argv.index('--port')+1])
print('load_tensors: offloaded '+os.environ.get('FIXTURE_LAYERS','24')+'/33 layers to GPU', flush=True)
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != '/health' and self.headers.get('Authorization') != 'Bearer '+os.environ['LLAMA_API_KEY'] and not os.environ.get('FIXTURE_NO_AUTH'):
            self.send_response(401); self.end_headers(); return
        value={'status':'ok'} if self.path=='/health' else {'data':[{'id':'Qwen/Qwen3.5-4B'}]}
        body=json.dumps(value).encode(); self.send_response(200)
        self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body)
    def log_message(self,*a): pass
HTTPServer(('127.0.0.1',port),Handler).serve_forever()
''', encoding='utf-8')
        self.server.chmod(0o755)
        self.model=self.root/'fixture.gguf'; self.model.write_bytes(b'GGUF'+b'\x03\x00\x00\x00'+b'x'*24)

    def free_port(self):
        with socket.socket() as s: s.bind(('127.0.0.1',0)); return s.getsockname()[1]

    def start_fixture(self, env=None):
        import subprocess,sys
        port=self.free_port(); key='fixture-key'
        log=self.root/'server.log'; handle=log.open('wb');self.addCleanup(handle.close)
        environ=self.gpu.server_environment(key); environ.update(env or {})
        proc=subprocess.Popen([sys.executable,str(self.server),'--port',str(port)], stdout=handle, stderr=handle, env=environ)
        self.addCleanup(self.gpu.stop_child,proc)
        return proc,port,key,log

    def test_health_positive_offload_and_auth_over_real_http(self):
        proc,port,key,log=self.start_fixture()
        self.assertEqual(self.gpu.wait_ready(proc,port,key,log,5)['offloaded_layers'],24)
        self.gpu.stop_child(proc);self.assertIsNotNone(proc.poll())

    def test_health_does_not_accept_zero_gpu_layers(self):
        proc,port,key,log=self.start_fixture({'FIXTURE_LAYERS':'0'})
        with self.assertRaisesRegex(RuntimeError,'CPU-only'): self.gpu.wait_ready(proc,port,key,log,5)

    def test_health_does_not_accept_unprotected_api(self):
        proc,port,key,log=self.start_fixture({'FIXTURE_NO_AUTH':'1'})
        with self.assertRaisesRegex(RuntimeError,'API key'): self.gpu.wait_ready(proc,port,key,log,5)

    def test_launch_passes_keyword_port_and_cleans_owned_process(self):
        import os, subprocess, sys
        from types import SimpleNamespace
        from tests.test_project import repo_at
        p=self.gpu.GPUProfile(port=self.free_port(),web_port=self.free_port(),timeout=5)
        repo=repo_at(self.root/'repo')
        actual_popen=subprocess.Popen; children=[]
        def spawn(command, **kwargs):

            if command[0] != str(self.server): return actual_popen(command, **kwargs)
            proc=actual_popen([sys.executable,*command], **kwargs);children.append(proc);return proc
        def serve(repo, *, port):
            self.assertEqual(port,p.web_port)
            self.assertEqual(os.environ['STATETREE_LOCAL_TIMEOUT'],'5')
            self.assertTrue(os.environ['STATETREE_LOCAL_API_KEY'])
        # Only SDK presence is bypassed for orchestration; no fake strands module is installed.
        with patch.object(self.gpu.importlib.util,'find_spec',return_value=SimpleNamespace()), \
             patch.object(self.gpu,'inspect_backend',return_value='CUDA0'), \
             patch.object(self.gpu.subprocess,'Popen',side_effect=spawn), \
             patch('statetree.web.workbench.serve',side_effect=serve), \
             patch.dict(os.environ,{'STATETREE_LOCAL_API_KEY':'original'}):
            self.gpu.launch(repo,self.root/'cache',p,server=self.server,model=self.model)
            self.assertEqual(os.environ['STATETREE_LOCAL_API_KEY'],'original')
        self.assertTrue(children)
        self.assertTrue(all(c.poll() is not None for c in children))

    def test_setup_failure_cleans_child_and_does_not_bind_project(self):
        import os, subprocess, sys
        from types import SimpleNamespace
        actual_popen=subprocess.Popen;children=[]
        def spawn(command, **kwargs):

            if command[0] != str(self.server): return actual_popen(command, **kwargs)
            proc=actual_popen([sys.executable,*command],**kwargs);children.append(proc);return proc
        p=self.gpu.GPUProfile(port=self.free_port(),web_port=self.free_port(),timeout=5)
        with patch.object(self.gpu.importlib.util,'find_spec',return_value=SimpleNamespace()), \
             patch.object(self.gpu,'inspect_backend',return_value='CUDA0'), \
             patch.object(self.gpu.subprocess,'Popen',side_effect=spawn), \
             patch.dict(os.environ,{'FIXTURE_LAYERS':'0'}), \
             patch.object(self.gpu,'configure_project') as configure:
            with self.assertRaises(RuntimeError): self.gpu.launch(self.root/'repo',self.root/'cache',p,server=self.server,model=self.model)
        configure.assert_not_called()
        self.assertTrue(all(c.poll() is not None for c in children))


class DownloadResumeTests(unittest.TestCase):
    def setUp(self):
        self.dl=importlib.import_module('statetree.gpu_download')
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)

    def response(self, body, status, headers=None):
        r=io.BytesIO(body);r.status=status;r.headers=headers or {};return r

    def test_resume_verifies_the_combined_file(self):
        data=b'prefix-suffix';dest=self.root/'model'
        dest.with_name('model.part').write_bytes(b'prefix-')
        r=self.response(b'suffix',206,{'Content-Range':'bytes 7-12/13'})
        with patch.object(self.dl,'urlopen',return_value=r) as request:
            self.dl.download_verified('https://example.invalid/file',dest,hashlib.sha256(data).hexdigest())
        self.assertEqual(request.call_args.args[0].get_header('Range'),'bytes=7-')
        self.assertEqual(dest.read_bytes(),data)

    def test_server_ignoring_range_restarts_not_appends(self):
        data=b'complete';dest=self.root/'file';dest.with_name('file.part').write_bytes(b'old')
        with patch.object(self.dl,'urlopen',return_value=self.response(data,200)):
            self.dl.download_verified('https://example.invalid/file',dest,hashlib.sha256(data).hexdigest())
        self.assertEqual(dest.read_bytes(),data)

    def test_custom_install_does_not_claim_pinned_asset_provenance(self):
        server=self.root/'custom-server'; server.write_text('not executed by installer')
        model=self.root/'custom.gguf'; model.write_bytes(b'GGUF'+b'\x03\x00\x00\x00'+b'x'*24)
        with patch.object(self.dl,'urlopen',side_effect=AssertionError('no downloads')):
            manifest=self.dl.install(self.root/'cache',server=server,model=model)
        self.assertEqual(manifest['binary_release'],'custom')
        self.assertEqual(manifest['model_revision'],'custom')

    def test_rejects_windows_drive_ads_and_symlink_archives(self):
        import stat
        from zipfile import ZipInfo
        for name in ('C:/escape', 'file:alternate_stream', '/absolute', 'dir/../../escape'):
            with self.subTest(name=name):
                z=self.root/'bad.zip'
                with ZipFile(z,'w') as f: f.writestr(name,'evil')
                with self.assertRaises(ValueError): self.dl.safe_extract(z,self.root/'out')
        z=self.root/'symlink.zip'
        info=ZipInfo('link');info.create_system=3;info.external_attr=(stat.S_IFLNK|0o777)<<16
        with ZipFile(z,'w') as f:f.writestr(info,'../../outside')
        with self.assertRaises(ValueError):self.dl.safe_extract(z,self.root/'out')
