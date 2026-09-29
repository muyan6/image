"""Offline checks: deployment validation must reject a healthy old process."""
import ast
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = Path(os.environ.get('REVIEW_OUTPUT', ROOT/'audit/deployment-tests')).resolve()
OUTPUT.mkdir(parents=True,exist_ok=True)
spec = importlib.util.spec_from_file_location('verify_revision',ROOT/'backend/tools/verify_revision.py')
gate = importlib.util.module_from_spec(spec);spec.loader.exec_module(gate)
NEW='a'*40;OLD='b'*40


class Reply:
    status=200
    def __init__(self,revision,ok=True):self.body=io.BytesIO(json.dumps({'ok':ok,'source_revision':revision}).encode())
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def read(self,*args):return self.body.read(*args)


class DeploymentTests(unittest.TestCase):
    def call(self,local=NEW,public=NEW):
        def fetch(base):return local if base.startswith('http://127.0.0.1:') else public
        with patch.object(gate,'revision_at',side_effect=fetch),\
             patch.object(sys,'argv',['verify_revision.py','--expected',NEW,'--public','https://example.invalid']):
            output=io.StringIO()
            with contextlib.redirect_stdout(output):status=gate.main()
        return status,output.getvalue().strip()

    def test_current_local_and_public_are_accepted(self):
        status,log=self.call();self.assertEqual(status,0);self.assertIn('DEPLOY_REVISION_OK',log)

    def test_healthy_old_local_process_is_rejected(self):
        status,log=self.call(local=OLD);self.assertEqual(status,1);self.assertIn('local process revision mismatch',log)

    def test_new_local_with_stale_public_proxy_is_rejected(self):
        status,log=self.call(public=OLD);self.assertEqual(status,1);self.assertIn('public process revision mismatch',log)

    def test_missing_revision_is_not_treated_as_healthy(self):
        for local,public in [('',NEW),(NEW,'')]:self.assertEqual(self.call(local,public)[0],1)

    def test_unreachable_endpoint_is_reported(self):
        with patch.object(gate,'revision_at',side_effect=TimeoutError):
            with patch.object(sys,'argv',['verify_revision.py','--expected',NEW]):
                output=io.StringIO()
                with contextlib.redirect_stdout(output):status=gate.main()
        self.assertEqual(status,1);self.assertIn('local revision check failed: TimeoutError',output.getvalue())

    def test_health_payload_requires_revision_and_ok(self):
        with patch.object(gate,'urlopen',return_value=Reply(NEW)):
            self.assertEqual(gate.revision_at('https://example.invalid'),NEW)
        with patch.object(gate,'urlopen',return_value=Reply('',False)):
            with self.assertRaises(ValueError):gate.revision_at('https://example.invalid')

    def test_revision_is_bound_at_process_start_and_not_copied_tree(self):
        tree=ast.parse((ROOT/'backend/main.py').read_text(encoding='utf-8-sig'))
        method=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='_source_revision')
        temp=OUTPUT/'copied-backend';temp.mkdir(exist_ok=True)
        namespace={'os':os,'subprocess':subprocess,'re':re,'BASE_DIR':str(temp)}
        exec(compile(ast.Module(body=[method],type_ignores=[]),'source_revision','exec'),namespace)
        self.assertEqual(namespace['_source_revision'](),'')
        source=(ROOT/'backend/main.py').read_text(encoding='utf-8')
        self.assertIn('SOURCE_REVISION = _source_revision()',source)
        self.assertIn('"source_revision": SOURCE_REVISION',source)

    def test_update_script_checks_revision_after_restart(self):
        bash=os.environ.get('BASH_PATH','C:/Program Files/Git/bin/bash.exe')
        r=subprocess.run([bash,'-n',str(ROOT/'.update')],capture_output=True,text=True)
        self.assertEqual(r.returncode,0,r.stderr)
        source=(ROOT/'.update').read_text(encoding='utf-8')
        self.assertIn('backend/tools/verify_revision.py',source)
        self.assertIn('EXPECTED_REVISION="$(git rev-parse HEAD)"',source)
        self.assertIn('--public "$PUBLIC_API_BASE"',source)

    def test_more_than_five_incoming_commits_do_not_abort_update(self):
        """With pipefail, `git log | head -5` exits 141 before git pull."""
        bash=os.environ.get('BASH_PATH','C:/Program Files/Git/bin/bash.exe')
        line=next(s for s in (ROOT/'.update').read_text(encoding='utf-8').splitlines()
                  if s.startswith('INCOMING='))
        # Use the actual checked-in history where 05fdc71..6745f9c has six commits.
        command=line.replace('HEAD..origin/$BRANCH','05fdc71..6745f9c')
        r=subprocess.run([bash,'-lc','set -euo pipefail; cd /f/Project/image; '
                          +command+'; printf UPDATE_LIST_OK'],capture_output=True,text=True)
        self.assertEqual(r.returncode,0,r.stdout+r.stderr)
        self.assertIn('UPDATE_LIST_OK',r.stdout)
        self.assertLessEqual(r.stdout.count('\n')+1,6)

    def test_systemd_probe_does_not_short_circuit_list_under_pipefail(self):
        source=(ROOT/'.update').read_text(encoding='utf-8')
        self.assertNotIn('systemctl list-unit-files 2>/dev/null | grep -q',source)
        self.assertIn('systemctl cat "${SERVICE_NAME}.service"',source)
        self.assertIn('if has_systemd_service; then',source)
        self.assertEqual(source.count('if has_systemd_service; then'),2)


if __name__=='__main__':
    suite=unittest.defaultTestLoader.loadTestsFromTestCase(DeploymentTests)
    names=[case._testMethodName for case in suite]
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    failures={case._testMethodName for case,_ in result.failures+result.errors}
    (OUTPUT/'deployment_results.json').write_text(json.dumps({'cases':[{'case':name,'passed':name not in failures} for name in names]},indent=2),encoding='utf-8')
    print('DEPLOYMENT_SUMMARY total=%d passed=%d failed=%d'%(result.testsRun,result.testsRun-len(failures),len(failures)))
    sys.exit(0 if result.wasSuccessful() else 1)
