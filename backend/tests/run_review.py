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
identity_test = Path(__file__).with_name('test_user_identity.py')
if not args.legacy and identity_test.exists():
    commands.append(['python', '-u', identity_test.as_posix()])
community_test = Path(__file__).with_name('test_community.py')
if not args.legacy and community_test.exists():
    commands.append(['python', '-u', community_test.as_posix()])
workflow_test = Path(__file__).with_name('test_workflow.py')
workflow_frontend = Path(__file__).with_name('test_workflow_frontend.cjs')
if not args.legacy and workflow_test.exists():
    commands.append(['python', '-u', workflow_test.as_posix()])
    commands.append(['node', workflow_frontend.as_posix()])
deployment_test = Path(__file__).with_name('test_deployment.py')
if not args.legacy and deployment_test.exists():
    commands.append(['python', '-u', deployment_test.as_posix()])
template_output_test = Path(__file__).with_name('test_template_output.py')
if not args.legacy and template_output_test.exists():
    commands.append(['python', '-u', template_output_test.as_posix()])
ci_test = Path(__file__).with_name('test_ci_performance.py')
if not args.legacy and ci_test.exists():
    commands.append(['python', '-u', ci_test.as_posix()])
text_test = Path(__file__).with_name('test_text_generation.py')
if not args.legacy and text_test.exists():
    commands.append(['python', '-u', text_test.as_posix()])
tier_test = Path(__file__).with_name('test_template_tiers.py')
if not args.legacy and tier_test.exists():
    commands.append(['python', '-u', tier_test.as_posix()])
executions = []
if not args.legacy:
    commands.extend([['python','-u',Path(__file__).with_name('test_cloud_pipeline.py').as_posix()],
                     ['python','-u',Path(__file__).with_name('test_cloud_origin.py').as_posix()],
                     ['node',Path(__file__).with_name('test_cloud_frontend.cjs').as_posix()]])
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
if not args.legacy and identity_test.exists():
    rows.extend(json.loads((output / 'identity_results.json').read_text(encoding='utf-8'))['cases'])
if not args.legacy and community_test.exists():
    rows.extend(json.loads((output / 'community_results.json').read_text(encoding='utf-8'))['cases'])
if not args.legacy and workflow_test.exists():
    for name in ('workflow_results.json', 'workflow_frontend_results.json'):
        rows.extend(json.loads((output / name).read_text(encoding='utf-8'))['cases'])
if not args.legacy and deployment_test.exists():
    rows.extend(json.loads((output / 'deployment_results.json').read_text(encoding='utf-8'))['cases'])
if not args.legacy and template_output_test.exists():
    rows.extend(json.loads((output / 'template_output_results.json').read_text(encoding='utf-8'))['cases'])
if not args.legacy and ci_test.exists():
    rows.extend(json.loads((output / 'ci_performance_results.json').read_text(encoding='utf-8'))['cases'])
if not args.legacy and text_test.exists():
    rows.extend(json.loads((output / 'text_generation_results.json').read_text(encoding='utf-8'))['cases'])
if not args.legacy and tier_test.exists():
    rows.extend(json.loads((output / 'template_tiers_results.json').read_text(encoding='utf-8'))['cases'])
errors = [r for r in rows if 'harness_error' in r]
if not args.legacy:
    for name in ('cloud_pipeline_results.json','cloud_origin_results.json','cloud_frontend_results.json'):
        rows.extend(json.loads((output/name).read_text(encoding='utf-8'))['cases'])
failed = sum(not r['passed'] for r in rows)
summary = {'total': len(rows), 'passed': len(rows) - failed, 'failed': failed, 'harness_errors': len(errors)}
(output / 'run_record.json').write_text(json.dumps({'summary': summary, 'executions': executions}, ensure_ascii=False, indent=2), encoding='utf-8')
print('REVIEW_SUMMARY total=%d passed=%d failed=%d' % (summary['total'], summary['passed'], failed), flush=True)
sys.exit(2 if errors else 1 if failed else 0)
