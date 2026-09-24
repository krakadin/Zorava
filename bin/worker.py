#!/usr/bin/python3
"""Command-line interface for the local AI worker supervisor."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import sys
import uuid

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from ai_router.request import MAX_REQUEST_BYTES, MAX_TASK_BYTES, RequestError
from ai_router.landlock import LandlockUnavailable, landlock_abi
from ai_router.state import StateStore
from ai_router.supervisor import Supervisor
from workers.kimi import KimiAdapter


RUNTIME = Path('/home/krakadin/.local/state/ai-workers')
ALLOWED_ROOTS = (Path('/home/krakadin/myDev'),)
EXIT_CODES = {
    'INVALID_REQUEST': 2, 'INVALID_WORKER': 2, 'INVALID_MODE': 2,
    'INVALID_TASK': 2, 'INVALID_TIMEOUT': 2, 'REQUEST_TOO_LARGE': 2,
    'PATH_NOT_ALLOWED': 2, 'PATH_ACCESS_ERROR': 2,
    'CONFIG_ERROR': 3, 'AUTH_ERROR': 4, 'WORKER_CRASH': 5,
    'WORKER_BUSY': 5, 'INVALID_OUTPUT': 5, 'OUTPUT_LIMIT': 5,
    'MODEL_UNAVAILABLE': 5, 'RATE_LIMITED': 5, 'QUOTA_OR_BILLING': 5,
    'NETWORK_ERROR': 5, 'TIMEOUT': 6, 'CANCELLED': 7,
    'SANDBOX_UNAVAILABLE': 3, 'PROJECT_DIRTY': 2,
    'NOT_A_GIT_REPOSITORY': 2, 'GIT_ERROR': 3, 'GIT_WORKTREE_ERROR': 3,
}


def build_supervisor() -> tuple[Supervisor, KimiAdapter]:
    runtime_tmp = RUNTIME / 'tmp'
    adapter = KimiAdapter(runtime_tmp=runtime_tmp)
    return Supervisor(RUNTIME, ALLOWED_ROOTS, {'kimi': adapter}), adapter


def emit_json(value: dict, exit_code: int = 0) -> int:
    sys.stdout.write(json.dumps(value, ensure_ascii=False, separators=(',', ':')) + '\n')
    return exit_code


def error_result(code: str, message: str) -> dict:
    return {'schema_version': 1, 'status': 'failed',
            'error': {'code': code, 'message': message}}


def read_stdin(limit: int) -> bytes:
    data = sys.stdin.buffer.read(limit + 1)
    if len(data) > limit:
        raise RequestError('REQUEST_TOO_LARGE', 'Standard input exceeds the size limit.')
    return data


def run_request(raw: bytes, *, job_type='delegation') -> tuple[dict, int]:
    try:
        supervisor, _ = build_supervisor()
        result = supervisor.run(raw, job_type=job_type)
    except RequestError as exc:
        return error_result(exc.code, str(exc)), EXIT_CODES.get(exc.code, 2)
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        # Configuration and process details are deliberately not returned.
        return error_result('CONFIG_ERROR', str(exc)[:240]), 3
    value = result.json()
    code = result.error.get('code') if result.error else None
    return value, EXIT_CODES.get(code, 0)


def command_delegate(args) -> int:
    try:
        task_bytes = read_stdin(MAX_TASK_BYTES)
        task = task_bytes.decode('utf-8')
        if not task.strip():
            raise RequestError('INVALID_TASK', 'Task input is empty.')
        request = {'worker': args.worker, 'task': task, 'cwd': args.cwd,
                   'mode': args.mode, 'timeout_seconds': args.timeout}
        value, code = run_request(json.dumps(request, ensure_ascii=False).encode('utf-8'))
    except (UnicodeDecodeError, RequestError) as exc:
        code_name = exc.code if isinstance(exc, RequestError) else 'INVALID_TASK'
        value, code = error_result(code_name, str(exc)), EXIT_CODES.get(code_name, 2)
    return emit_json(value, code) if args.json else print_human(value, code)


def command_run(args) -> int:
    try:
        raw = read_stdin(MAX_REQUEST_BYTES)
        value, code = run_request(raw)
    except RequestError as exc:
        value, code = error_result(exc.code, str(exc)), EXIT_CODES.get(exc.code, 2)
    return emit_json(value, code) if args.json else print_human(value, code)


def command_test(args) -> int:
    task = f'Reply with exactly {args.worker.upper()}_WORKER_OK. Do not inspect files or call tools.'
    request = {'worker': args.worker, 'task': task, 'cwd': str(PROJECT),
               'mode': 'read-only', 'timeout_seconds': 120}
    value, code = run_request(json.dumps(request).encode('utf-8'), job_type='provider_test')
    expected = f'{args.worker.upper()}_WORKER_OK'
    if value.get('status') == 'completed':
        received = (value.get('result') or '').strip()
        value['expected_marker'] = expected
        value['marker_received'] = received == expected
        if received != expected:
            value['error'] = {'code': 'SMOKE_TEST_MISMATCH',
                              'message': 'Worker response did not match the expected marker.'}
            code = 5
    return emit_json(value, code) if args.json else print_human(value, code)


def print_human(value: dict, exit_code: int = 0) -> int:
    if value.get('job_id'):
        print(f"Kimi job {value.get('status', 'unknown')} ({value['job_id']})")
        print(f"Model requested: {value.get('requested_model', KimiAdapter.requested_model)}")
        print(f"CLI version: {value.get('worker_version', 'unknown')}")
        if value.get('duration_ms') is not None:
            print(f"Duration: {value['duration_ms']} ms")
        if value.get('result'):
            print(value['result'])
        if value.get('workspace'):
            print(f"Isolated worktree: {value['workspace'].get('path','unknown')}")
            print(f"Changed files: {value['workspace'].get('change_count',0)}")
            if value.get('diff'):
                print(value['diff'])
    if value.get('error'):
        print(f"{value['error'].get('code')}: {value['error'].get('message')}", file=sys.stderr)
    elif isinstance(value.get('result'), str):
        print(value['result'])
    elif 'jobs' in value:
        print(json.dumps(value['jobs'], ensure_ascii=False, indent=2))
    elif 'job' in value:
        print(json.dumps(value['job'], ensure_ascii=False, indent=2))
    return exit_code


def command_preflight(args) -> int:
    try:
        adapter = KimiAdapter(RUNTIME / 'tmp')
        version = adapter.version()
        info = adapter.configuration_info()
        try:
            abi = landlock_abi()
            edit_sandbox = {'status':'AVAILABLE','mechanism':'Landlock','abi':abi}
        except LandlockUnavailable as exc:
            edit_sandbox = {'status':'UNAVAILABLE','reason':str(exc)}
        value = {'schema_version': 1, 'worker': 'kimi', 'status': 'CONFIGURED',
                 'executable': str(adapter.executable), 'version': version,
                 'requested_model': info['cli_model'], 'provider': info['provider'],
                 'endpoint_host': info['endpoint_host'],
                 'authentication': 'Kimi Code managed OAuth; credential not inspected',
                 'isolated_edit_sandbox': edit_sandbox,
                 'provider_test': 'not performed by preflight'}
        return emit_json(value) if args.json else print_human(value)
    except (OSError, RuntimeError, ValueError) as exc:
        value = error_result('CONFIG_ERROR', str(exc)[:240])
        return emit_json(value, 3) if args.json else print_human(value, 3)


def command_status(args) -> int:
    """Report local configuration and last explicit Kimi test; never calls providers."""
    settings_path = Path('/home/krakadin/.claude/settings.json')
    model = None
    settings_base_override = False
    try:
        if settings_path.is_file():
            settings = json.loads(settings_path.read_text(encoding='utf-8'))
            if isinstance(settings, dict):
                candidate = settings.get('model')
                model = candidate if isinstance(candidate, str) else None
                env_settings = settings.get('env', {})
                settings_base_override = isinstance(env_settings, dict) and 'ANTHROPIC_BASE_URL' in env_settings
    except (OSError, UnicodeError, ValueError):
        pass
    env_override = 'ANTHROPIC_BASE_URL' in os.environ
    try:
        rows = StateStore(RUNTIME / 'workers.db').jobs(limit=100)
    except (OSError, RuntimeError, sqlite3.Error):
        rows = []
    last_test = next((row for row in rows if row.get('worker') == 'kimi'
                      and row.get('job_type') == 'provider_test'), None)
    kimi_status = 'UNKNOWN'
    if last_test:
        if last_test.get('status') == 'completed':
            kimi_status = 'LAST_TEST_SUCCEEDED'
        elif last_test.get('error_code') == 'AUTH_ERROR':
            kimi_status = 'AUTH_REQUIRED'
        else:
            kimi_status = 'LAST_TEST_FAILED'
    value = {
        'schema_version': 1,
        'claude': {
            'status': 'CONFIGURED' if model else 'UNKNOWN',
            'model': model,
            'provider': 'Anthropic',
            'authentication': 'Claude Code-managed Max OAuth; not tested by ai-worker',
            'routing': 'OVERRIDE_PRESENT' if settings_base_override or env_override else 'DIRECT (no ANTHROPIC_BASE_URL override detected)',
        },
        'kimi': {
            'status': kimi_status,
            'executable': '/home/krakadin/.kimi-code/bin/kimi',
            'requested_model': 'kimi-code/k3',
            'provider': 'Kimi Code',
            'authentication': 'Kimi Code-managed OAuth; credential not inspected',
            'last_test_at': last_test.get('completed_at') if last_test else None,
            'last_test_status': last_test.get('status') if last_test else None,
        },
        'dashboard': 'not implemented',
        'runtime': str(RUNTIME),
    }
    if args.json:
        return emit_json(value)
    print('AI Worker Status')
    for provider in ('claude', 'kimi'):
        info = value[provider]
        print(f"\n{provider.title()}\n  Status: {info['status']}\n  Model: {info.get('model') or info.get('requested_model') or 'unknown'}")
        print(f"  Provider: {info['provider']}\n  Authentication: {info['authentication']}")
        if provider == 'claude':
            print(f"  Routing: {info['routing']}")
        else:
            print(f"  Last live test: {info['last_test_at'] or 'not performed'}")
    print('\nDashboard: not implemented (worker CLI operates independently)')
    return 0


def command_diff(args) -> int:
    try:
        job_id = str(uuid.UUID(args.job_id))
    except ValueError:
        value = error_result('INVALID_JOB_ID', 'Job ID must be a UUID.')
        return emit_json(value,2) if args.json else print_human(value,2)
    try:
        job=StateStore(RUNTIME/'workers.db').get_job(job_id)
        if job is None:
            value=error_result('NOT_FOUND','Job was not found.')
            return emit_json(value,2) if args.json else print_human(value,2)
        if job.get('mode')!='isolated-edit':
            value=error_result('INVALID_MODE','This job has no isolated-edit worktree.')
            return emit_json(value,2) if args.json else print_human(value,2)
        adapter=KimiAdapter(RUNTIME/'tmp')
        workspace,diff,truncated=adapter.collect_edit_output(job_id)
        if workspace is None:
            value=error_result('WORKTREE_MISSING','The isolated worktree is unavailable.')
            return emit_json(value,5) if args.json else print_human(value,5)
        value={'schema_version':1,'job_id':job_id,'status':job.get('status'),
               'requested_model':job.get('requested_model'),'workspace':workspace,
               'diff':diff,'diff_truncated':truncated}
        return emit_json(value) if args.json else print_human({'job_id':job_id,'workspace':workspace,'result':diff or '(No changes.)'})
    except (OSError,sqlite3.Error,RuntimeError,ValueError):
        value=error_result('DIFF_UNAVAILABLE','The isolated diff could not be read safely.')
        return emit_json(value,5) if args.json else print_human(value,5)


def command_discard(args) -> int:
    try:
        job_id=str(uuid.UUID(args.job_id))
        store=StateStore(RUNTIME/'workers.db')
        job=store.get_job(job_id)
        if job is None:
            value=error_result('NOT_FOUND','Job was not found.')
            return emit_json(value,2) if args.json else print_human(value,2)
        if job.get('mode')!='isolated-edit':
            value=error_result('INVALID_MODE','Only isolated-edit jobs have disposable worktrees.')
            return emit_json(value,2) if args.json else print_human(value,2)
        if job.get('status') in ('running','queued'):
            value=error_result('JOB_ACTIVE','An active job cannot be discarded.')
            return emit_json(value,5) if args.json else print_human(value,5)
        job_dir=RUNTIME/'jobs'/job_id
        worktree=job_dir/'worktree'
        runtime_jobs=(RUNTIME/'jobs').resolve(strict=True)
        if (job_dir.is_symlink() or worktree.is_symlink() or not worktree.is_dir()
                or not job_dir.is_dir()):
            value=error_result('WORKTREE_MISSING','The isolated worktree is missing or unsafe.')
            return emit_json(value,5) if args.json else print_human(value,5)
        try:
            job_info=job_dir.stat()
            if (job_info.st_uid!=os.getuid() or stat.S_IMODE(job_info.st_mode)!=0o700
                    or job_dir.resolve(strict=True).parent!=runtime_jobs
                    or worktree.resolve(strict=True).parent!=job_dir.resolve(strict=True)):
                raise ValueError('unsafe job directory')
        except (OSError,ValueError):
            value=error_result('UNSAFE_RUNTIME_PATH','Refusing to remove an unsafe job directory.')
            return emit_json(value,5) if args.json else print_human(value,5)
        try:
            source=Path(job['cwd']).resolve(strict=True)
            source.relative_to(Path('/home/krakadin/myDev').resolve(strict=True))
        except (OSError,ValueError,KeyError):
            value=error_result('PATH_NOT_ALLOWED','Source repository path is no longer allowed.')
            return emit_json(value,2) if args.json else print_human(value,2)
        if not args.confirm:
            value={'schema_version':1,'job_id':job_id,'status':'confirmation_required',
                   'message':'This permanently removes the ai-router worktree and its per-job Kimi session history. Review `ai-worker diff` first, then repeat with --confirm.'}
            return emit_json(value,2) if args.json else print_human({'result':value['message']},2)
        adapter=KimiAdapter(RUNTIME/'tmp')
        result=adapter._git(['worktree','remove','--force',str(worktree)],source,timeout=60)
        if result.returncode!=0 or worktree.exists():
            value=error_result('WORKTREE_REMOVE_FAILED','Git could not safely remove the isolated worktree.')
            return emit_json(value,5) if args.json else print_human(value,5)
        shutil.rmtree(job_dir)
        value={'schema_version':1,'job_id':job_id,'status':'discarded'}
        return emit_json(value) if args.json else print_human({'result':'Isolated worktree and ai-router job data discarded.'})
    except (OSError,sqlite3.Error,RuntimeError,ValueError):
        value=error_result('DISCARD_FAILED','The isolated worktree could not be safely discarded.')
        return emit_json(value,5) if args.json else print_human(value,5)


def command_jobs(args) -> int:
    try:
        store = StateStore(RUNTIME / 'workers.db')
        rows = store.jobs(limit=50)
    except (OSError, RuntimeError):
        rows = []
    value = {'schema_version': 1, 'jobs': rows}
    return emit_json(value) if args.json else print_human({'result': json.dumps(rows, ensure_ascii=False)})


def command_show(args) -> int:
    try:
        job_id = str(uuid.UUID(args.job_id))
    except ValueError:
        value = error_result('INVALID_JOB_ID', 'Job ID must be a UUID.')
        return emit_json(value, 2) if args.json else print_human(value, 2)
    try:
        job = StateStore(RUNTIME / 'workers.db').get_job(job_id)
    except (OSError, RuntimeError):
        job = None
    if job is None:
        value = error_result('NOT_FOUND', 'Job was not found.')
        return emit_json(value, 2) if args.json else print_human(value, 2)
    value = {'schema_version': 1, 'job': job}
    return emit_json(value) if args.json else print_human({'result': json.dumps(job, ensure_ascii=False)})


def command_cancel(args) -> int:
    try:
        supervisor, _ = build_supervisor()
        changed, status = supervisor.cancel(args.job_id)
        code = 0 if changed else (2 if status in ('INVALID_JOB_ID', 'NOT_FOUND') else 5)
        value = {'schema_version': 1, 'job_id': args.job_id, 'status': status.lower(), 'changed': changed}
    except (OSError, RuntimeError, ValueError):
        value, code = error_result('CONFIG_ERROR', 'Cancellation could not access runtime state.'), 3
    return emit_json(value, code) if args.json else print_human(value, code)


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='ai-worker', description='Local read-only AI worker supervisor.')
    commands = parser.add_subparsers(dest='command', required=True)
    run = commands.add_parser('run', help='Read a JSON worker request from stdin.')
    run.add_argument('--json', action='store_true', help='Emit machine-readable JSON only.')
    run.set_defaults(func=command_run)
    delegate = commands.add_parser('delegate', help='Read task text from stdin and delegate to one worker.')
    delegate.add_argument('worker', choices=('kimi',))
    delegate.add_argument('--cwd', required=True)
    delegate.add_argument('--timeout', type=int, default=600)
    delegate.add_argument('--mode', choices=('read-only','isolated-edit'), default='read-only')
    delegate.add_argument('--json', action='store_true')
    delegate.set_defaults(func=command_delegate)
    test = commands.add_parser('test', help='Run a small live provider smoke test.')
    test.add_argument('worker', choices=('kimi',))
    test.add_argument('--json', action='store_true')
    test.set_defaults(func=command_test)
    preflight = commands.add_parser('preflight', help='Check Kimi executable/configuration locally; no provider call.')
    preflight.add_argument('--json', action='store_true')
    preflight.set_defaults(func=command_preflight)
    status = commands.add_parser('status', help='Show local Claude routing and cached Kimi test status.')
    status.add_argument('--json', action='store_true')
    status.set_defaults(func=command_status)
    jobs = commands.add_parser('jobs', help='List recent local jobs.')
    jobs.add_argument('--json', action='store_true')
    jobs.set_defaults(func=command_jobs)
    show = commands.add_parser('show', help='Show a local job by UUID.')
    show.add_argument('job_id')
    show.add_argument('--json', action='store_true')
    show.set_defaults(func=command_show)
    diff = commands.add_parser('diff', help='Show changes from a Kimi isolated-edit job.')
    diff.add_argument('job_id')
    diff.add_argument('--json', action='store_true')
    diff.set_defaults(func=command_diff)
    discard = commands.add_parser('discard', help='Remove a completed isolated-edit worktree after explicit confirmation.')
    discard.add_argument('job_id')
    discard.add_argument('--confirm',action='store_true')
    discard.add_argument('--json',action='store_true')
    discard.set_defaults(func=command_discard)
    cancel = commands.add_parser('cancel', help='Cancel a worker process started by ai-worker.')
    cancel.add_argument('job_id')
    cancel.add_argument('--json', action='store_true')
    cancel.set_defaults(func=command_cancel)
    return parser


def main(argv=None) -> int:
    try:
        args = make_parser().parse_args(argv)
        return args.func(args)
    except KeyboardInterrupt:
        value = error_result('CANCELLED', 'Interrupted by user.')
        return emit_json(value, 7)


if __name__ == '__main__':
    raise SystemExit(main())
