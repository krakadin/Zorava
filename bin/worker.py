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

from ai_router.request import MAX_REQUEST_BYTES, MAX_TASK_BYTES, DEFAULT_TIMEOUT, RequestError
from ai_router.landlock import LandlockUnavailable, landlock_abi
from ai_router.settings import (MAX_TOOL_CALLS_LIMIT, UNLIMITED_TOOL_CALLS, SettingsError,
                                load_settings, parse_budget_token, save_settings)
from ai_router.state import StateStore
from ai_router.retention import DEFAULT_RETENTION_DAYS, apply_cleanup, preview_cleanup
from ai_router.supervisor import Supervisor
from workers.kimi import KimiAdapter
from workers.qwen import QwenAdapter


RUNTIME = Path('/home/krakadin/.local/state/ai-workers')
ALLOWED_ROOTS = (Path('/home/krakadin/myDev'),)
EXIT_CODES = {
    'INVALID_REQUEST': 2, 'INVALID_WORKER': 2, 'INVALID_MODE': 2,
    'INVALID_TASK': 2, 'INVALID_TIMEOUT': 2, 'REQUEST_TOO_LARGE': 2,
    'PATH_NOT_ALLOWED': 2, 'PATH_ACCESS_ERROR': 2,
    'CONFIG_ERROR': 3, 'AUTH_ERROR': 4, 'WORKER_CRASH': 5,
    'WORKER_BUSY': 5, 'INVALID_OUTPUT': 5, 'OUTPUT_LIMIT': 5,
    'MODEL_UNAVAILABLE': 5, 'RATE_LIMITED': 5, 'QUOTA_OR_BILLING': 5, 'SMOKE_TEST_MISMATCH': 5,
    'NETWORK_ERROR': 5, 'BUDGET_EXHAUSTED': 5, 'TIMEOUT': 6, 'CANCELLED': 7,
    'SANDBOX_UNAVAILABLE': 3, 'PROJECT_DIRTY': 2,
    'NOT_A_GIT_REPOSITORY': 2, 'GIT_ERROR': 3, 'GIT_WORKTREE_ERROR': 3,
}


def build_supervisor() -> tuple[Supervisor, dict]:
    runtime_tmp = RUNTIME / 'tmp'
    adapters = {'kimi': KimiAdapter(runtime_tmp=runtime_tmp),
                'qwen': QwenAdapter(runtime_tmp=runtime_tmp)}
    return Supervisor(RUNTIME, ALLOWED_ROOTS, adapters), adapters


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


def run_request(raw: bytes, *, job_type='delegation', job_id=None) -> tuple[dict, int]:
    try:
        supervisor, _ = build_supervisor()
        result = supervisor.run(raw, job_type=job_type, job_id=job_id)
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
                   'mode': args.mode, 'timeout_seconds': args.timeout or DEFAULT_TIMEOUT[args.worker]}
        if args.max_tool_calls is not None:
            try:
                request['max_tool_calls'] = parse_budget_token(args.max_tool_calls)
            except SettingsError as exc:
                raise RequestError('INVALID_REQUEST', str(exc)) from None
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
    if args.worker == 'qwen':
        request['max_tool_calls'] = 0
    value, code = run_request(json.dumps(request).encode('utf-8'), job_type='provider_test', job_id=args.job_id)
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
        print(f"{str(value.get('worker', 'Worker')).title()} job {value.get('status', 'unknown')} ({value['job_id']})")
        print(f"Model requested: {value.get('requested_model', 'unknown')}")
        print(f"CLI version: {value.get('worker_version', 'unknown')}")
        if value.get('duration_ms') is not None:
            print(f"Duration: {value['duration_ms']} ms")
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
        adapter = {'kimi': KimiAdapter, 'qwen': QwenAdapter}[args.worker](RUNTIME / 'tmp')
        version = adapter.version()
        info = adapter.configuration_info()
        value = {'schema_version': 1, 'worker': args.worker, 'status': 'CONFIGURED',
                 'executable': str(adapter.executable), 'version': version,
                 'requested_model': info['cli_model'], 'provider': info['provider'],
                 'endpoint_host': info['endpoint_host'],
                 'authentication': ('Kimi Code managed OAuth; credential not inspected' if args.worker == 'kimi'
                                    else ('Qwen-owned credential present' if info['credential_present'] else 'Qwen credential missing')),
                 'provider_test': 'not performed by preflight'}
        if args.worker == 'kimi':
            try:
                abi = landlock_abi()
                edit_sandbox = {'status':'AVAILABLE','mechanism':'Landlock','abi':abi}
            except LandlockUnavailable as exc:
                edit_sandbox = {'status':'UNAVAILABLE','reason':str(exc)}
            value['isolated_edit_sandbox'] = edit_sandbox
        return emit_json(value) if args.json else print_human(value)
    except (OSError, RuntimeError, ValueError) as exc:
        value = error_result('CONFIG_ERROR', str(exc)[:240])
        return emit_json(value, 3) if args.json else print_human(value, 3)


