"""Offline integration regression runner. Exit 1 means violated invariants."""
from pathlib import Path
import argparse
import json
import os
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
parser = argparse.ArgumentParser()
parser.add_argument('--source-root', type=Path, default=ROOT)
parser.add_argument('--output', type=Path, default=ROOT / 'audit' / 'regression')
parser.add_argument('--legacy', action='store_true', help='Replay the original 35-case audit against an original/rollback copy')
args = parser.parse_args()
source = args.source_root.resolve()
output = args.output.resolve()
output.mkdir(parents=True, exist_ok=True)
environment = {**os.environ, 'PYTHONIOENCODING': 'utf-8', 'REVIEW_ROOT': str(source), 'REVIEW_OUTPUT': str(output)}
tests = []
if args.legacy:
    originals = ROOT / 'audit' / '2026-09-29'
    for name in ('review_backend.py', 'review_frontend.cjs'):
        text = (originals / name).read_text(encoding='utf-8')
        if name.endswith('.py'):
            text = text.replace('HERE = Path(__file__).resolve().parent\nROOT = HERE.parents[1]',
                                "HERE = Path(os.environ['REVIEW_OUTPUT']).resolve()\nROOT = Path(os.environ['REVIEW_ROOT']).resolve()")
        else:
            text = text.replace("const HERE = __dirname;\nconst ROOT = path.resolve(HERE, '../..');",
                                "const HERE = process.env.REVIEW_OUTPUT;\nconst ROOT = process.env.REVIEW_ROOT;")
        copied = output / name
        copied.write_text(text, encoding='utf-8')
        tests.append(copied)
else:
    tests = [Path(__file__).with_name('review_backend.py'), Path(__file__).with_name('review_frontend.cjs')]

commands = [['python', '-u', tests[0].as_posix()], ['node', tests[1].as_posix()]]
executions = []
for command in commands:
    result = subprocess.run(command, cwd=source, env=environment, capture_output=True, text=True, encoding='utf-8')
    executions.append({'command': command, 'source_root': source.as_posix(), 'exit_status': result.returncode,
                       'stdout': result.stdout, 'stderr': result.stderr})
    print(result.stdout.rstrip().splitlines()[-1] if result.stdout else 'TEST_PROCESS_ERROR', flush=True)
    if 'ERROR ' in result.stdout or result.returncode not in (0, 1):
        print(result.stdout + result.stderr, flush=True)
rows = []
for name in ('backend_results.json', 'frontend_results.json'):
    path = output / name
    if not path.is_file():
        print('HARNESS_ERROR missing results')
        sys.exit(2)
    rows.extend(json.loads(path.read_text(encoding='utf-8')))
errors = [r for r in rows if 'harness_error' in r]
failed = sum(not r['passed'] for r in rows)
summary = {'total': len(rows), 'passed': len(rows) - failed, 'failed': failed, 'harness_errors': len(errors)}
(output / 'run_record.json').write_text(json.dumps({'summary': summary, 'executions': executions}, ensure_ascii=False, indent=2), encoding='utf-8')
print('REVIEW_SUMMARY total=%d passed=%d failed=%d' % (summary['total'], summary['passed'], failed), flush=True)
sys.exit(2 if errors else 1 if failed else 0)
