"""Recheck saved native receipts and usage without model calls.

Run this file from the committed measurement directory. The original oracle and
read-rate records are retained; this does not rerun fixture tests or release gates.
"""
from pathlib import Path
import collections
import gzip
import hashlib
import json
import sys
import tarfile
import tempfile

directory = Path(__file__).resolve().parent
repo = directory.parents[2]
sys.path.insert(0, str(repo/'evals'))
from phase_eval_context_reads import context_outputs

def digest(body):
    return hashlib.sha256(body).hexdigest()

def check(condition, *context):
    # assert would vanish under `python -O` and still print PASS.
    if not condition:
        raise SystemExit(f'FAIL {context}')

for attempt in sorted(directory.glob('attempt-*')):
    checksums = json.loads((attempt/'checksums.json').read_text(encoding='utf-8'))
    for name, expected in checksums.items():
        check(digest((attempt/name).read_bytes()) == expected, attempt.name, name)
    raw = gzip.decompress((attempt/'results.jsonl.gz').read_bytes())
    rows = [json.loads(line) for line in raw.splitlines()]
    audit = json.loads((attempt/'integrity-audit.json').read_text(encoding='utf-8'))
    meta = json.loads((attempt/'meta.json').read_text(encoding='utf-8'))
    check(digest(raw) == audit['results_sha256'], attempt.name, 'results_sha256')
    check(audit['status'] == 'PASS', attempt.name, 'integrity-audit')
    expected_units = {('author', kit, stack, mode, '', trial)
                      for kit in ['before','after'] for stack,mode in meta['selected_cases']
                      for trial in range(1,4)}
    observed_units = set()
    totals = collections.defaultdict(collections.Counter)
    with tarfile.open(attempt/'native-evidence.tar.gz') as archive:
        members = {member.name: archive.extractfile(member).read()
                   for member in archive if member.isfile()}
    referenced = set()
    for row in rows:
        identity = ('author',row['kit'],row['stack'],row['mode'],'',row['trial'])
        check(identity not in observed_units, attempt.name, identity)
        observed_units.add(identity)
        unit = hashlib.sha256(json.dumps(list(identity)).encode()).hexdigest()
        def read(name):
            path = f'evidence/{unit}/{name}'
            referenced.add(path)
            return members[path]
        receipt = json.loads(read('command.json'))
        check(receipt == {k:v for k,v in row['execution'].items() if k != 'evidence'},
              attempt.name, unit, 'receipt')
        stdout = read('stdout.raw')
        for body, key in [(read('prompt.txt'),'prompt_sha256'),
                          (stdout,'stdout_sha256'),(read('stderr.raw'),'stderr_sha256')]:
            check(digest(body) == receipt[key], attempt.name, unit, key)
        argv = receipt['argv']
        check(argv[argv.index('--model')+1] == 'gpt-6.1-sol', attempt.name, unit, 'model')
        check(argv[argv.index('--sandbox')+1] == 'workspace-write', attempt.name, unit, 'sandbox')
        check(receipt['returncode'] == row['rc'] == 0 and row['error'] is None,
              attempt.name, unit, 'returncode')
        events = [json.loads(line) for line in stdout.splitlines()]
        check(not any(e.get('type') in ['error','turn.failed'] for e in events),
              attempt.name, unit, 'provider error')
        terminals = [e['usage'] for e in events if e.get('type') == 'turn.completed']
        check(len(terminals) == 1, attempt.name, unit, 'terminal usage')
        usage = terminals[0]
        expected = dict(input=usage['input_tokens'],cached_input=usage['cached_input_tokens'],output=usage['output_tokens'])
        expected['uncached_input'] = expected['input']-expected['cached_input']
        check(expected == row['usage'], attempt.name, unit, 'usage')
        totals[row['kit']].update(expected)
        # Reparse actual command outputs with their matching installed catalog.
        with tempfile.TemporaryDirectory(prefix='af-context-receipt-') as temporary:
            tmp = Path(temporary)
            (tmp/'stdout.raw').write_bytes(stdout)
            (tmp/'catalog.json').write_bytes(read('catalog.json'))
            check(context_outputs(tmp/'stdout.raw', catalog=tmp/'catalog.json') == row['context_outputs'],
                  attempt.name, unit, 'context_outputs')
    check(len(rows) == 18 and observed_units == expected_units, attempt.name, 'units')
    check(referenced == set(members), attempt.name, 'Unreferenced or missing native evidence')
    print(json.dumps({'attempt':attempt.name,'status':'PASS','units':len(rows),
                      'usage':{key:dict(value) for key,value in totals.items()},
                      'scope':'Receipt/usage/output recheck; not a release gate'},ensure_ascii=False))