def command_status(args) -> int:
    """Report local configuration and last explicit provider tests; never calls providers."""
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
    worker_names = ('qwen', 'kimi')
    last_tests = {name: None for name in worker_names}
    last_successes = {name: None for name in worker_names}
    try:
        store = StateStore(RUNTIME / 'workers.db')
        for name in worker_names:
            tests = store.provider_tests(name, limit=1)
            last_tests[name] = tests[0] if tests else None
            last_successes[name] = store.last_successful_test(name)
    except (OSError, RuntimeError, sqlite3.Error):
        pass
    provider_status = {}
    for name, last_test in last_tests.items():
        status = 'UNKNOWN'
        if last_test:
            test_status = last_test.get('status')
            if test_status in ('queued', 'running'): status = 'TESTING'
            elif test_status == 'completed': status = 'LAST_TEST_SUCCEEDED'
            elif last_test.get('error_code') == 'AUTH_ERROR': status = 'AUTH_REQUIRED'
            else: status = 'LAST_TEST_FAILED'
        provider_status[name] = status

    def test_fields(name: str) -> dict:
        last_test = last_tests[name]
        return {
            'last_test_id': last_test.get('id') if last_test else None,
            'last_test_at': (last_test.get('completed_at') or last_test.get('created_at')) if last_test else None,
            'last_test_status': last_test.get('status') if last_test else None,
            'last_test_error': (last_test.get('error_code') or last_test.get('error_message')) if last_test else None,
            'last_success_at': last_successes[name],
        }
    try:
        qwen_info = QwenAdapter(RUNTIME/'tmp').configuration_info()
    except (OSError, RuntimeError, ValueError):
        qwen_info = {'provider': 'Alibaba Model Studio Token Plan', 'endpoint_host': 'unknown',
                     'cli_model': QwenAdapter.requested_model, 'credential_present': False}
    value = {
        'schema_version': 1,
        'claude': {
            'status': 'CONFIGURED' if model else 'UNKNOWN',
            'model': model,
            'provider': 'Anthropic',
            'authentication': 'Claude Code-managed Max OAuth; not tested by ai-worker',
            'routing': 'OVERRIDE_PRESENT' if settings_base_override or env_override else 'DIRECT (no ANTHROPIC_BASE_URL override detected)',
        },
        'qwen': {
            'status': provider_status['qwen'],
            'executable': str(QwenAdapter(RUNTIME/'tmp').executable),
            'requested_model': qwen_info['cli_model'],
            'provider': qwen_info['provider'],
            'endpoint_host': qwen_info['endpoint_host'],
            'authentication': 'Qwen-owned credential present' if qwen_info['credential_present'] else 'Qwen credential missing',
            **test_fields('qwen'),
        },
        'kimi': {
            'status': provider_status['kimi'],
            'executable': '/home/krakadin/.kimi-code/bin/kimi',
            'requested_model': 'kimi-code/k3',
            'provider': 'Kimi Code',
            'authentication': 'Kimi Code-managed OAuth; credential not inspected',
            **test_fields('kimi'),
        },
        'dashboard': {'available': True, 'default_url': 'http://127.0.0.1:8787/',
                      'status': 'not checked; dashboard is optional'},
        'runtime': str(RUNTIME),
    }
    if args.json:
        return emit_json(value)
    print('Zorava Status')
    for provider in ('claude', 'qwen', 'kimi'):
        info = value[provider]
        print(f"\n{provider.title()}\n  Status: {info['status']}\n  Model: {info.get('model') or info.get('requested_model') or 'unknown'}")
        print(f"  Provider: {info['provider']}\n  Authentication: {info['authentication']}")
        if provider == 'claude':
            print(f"  Routing: {info['routing']}")
        else:
            print(f"  Last live test: {info['last_test_at'] or 'not performed'}")
            if info.get('last_test_id'):
                print(f"  Last test job: {info['last_test_id']} ({info.get('last_test_status') or 'unknown'})")
            if info.get('last_test_error'):
                print(f"  Last test error: {info['last_test_error']}")
            print(f"  Last successful test: {info.get('last_success_at') or 'never'}")
    print('\nDashboard: available at http://127.0.0.1:8787/ when started; worker CLI operates independently')
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
        adapter=(QwenAdapter if job['worker']=='qwen' else KimiAdapter)(RUNTIME/'tmp')
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
                   'message':'This permanently removes the ai-router worktree and its per-job worker runtime data. Review `ai-worker diff` first, then repeat with --confirm.'}
            return emit_json(value,2) if args.json else print_human({'result':value['message']},2)
        adapter=(QwenAdapter if job['worker']=='qwen' else KimiAdapter)(RUNTIME/'tmp')
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


