"""Verify a committed-source snapshot without including working-tree changes."""
import concurrent.futures
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
root = Path(__file__).resolve().parents[2]
evidence = Path(__file__).resolve().parent
commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()
tree = subprocess.check_output(['git', 'rev-parse', f'{commit}^{{tree}}'], cwd=root, text=True).strip()
snapshot = root / 'outputs' / f'status-2026-10-23-{commit[:7]}'
snapshot.mkdir(parents=True, exist_ok=False)
archive = subprocess.check_output(['git', 'archive', '--format=zip', commit], cwd=root)
with zipfile.ZipFile(io.BytesIO(archive)) as source:
    source.extractall(snapshot)
os.environ['PYTHONIOENCODING'] = 'utf-8'
os.environ['PYTHONUTF8'] = '1'

def run(name, command, cwd):
    log = evidence / f'{name}.txt'
    with log.open('w', encoding='utf-8') as out:
        result = subprocess.run(command, cwd=cwd, stdout=out, stderr=subprocess.STDOUT)
    content = log.read_text(encoding='utf-8', errors='replace')
    return {'name': name, 'command': command, 'exit_code': result.returncode,
            'log': str(log.relative_to(root)),
            'sha256': hashlib.sha256(log.read_bytes()).hexdigest(),
            'tail': content[-2200:]}

jobs = [
    ('backend', [sys.executable, '-m', 'pytest', '-q', '--tb=short'], snapshot),
    ('client', ['node', '--test', 'test/offline/*.test.ts',
                'test/device/*.test.ts', 'test/update/*.test.ts'], snapshot / 'android-app'),
    ('claims', [sys.executable, 'tools/verify_claims.py', '--check'], snapshot),
    ('ledgers', [sys.executable, 'tools/scan_all_ledgers.py'], snapshot),
]
with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
    results = list(pool.map(lambda job: run(*job), jobs))
record = {'source_commit': commit, 'source_tree': tree,
          'snapshot': str(snapshot), 'scope': 'git archive; excludes all uncommitted work',
          'results': results}
(evidence / 'verification.json').write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
for item in results:
    print(item['name'], item['exit_code'], item['tail'], flush=True)
