"""Offline static delivery/build/deployment tests; optional isolated real Nginx.

NGINX_TEST_BINARY enables a private loopback process, never an installed service.
All fixtures/configs live under REVIEW_OUTPUT and no application secrets are read.
"""
import contextlib
import gzip
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(os.environ.get('REVIEW_ROOT', Path(__file__).resolve().parents[2])).resolve()
OUTPUT = Path(os.environ.get('REVIEW_OUTPUT', ROOT/'audit/2026-10-02/load-optimization/static-tests')).resolve()
OUTPUT.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(ROOT/'backend'))
from fastapi import FastAPI
from fastapi.testclient import TestClient
from web_delivery import create_web_assets, accepts_gzip, web_home_path, favicon_path


def load_tool(name):
    spec = importlib.util.spec_from_file_location('static_test_'+name, ROOT/'backend/tools'/f'{name}.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


builder = load_tool('build_web_assets')
installer = load_tool('install_nginx_include')
observations = []


def public_fixture(base):
    source = base/'static/web'; source.mkdir(parents=True, exist_ok=True)
    files = {
        'boot.js': b"import './app.js';\n" + b'// fixture boot\n'*120,
        'app.js': b"import './shared-load.js';\nexport const fixture = true;\n" + b'// fixture app\n'*200,
        'shared-load.js': b'export const publicOnly = true;\n',
        'shell.css': b'body { color: #123; background: #fff; }\n'*120,
        'favicon.svg': b'<svg xmlns="http://www.w3.org/2000/svg"><path d="M0 0h10v10H0z"/></svg>',
    }
    for name, value in files.items(): (source/name).write_bytes(value)
    (base/'index.html').write_text('<!doctype html><link rel="icon" href="/favicon.svg"><link rel="stylesheet" href="/web-assets/shell.css"><script type="module" src="/web-assets/boot.js"></script>', encoding='utf-8')
    return source, files


SITE = '''# fixture TLS/API configuration
server { listen 80; server_name fixture.invalid; return 301 https://$host$request_uri; }
server # quoted braces and comments must not shift offsets
{
    listen 443 ssl;
    server_name "fixture.invalid";
    ssl_certificate /fixture/tls/fullchain.pem;
    ssl_certificate_key /fixture/tls/privkey.pem;
    # server_name ignored.invalid; listen 443; }
    location /api/ { proxy_pass http://127.0.0.1:8000; proxy_cache off; }
    location /admin { proxy_pass http://127.0.0.1:8000; }
    set $fixture "quoted { # literal }";
}
'''


class StaticTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='static_', dir=OUTPUT)
        self.base = Path(self.tmp.name).resolve()
        self.source, self.files = public_fixture(self.base)
        self.output = self.base/'static/web-dist'
        self.manifest = builder.build(self.output, self.source, self.base/'index.html')
        app = FastAPI(); app.mount('/web-assets', create_web_assets(self.base))
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        assert self.base.is_relative_to(OUTPUT)
        self.tmp.cleanup()

    def version_url(self, name='app.js'):
        return '/web-assets/'+self.manifest['version']+'/'+name

    def test_asgi_allowlist_for_public_types_and_private_rejection(self):
        (self.source/'.env').write_text('FIXTURE_SECRET', encoding='utf-8')
        (self.source/'main.py').write_text('FIXTURE_CODE', encoding='utf-8')
        (self.source/'data').mkdir(); (self.source/'data/settings.json').write_text('{}', encoding='utf-8')
        for name in self.files:
            response = self.client.get('/web-assets/'+name, headers={'Accept-Encoding':'identity'})
            self.assertEqual(response.status_code, 200); self.assertEqual(response.content, self.files[name])
        for path in ('.env','main.py','data/settings.json','../index.html','%2e%2e%2findex.html','app.js.gz',
                     'app.js/map','not-a-version/app.js','00000000000000000/app.js','0000000000000000/main.py'):
            response = self.client.get('/web-assets/'+path, headers={'Accept-Encoding':'identity'})
            self.assertEqual(response.status_code, 404, path)
        self.assertEqual(self.client.post('/web-assets/app.js', content=b'fixture').status_code,405)

    def test_asgi_mime_head_versioned_cache_and_legacy_cache(self):
        for name, mime in (('app.js','text/javascript'), ('shell.css','text/css'), ('favicon.svg','image/svg+xml')):
            response = self.client.get(self.version_url(name), headers={'Accept-Encoding':'identity'})
            self.assertEqual(response.status_code,200)
            self.assertIn(mime,response.headers['content-type'])
            self.assertIn('max-age=31536000',response.headers['cache-control'])
            self.assertIn('immutable',response.headers['cache-control'])
            self.assertEqual(response.headers['x-content-type-options'],'nosniff')
            head = self.client.head(self.version_url(name),headers={'Accept-Encoding':'identity'})
            self.assertEqual(head.status_code,200); self.assertEqual(head.content,b'')
            self.assertEqual(int(head.headers['content-length']),len(self.files[name]))
        legacy = self.client.get('/web-assets/app.js',headers={'Accept-Encoding':'identity'})
        self.assertIn('must-revalidate',legacy.headers['cache-control'])
        self.assertNotIn('immutable',legacy.headers['cache-control'])

    def test_gzip_quality_negotiation_and_representation_etags(self):
        expected = {'':False,'identity':False,'gzip':True,'br, gzip;q=0.5':True,'gzip;q=0':False,
                    '*;q=0.6':True,'gzip;q=0,*;q=1':False,'gzip;q=bad':False,'gzip;q=0.000':False,
                    'gzip;q=NaN':False,'gzip;q=Infinity':False,'gzip;q=2':False}
        for value, accepted in expected.items(): self.assertEqual(accepts_gzip(value),accepted,value)
        url = self.version_url()
        plain = self.client.get(url,headers={'Accept-Encoding':'identity'})
        zipped = self.client.get(url,headers={'Accept-Encoding':'gzip'})
        self.assertEqual(plain.status_code,200); self.assertEqual(zipped.status_code,200)
        self.assertEqual(zipped.content,plain.content)
        self.assertEqual(zipped.headers['content-encoding'],'gzip')
        self.assertEqual(int(zipped.headers['content-length']),len((self.output/'web-assets'/self.manifest['version']/'app.js.gz').read_bytes()))
        self.assertNotEqual(zipped.headers['etag'],plain.headers['etag'])
        denied = self.client.get(url,headers={'Accept-Encoding':'gzip;q=0,*;q=1'})
        self.assertNotIn('content-encoding',denied.headers)
        observations.append({'case':'ASGI precompressed asset','raw_bytes':len(plain.content),
                             'gzip_bytes':int(zipped.headers['content-length']), 'quality_zero_respected':True})

    def test_conditional_304_etag_modified_since_and_gzip(self):
        for encoding in ('identity','gzip'):
            first = self.client.get(self.version_url(),headers={'Accept-Encoding':encoding})
            self.assertEqual(first.status_code,200)
            for header, value in (('If-None-Match',first.headers['etag']), ('If-Modified-Since',first.headers['last-modified'])):
                cached = self.client.get(self.version_url(),headers={'Accept-Encoding':encoding,header:value})
                self.assertEqual(cached.status_code,304); self.assertEqual(cached.content,b'')
                self.assertEqual(cached.headers['cache-control'],first.headers['cache-control'])
                self.assertIn('Accept-Encoding',cached.headers['vary'])

    def test_asgi_raw_and_gzip_resolved_paths_never_escape_public_root(self):
        outside = self.base/'fixture-secret.txt'; outside.write_bytes(b'FIXTURE_SECRET')
        secret_gzip = self.base/'fixture-secret.gz'; secret_gzip.write_bytes(gzip.compress(b'FIXTURE_SECRET',mtime=0))
        for name, encoding, target in (('escape.js','identity',outside), ('app.js.gz','gzip',secret_gzip)):
            file = self.source/name
            if file.exists(): file.unlink()
            try:
                file.symlink_to(target); real_link = True
            except OSError:
                file.write_bytes(target.read_bytes()); real_link = False
            requested = '/web-assets/escape.js' if name=='escape.js' else '/web-assets/app.js'
            if real_link:
                response = self.client.get(requested,headers={'Accept-Encoding':encoding})
            else:
                original = Path.resolve
                def resolve(path, *args, **kwargs):
                    return target.resolve() if path == file else original(path,*args,**kwargs)
                with patch.object(Path,'resolve',autospec=True,side_effect=resolve):
                    response = self.client.get(requested,headers={'Accept-Encoding':encoding})
            self.assertEqual(response.status_code,404,name)
            observations.append({'case':'ASGI resolved-path guard '+name,'real_symlink_available':real_link,'status':404})

    def test_home_path_prefers_built_and_falls_back_without_export(self):
        self.assertEqual(Path(web_home_path(self.base)),self.output/'index.html')
        (self.output/'index.html').unlink()
        self.assertEqual(Path(web_home_path(self.base)),self.base/'index.html')
        self.assertEqual(Path(favicon_path(self.base)),self.source/'favicon.svg')

    def test_builder_deterministic_content_hash_relative_imports_and_gzip(self):
        second = self.base/'second-build'; repeated = builder.build(second,self.source,self.base/'index.html')
        self.assertEqual(self.manifest,repeated)
        digest = hashlib.sha256()
        for name in sorted(self.files): digest.update(name.encode()+b'\0'+self.files[name]+b'\0')
        self.assertEqual(self.manifest['version'],digest.hexdigest()[:16])
        for entry in self.manifest['files']:
            name = entry['name']; first = self.output/'web-assets'/self.manifest['version']/name
            duplicate = second/'web-assets'/repeated['version']/name
            self.assertEqual(first.read_bytes(),duplicate.read_bytes())
            self.assertEqual(first.with_name(name+'.gz').read_bytes(),duplicate.with_name(name+'.gz').read_bytes())
            self.assertEqual(gzip.decompress(first.with_name(name+'.gz').read_bytes()),self.files[name])
            self.assertEqual(entry['sha256'],hashlib.sha256(self.files[name]).hexdigest())
        html = (self.output/'index.html').read_text(encoding='utf-8')
        self.assertIn('/web-assets/'+self.manifest['version']+'/boot.js',html)
        self.assertIn('/web-assets/'+self.manifest['version']+'/favicon.svg',html)
        for name in ('boot.js','app.js'):
            text = (self.output/'web-assets'/self.manifest['version']/name).read_text(encoding='utf-8')
            for dependency in re.findall(r"['\"]\./([^'\"]+)['\"]",text):
                self.assertTrue((self.output/'web-assets'/self.manifest['version']/dependency).is_file())
        self.assertEqual(gzip.decompress((self.output/'index.html.gz').read_bytes()).decode(),html)

    def test_builder_content_change_creates_new_release_and_preserves_old(self):
        old = self.manifest['version']; (self.source/'shared-load.js').write_bytes(b'export const publicOnly = false;\n')
        fresh = builder.build(self.output,self.source,self.base/'index.html')
        self.assertNotEqual(fresh['version'],old)
        self.assertTrue((self.output/'web-assets'/old/'app.js').is_file())
        self.assertEqual((self.output/'web-assets'/old/'shared-load.js').read_bytes(),self.files['shared-load.js'])

    def test_builder_never_exports_env_python_data_or_outside_symlink(self):
        (self.source/'.env').write_text('FIXTURE_SECRET',encoding='utf-8')
        (self.source/'main.py').write_text('FIXTURE_CODE',encoding='utf-8')
        (self.source/'.hidden.js').write_text('FIXTURE_HIDDEN',encoding='utf-8')
        (self.source/'data').mkdir(); (self.source/'data/settings.json').write_text('{}',encoding='utf-8')
        outside = self.base/'fixture-private.env'; outside.write_text('FIXTURE_SECRET',encoding='utf-8')
        link = self.source/'leak.js'
        try: link.symlink_to(outside); real_link = True
        except OSError: link.write_text('FIXTURE_SECRET',encoding='utf-8'); real_link = False
        export = self.base/'clean-export'
        if real_link: result = builder.build(export,self.source,self.base/'index.html')
        else:
            original = Path.resolve
            def resolve(path,*args,**kwargs): return outside.resolve() if path==link else original(path,*args,**kwargs)
            with patch.object(Path,'resolve',autospec=True,side_effect=resolve): result = builder.build(export,self.source,self.base/'index.html')
        self.assertEqual({row['name'] for row in result['files']},set(self.files))
        self.assertFalse(any(path.name in ('.env','main.py','.hidden.js','settings.json','leak.js') for path in export.rglob('*')))
        self.assertFalse(any(b'FIXTURE_SECRET' in path.read_bytes() for path in export.rglob('*') if path.is_file()))

    def test_builder_detects_immutable_collision_and_invalid_nginx_inputs(self):
        target = self.output/'web-assets'/self.manifest['version']/'app.js'; target.write_bytes(b'FIXTURE_COLLISION')
        with self.assertRaisesRegex(ValueError,'collision'): builder.build(self.output,self.source,self.base/'index.html')
        for root in ('/fixture/"bad','/fixture/$bad','/fixture/;bad','/fixture/\nbad'):
            with self.assertRaises(ValueError): builder.nginx_config(root)
        for port in (0,65536):
            with self.assertRaises(ValueError): builder.nginx_config('/fixture',port)
        dynamic = builder.nginx_config('/fixture',gzip_static=False)
        self.assertNotIn('gzip_static on;',dynamic); self.assertIn('gzip on;',dynamic)

    def test_docker_app_layout_builds_independently_of_repository_parent(self):
        app = self.base/'app'; source,_ = public_fixture(app); (app/'tools').mkdir()
        shutil.copy2(ROOT/'backend/tools/build_web_assets.py',app/'tools/build_web_assets.py')
        result = subprocess.run([sys.executable,str(app/'tools/build_web_assets.py')],cwd=self.base,capture_output=True,text=True,encoding='utf-8')
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertIn('WEB_ASSETS_BUILT',result.stdout); self.assertTrue((app/'static/web-dist/index.html').is_file())
        for path in (ROOT/'Dockerfile',ROOT/'backend/Dockerfile'):
            text = path.read_text(encoding='utf-8')
            self.assertIn('WORKDIR /app',text); self.assertIn('RUN python tools/build_web_assets.py',text)
            self.assertIn('./tools/build_web_assets.py',text)
        deploy = ROOT/'deploy/install-nginx-static.sh'; script = deploy.read_text(encoding='utf-8')
        self.assertIn('--bt) BT=1',script)
        self.assertIn('SITE="/www/server/panel/vhost/nginx/$DOMAIN.conf"',script)
        self.assertIn('/www/server/nginx/sbin/nginx',script)
        self.assertIn('--site "$SITE" --domain "$DOMAIN"',script)
        self.assertIn('--public-root "$PUBLIC_ROOT"',script); self.assertIn('--port "$PORT"',script)
        bash = os.environ.get('BASH_PATH','C:/Program Files/Git/bin/bash.exe' if os.name=='nt' else 'bash')
        checked = subprocess.run([bash,'-n',str(deploy)],capture_output=True,text=True)
        self.assertEqual(checked.returncode,0,checked.stdout+checked.stderr)
        observations.append({'case':'Docker /app layout and BaoTa installer','isolated_builder_exit':result.returncode,
                             'deploy_shell_syntax_exit':checked.returncode,'baota_site_and_binary_parameters_verified':True})

    def test_include_preserves_tls_api_other_server_and_is_idempotent(self):
        changed = installer.insert(SITE,'fixture.invalid','/fixture/public/nginx-web.conf')
        for directive in ('ssl_certificate /fixture/tls/fullchain.pem;', 'ssl_certificate_key /fixture/tls/privkey.pem;',
                          'location /api/ { proxy_pass http://127.0.0.1:8000; proxy_cache off; }',
                          'location /admin { proxy_pass http://127.0.0.1:8000; }',
                          'server { listen 80; server_name fixture.invalid; return 301 https://$host$request_uri; }'):
            self.assertIn(directive,changed)
        self.assertEqual(changed.count('include "/fixture/public/nginx-web.conf";'),1)
        self.assertEqual(installer.insert(changed,'fixture.invalid','/fixture/public/nginx-web.conf'),changed)
        self.assertIn('quoted { # literal }',changed)

    def test_include_comments_and_ambiguous_or_incomplete_sites_are_rejected(self):
        commented = 'server { listen 443 ssl; # server_name fixture.invalid;\n server_name other.invalid; }'
        with self.assertRaises(ValueError): installer.insert(commented,'fixture.invalid','/fixture/public.conf')
        for text in (SITE+SITE, 'server { listen 443; server_name fixture.invalid;', 'server { set $x "unterminated; }'):
            with self.assertRaises(ValueError): installer.insert(text,'fixture.invalid','/fixture/public.conf')

    def install_call(self, replies, dry_run=False):
        site = self.base/'site.conf'; site.write_text(SITE,encoding='utf-8')
        include = self.base/'nginx-web.conf'; include.write_text(builder.nginx_config(self.output),encoding='utf-8')
        args = ['install_nginx_include.py','--site',str(site),'--include',str(include),'--domain','fixture.invalid','--nginx','fixture-nginx']
        if dry_run: args.append('--dry-run')
        log = io.StringIO()
        with patch.object(sys,'argv',args), patch.object(installer.subprocess,'run',side_effect=replies) as run, contextlib.redirect_stdout(log):
            status = installer.main()
        return site,status,log.getvalue(),run

    def test_include_dry_run_does_not_write_or_reload(self):
        site,status,log,run = self.install_call([],True)
        self.assertEqual(status,0); self.assertEqual(site.read_text(encoding='utf-8'),SITE); run.assert_not_called()
        self.assertIn('photo-rescue-managed-static',log)

    def test_include_success_runs_validation_before_reload_and_keeps_backup(self):
        reply = subprocess.CompletedProcess([],0,'FIXTURE_OK','')
        site,status,log,run = self.install_call([reply,reply])
        self.assertEqual(status,0); self.assertIn('NGINX_STATIC_ENABLED',log)
        self.assertEqual(run.call_args_list[0].args[0],['fixture-nginx','-t'])
        self.assertEqual(run.call_args_list[1].args[0],['fixture-nginx','-s','reload'])
        self.assertNotEqual(site.read_text(encoding='utf-8'),SITE)
        backups = list(self.base.glob('site.conf.photo-static-backup-*')); self.assertEqual(len(backups),1)
        self.assertEqual(backups[0].read_text(encoding='utf-8'),SITE)

    def test_include_validation_and_reload_failure_restore_exact_original(self):
        good = subprocess.CompletedProcess([],0,'FIXTURE_OK','')
        bad = subprocess.CompletedProcess([],1,'','FIXTURE_NGINX_FAILURE')
        for sequence in ([bad],[good,bad]):
            with self.assertRaisesRegex(RuntimeError,'FIXTURE_NGINX_FAILURE'): self.install_call(sequence)
            self.assertEqual((self.base/'site.conf').read_text(encoding='utf-8'),SITE)
        self.assertEqual(len(list(self.base.glob('site.conf.photo-static-backup-*'))),2)


class NginxStaticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        binary = os.environ.get('NGINX_TEST_BINARY')
        if not binary: raise unittest.SkipTest('NGINX_TEST_BINARY not set; isolated Nginx integration is optional')
        cls.binary = str(Path(binary).resolve(strict=True))
        cls.tmp = tempfile.TemporaryDirectory(prefix='nginx_static_',dir=OUTPUT)
        cls.base = Path(cls.tmp.name).resolve(); cls.source,cls.files = public_fixture(cls.base)
        cls.public = cls.base/'public'; cls.manifest = builder.build(cls.public,cls.source,cls.base/'index.html')
        cls.upstream_hits = []
        class Upstream(BaseHTTPRequestHandler):
            def do_GET(self): self.reply()
            def do_POST(self): self.reply()
            def reply(self):
                size = int(self.headers.get('Content-Length','0')); body = self.rfile.read(size)
                cls.upstream_hits.append((self.command,self.path,body))
                data = json.dumps({'fixture_proxy':True,'method':self.command,'path':self.path}).encode()
                self.send_response(200); self.send_header('Content-Type','application/json')
                self.send_header('Cache-Control','private, no-store'); self.send_header('Content-Length',str(len(data)))
                self.end_headers(); self.wfile.write(data)
            def log_message(self,*args): pass
        cls.upstream = ThreadingHTTPServer(('127.0.0.1',0),Upstream)
        cls.thread = threading.Thread(target=cls.upstream.serve_forever,daemon=True); cls.thread.start()
        with socket.socket() as reservation:
            reservation.bind(('127.0.0.1',0)); cls.port = reservation.getsockname()[1]
        for name in ('conf','logs','temp/client_body_temp','temp/proxy_temp','temp/fastcgi_temp','temp/uwsgi_temp','temp/scgi_temp'):
            (cls.base/name).mkdir(parents=True,exist_ok=True)
        include = cls.base/'conf/public.conf'
        include.write_text(builder.nginx_config(cls.public,cls.upstream.server_port),encoding='utf-8')
        config = '''daemon off;
master_process off;
pid logs/nginx.pid;
error_log logs/error.log;
events { worker_connections 64; }
http {
    access_log logs/access.log;
    server {
        listen 127.0.0.1:__PORT__;
        server_name fixture.invalid;
        include "__INCLUDE__";
        location /api/ { proxy_pass http://127.0.0.1:__UPSTREAM__; proxy_cache off; }
        location /admin { proxy_pass http://127.0.0.1:__UPSTREAM__; proxy_cache off; }
        location / { proxy_pass http://127.0.0.1:__UPSTREAM__; proxy_cache off; }
    }
}
'''.replace('__PORT__',str(cls.port)).replace('__UPSTREAM__',str(cls.upstream.server_port)).replace('__INCLUDE__',str(include).replace('\\','/'))
        (cls.base/'conf/nginx.conf').write_text(config,encoding='utf-8')
        cls.arguments = [cls.binary,'-p',str(cls.base).replace('\\','/')+'/','-c','conf/nginx.conf']
        cls.creation = subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0
        try:
            result = subprocess.run(cls.arguments+['-t'],capture_output=True,text=True,creationflags=cls.creation,timeout=15)
            if result.returncode: raise RuntimeError('FIXTURE_NGINX_CONFIG_FAILED: '+result.stdout+result.stderr)
            cls.test_output = result.stdout+result.stderr
            cls.process = subprocess.Popen(cls.arguments,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                                           creationflags=cls.creation,cwd=cls.base)
            until = time.monotonic()+10
            while True:
                if cls.process.poll() is not None: raise RuntimeError((cls.base/'logs/error.log').read_text(encoding='utf-8',errors='replace'))
                try:
                    with urlopen('http://127.0.0.1:'+str(cls.port)+'/',timeout=1) as reply:
                        if reply.status==200: break
                except (URLError,TimeoutError): pass
                if time.monotonic()>until: raise RuntimeError('FIXTURE_NGINX_START_TIMEOUT')
                time.sleep(.05)
        except Exception:
            cls.stop(); raise
        observations.append({'case':'real isolated Nginx syntax','binary':Path(cls.binary).name,'config_test_exit':result.returncode,
                             'syntax_output':cls.test_output.strip(),'loopback_only':True})

    @classmethod
    def stop(cls):
        process = getattr(cls,'process',None)
        if process is not None and process.poll() is None:
            try:
                subprocess.run(cls.arguments+['-s','quit'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                               creationflags=cls.creation,timeout=3)
            except (OSError,subprocess.TimeoutExpired): pass
            try: process.wait(timeout=2)
            except subprocess.TimeoutExpired: process.terminate(); process.wait(timeout=5)
            observations.append({'case':'isolated Nginx cleanup','process_stopped':process.poll() is not None})
        if hasattr(cls,'upstream'): cls.upstream.shutdown(); cls.upstream.server_close()
        if hasattr(cls,'thread'): cls.thread.join(timeout=3)
        if hasattr(cls,'tmp'):
            assert cls.base.is_relative_to(OUTPUT)
            cls.tmp.cleanup()

    @classmethod
    def tearDownClass(cls): cls.stop()

    def fetch(self,path,headers=None,method='GET',data=None):
        request = Request('http://127.0.0.1:'+str(self.port)+path,headers=headers or {},method=method,data=data)
        try:
            with urlopen(request,timeout=5) as response: return response.status,dict(response.headers),response.read()
        except HTTPError as error: return error.code,dict(error.headers),error.read()

    def version_url(self,name='app.js'): return '/web-assets/'+self.manifest['version']+'/'+name

    def test_real_nginx_static_gzip_quality_mime_head_and_cache(self):
        before = len(self.upstream_hits)
        status,headers,data = self.fetch(self.version_url(),{'Accept-Encoding':'gzip'})
        self.assertEqual(status,200); self.assertEqual(headers['Content-Encoding'],'gzip')
        self.assertEqual(gzip.decompress(data),self.files['app.js'])
        self.assertIn('immutable',headers['Cache-Control']); self.assertIn('text/javascript',headers['Content-Type'])
        self.assertEqual(headers['X-Static-Delivery'],'nginx')
        status,plain,raw = self.fetch(self.version_url(),{'Accept-Encoding':'gzip;q=0'})
        self.assertEqual(status,200); self.assertNotIn('Content-Encoding',plain); self.assertEqual(raw,self.files['app.js'])
        status,head,body = self.fetch(self.version_url('shell.css'),method='HEAD')
        self.assertEqual(status,200); self.assertEqual(body,b''); self.assertIn('text/css',head['Content-Type'])
        status,legacy,_ = self.fetch('/web-assets/app.js')
        self.assertEqual(status,200); self.assertNotIn('immutable',legacy['Cache-Control'])
        self.assertEqual(len(self.upstream_hits),before)
        observations.append({'case':'real Nginx static gzip','raw_bytes':len(raw),'gzip_bytes':len(data),'backend_requests':0})

    def test_real_nginx_304_favicon_and_nonpublic_allowlist(self):
        status,headers,_ = self.fetch(self.version_url())
        status,cached,body = self.fetch(self.version_url(),{'If-None-Match':headers['ETag']})
        self.assertEqual(status,304); self.assertEqual(body,b'')
        self.assertIn('immutable',cached['Cache-Control'])
        status,favicon,data = self.fetch('/favicon.svg')
        self.assertEqual(status,200); self.assertIn('image/svg+xml',favicon['Content-Type'])
        for name in ('.env','secret.py','data.json'):
            (self.public/'web-assets'/name).write_text('FIXTURE_PRIVATE',encoding='utf-8')
            status,_,data = self.fetch('/web-assets/'+name)
            self.assertEqual(status,404,name); self.assertNotIn(b'FIXTURE_PRIVATE',data)
        status,_,_ = self.fetch(self.version_url(),method='POST',data=b'fixture')
        self.assertEqual(status,403)

    def test_real_nginx_root_html_bypasses_backend_but_wechat_get_post_proxy(self):
        before = len(self.upstream_hits)
        status,headers,data = self.fetch('/')
        self.assertEqual(status,200); self.assertIn('text/html',headers['Content-Type'])
        self.assertIn(b'<!doctype html>',data); self.assertEqual(len(self.upstream_hits),before)
        status,headers,data = self.fetch('/?echostr=fixture_echo&signature=fixture_sig')
        self.assertEqual(status,200); self.assertTrue(json.loads(data)['fixture_proxy'])
        self.assertEqual(json.loads(data)['path'],'/?echostr=fixture_echo&signature=fixture_sig')
        self.assertIn('no-store',headers['Cache-Control'])
        status,headers,data = self.fetch('/',method='POST',data=b'FIXTURE_WECHAT_MESSAGE')
        self.assertEqual(status,200); self.assertEqual(json.loads(data)['method'],'POST')
        self.assertEqual(self.upstream_hits[-1][2],b'FIXTURE_WECHAT_MESSAGE')

    def test_real_nginx_api_and_admin_never_hit_public_directory(self):
        (self.public/'api').mkdir(); (self.public/'api/me').write_text('FIXTURE_WRONG_CACHE',encoding='utf-8')
        for path in ('/api/me','/api/my/jobs','/admin'):
            status,headers,data = self.fetch(path)
            self.assertEqual(status,200); self.assertTrue(json.loads(data)['fixture_proxy'])
            self.assertNotIn(b'FIXTURE_WRONG_CACHE',data); self.assertIn('no-store',headers['Cache-Control'])
            self.assertNotIn('X-Static-Delivery',headers)


if __name__ == '__main__':
    suite = unittest.TestSuite([unittest.defaultTestLoader.loadTestsFromTestCase(StaticTests),
                              unittest.defaultTestLoader.loadTestsFromTestCase(NginxStaticTests)])
    cases = [(case.__class__.__name__,case._testMethodName) for group in suite for case in group]
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    def affected(test):
        name = getattr(test,'_testMethodName',None)
        if name: return [name]
        return [name for klass,name in cases
                if re.search(r'(?<![A-Za-z0-9_])'+re.escape(klass)+r'(?![A-Za-z0-9_])',str(test))]
    failed = {name for test,_ in result.failures+result.errors for name in affected(test)}
    skipped = {name:reason for test,reason in result.skipped for name in affected(test)}
    executed = [name for _,name in cases if name not in skipped]
    (OUTPUT/'load_static_results.json').write_text(json.dumps({'cases':[{'case':name,'passed':name not in failed}
        for name in executed], 'skipped':skipped, 'observations':observations},ensure_ascii=False,indent=2),encoding='utf-8')
    print('LOAD_STATIC_SUMMARY total=%d passed=%d failed=%d skipped=%d'%(len(executed),
        len(executed)-len(failed),len(failed),len(skipped)))
    raise SystemExit(0 if result.wasSuccessful() else 1)