def command_cleanup(args) -> int:
    try:
        store = StateStore(RUNTIME / 'workers.db')
        if args.confirm:
            result = apply_cleanup(store, RUNTIME, retention_days=DEFAULT_RETENTION_DAYS)
            value = {'schema_version':1, 'status':'completed', 'retention_days':DEFAULT_RETENTION_DAYS,
                     'eligible_count':result['eligible_count'], 'protected_count':result['protected_count'],
                     'deleted_count':result['deleted_count']}
        else:
            result = preview_cleanup(store, RUNTIME, retention_days=DEFAULT_RETENTION_DAYS)
            value = {'schema_version':1, 'status':'dry_run', 'retention_days':DEFAULT_RETENTION_DAYS,
                     'eligible_count':result['eligible_count'], 'protected_count':result['protected_count'],
                     'eligible':result['eligible'], 'protected':result['protected']}
        return emit_json(value) if args.json else print_human({'result':json.dumps(value,ensure_ascii=False,indent=2)})
    except (OSError, RuntimeError, ValueError, sqlite3.Error):
        value = error_result('CLEANUP_FAILED','Expired ai-router history could not be inspected safely.')
        return emit_json(value,3) if args.json else print_human(value,3)


def command_dashboard(args) -> int:
    from dashboard.server import serve
    try:
        serve(host='127.0.0.1', port=args.port)
        return 0
    except OSError as exc:
        if exc.errno == 98:
            message = f'127.0.0.1:{args.port} is already in use; no process was stopped.'
        else:
            message = 'Dashboard could not bind to the requested loopback port.'
        print(message, file=sys.stderr)
        return 3


def _settings_view(settings: dict) -> dict:
    budget = settings['qwen_coding_max_tool_calls']
    return {'qwen_coding_max_tool_calls': budget,
            'qwen_coding_budget': 'unlimited' if budget == UNLIMITED_TOOL_CALLS else budget,
            'applies_to': 'qwen isolated-edit jobs without an explicit max_tool_calls'}


def command_settings_show(args) -> int:
    try:
        value = {'schema_version': 1, 'settings': _settings_view(load_settings(RUNTIME))}
        return emit_json(value) if args.json else print_human({'result': json.dumps(value['settings'], ensure_ascii=False)})
    except SettingsError as exc:
        value = error_result(exc.code, str(exc)[:240])
        return emit_json(value, 3) if args.json else print_human(value, 3)


