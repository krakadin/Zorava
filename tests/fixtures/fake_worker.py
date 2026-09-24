#!/usr/bin/python3
"""Tiny deterministic process used only by offline supervisor tests."""

import json
import os
from pathlib import Path
import subprocess
import sys
import time


mode = sys.argv[1]
payload = json.load(sys.stdin)
if mode == 'success':
    print(json.dumps({'text':'fake worker completed','reported_model':'fake-v1','usage':{'input_tokens':3,'output_tokens':4}}))
elif mode == 'malformed':
    print('not structured output')
elif mode == 'crash':
    print('401 authentication failed; Authorization: Bearer fake-crash-token-value', file=sys.stderr)
    raise SystemExit(17)
elif mode == 'sleep':
    time.sleep(60)
elif mode == 'gate':
    # Waits for the test-created gate file, then succeeds. Lets tests hold a
    # worker slot deterministically and release it on demand.
    gate = Path(os.environ['AI_TEST_GATE'])
    deadline = time.monotonic() + 30
    while not gate.exists():
        if time.monotonic() > deadline:
            raise SystemExit(4)
        time.sleep(0.02)
    print(json.dumps({'text':'fake worker completed','reported_model':'fake-v1'}))
elif mode == 'echo':
    # Echoes this job's own task so tests can verify per-job isolation.
    print(json.dumps({'text':f"job {payload['job_id']} task {payload['task']}",
                      'reported_model':'fake-v1'}))
elif mode == 'huge':
    sys.stdout.write('x'*200000)
elif mode == 'secret':
    print(json.dumps({'text':'Authorization: Bearer fake-output-token-value api_key="sk-test-FAKE-NOT-REAL-123456789"'}))
elif mode == 'env-names':
    print(json.dumps({'text':' '.join(sorted(os.environ))}))
elif mode == 'child':
    child = subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])
    Path(os.environ['AI_TEST_CHILD_PID']).write_text(str(child.pid))
    time.sleep(60)
else:
    raise SystemExit(3)