def command_settings_set(args) -> int:
    try:
        budget = parse_budget_token(args.value)
    except SettingsError as exc:
        value = error_result('INVALID_REQUEST', str(exc)[:240])
        return emit_json(value, 2) if args.json else print_human(value, 2)
    try:
        saved = save_settings(RUNTIME, qwen_coding_max_tool_calls=budget)
    except SettingsError as exc:
        value = error_result(exc.code, str(exc)[:240])
        return emit_json(value, 3) if args.json else print_human(value, 3)
    value = {'schema_version': 1, 'status': 'saved', 'settings': _settings_view(saved),
             'note': 'Applies to future Qwen coding jobs only; running jobs are unchanged.'}
    return emit_json(value) if args.json else print_human({'result': json.dumps(value['settings'], ensure_ascii=False)})


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='ai-worker', description='Zorava: local read-only AI worker supervisor.')
    commands = parser.add_subparsers(dest='command', required=True)
    run = commands.add_parser('run', help='Read a JSON worker request from stdin.')
    run.add_argument('--json', action='store_true', help='Emit machine-readable JSON only.')
    run.set_defaults(func=command_run)
    delegate = commands.add_parser('delegate', help='Read task text from stdin and delegate to one worker.')
    delegate.add_argument('worker', choices=('qwen','kimi'))
    delegate.add_argument('--cwd', required=True)
    delegate.add_argument('--timeout', type=int)
    delegate.add_argument('--mode', choices=('read-only','isolated-edit'), default='isolated-edit')
    delegate.add_argument('--max-tool-calls',
                          help=f"Qwen tool-call budget: 'unlimited' or 0-{MAX_TOOL_CALLS_LIMIT} (not supported for kimi).")
    delegate.add_argument('--json', action='store_true')
    delegate.set_defaults(func=command_delegate)
    test = commands.add_parser('test', help='Run a small live provider smoke test.')
    test.add_argument('worker', choices=('qwen','kimi'))
    test.add_argument('--json', action='store_true')
    test.add_argument('--job-id', help=argparse.SUPPRESS)
    test.set_defaults(func=command_test)
    preflight = commands.add_parser('preflight', help='Check worker executable/configuration locally; no provider call.')
    preflight.add_argument('worker', choices=('qwen','kimi'), nargs='?', default='kimi')
    preflight.add_argument('--json', action='store_true')
    preflight.set_defaults(func=command_preflight)
    status = commands.add_parser('status', help='Show local Claude routing and cached worker test status.')
    status.add_argument('--json', action='store_true')
    status.set_defaults(func=command_status)
    jobs = commands.add_parser('jobs', help='List recent local jobs.')
    jobs.add_argument('--json', action='store_true')
    jobs.set_defaults(func=command_jobs)
    show = commands.add_parser('show', help='Show a local job by UUID.')
    show.add_argument('job_id')
    show.add_argument('--json', action='store_true')
    show.set_defaults(func=command_show)
    diff = commands.add_parser('diff', help='Show changes from a worker isolated-edit job.')
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
    cleanup = commands.add_parser('cleanup', help='Preview or confirm purging expired ai-router job history (30 days).')
    cleanup_mode = cleanup.add_mutually_exclusive_group()
    cleanup_mode.add_argument('--dry-run', action='store_true', help='Preview (the default).')
    cleanup_mode.add_argument('--confirm', action='store_true', help='Permanently purge eligible expired records.')
    cleanup.add_argument('--json', action='store_true')
    cleanup.set_defaults(func=command_cleanup)
    dashboard = commands.add_parser('dashboard', help='Run the local-only operations dashboard.')
    dashboard.add_argument('--port', type=int, default=8787)
    dashboard.set_defaults(func=command_dashboard)
    settings_cmd = commands.add_parser('settings', help='Show or set saved local defaults (no credentials).')
    settings_sub = settings_cmd.add_subparsers(dest='settings_command', required=True)
    settings_show = settings_sub.add_parser('show', help='Show the saved Qwen coding tool-call budget.')
    settings_show.add_argument('--json', action='store_true')
    settings_show.set_defaults(func=command_settings_show)
    settings_set = settings_sub.add_parser('set', help='Save the default Qwen coding tool-call budget.')
    settings_set.add_argument('key', choices=('qwen-coding-budget',))
    settings_set.add_argument('value', help=f"'unlimited' or an integer 0-{MAX_TOOL_CALLS_LIMIT}.")
    settings_set.add_argument('--json', action='store_true')
    settings_set.set_defaults(func=command_settings_set)
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
