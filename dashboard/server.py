"""Small loopback-only dashboard for local ai-worker state and fixed actions."""

from __future__ import annotations

from datetime import datetime, timezone
import fcntl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import uuid
from urllib.parse import parse_qs, urlsplit

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from ai_router.request import DEFAULT_TIMEOUT, MAX_TIMEOUT
from ai_router.retention import DEFAULT_RETENTION_DAYS, apply_cleanup, preview_cleanup
from ai_router.settings import (SETTINGS_FIELDS, SettingsError, load_worker_concurrency,
                                load_worker_model_profile, save_settings,
                                validate_concurrency, validate_model_profile_selection,
                                verified_model_catalog)
from ai_router.security import redact
from ai_router.state import StateStore
from ai_router.supervisor import Supervisor
from ai_router.updates import SOURCES, WORKERS, UpdateRegistry, requested_workers
from workers.base import common_child_environment
from workers.kimi import KimiAdapter
from workers.qwen import QwenAdapter


RUNTIME = Path('/home/krakadin/.local/state/ai-workers')
WORKER_SCRIPT = PROJECT / 'bin' / 'worker.py'
DEFAULT_HOST = '127.0.0.1'
DEFAULT_PORT = 8787
MAX_HTTP_BODY = 4096


class DashboardController:
    def __init__(self, runtime: Path = RUNTIME, port: int = DEFAULT_PORT):
        self.runtime = runtime
        self.port = port
        self.store = StateStore(runtime / 'workers.db')
        self.csrf_token = secrets.token_urlsafe(32)
        self._lock = threading.RLock()
        self._test_processes: dict[str, tuple[str, subprocess.Popen]] = {}
        self._test_threads: dict[str, threading.Thread] = {}
        self._stopping = False
        self.provider_snapshot = None
        # Cached, read-only CLI version metadata. It is only ever refreshed by
        # an explicit operator action; dashboard GETs stay offline.
        self.updates = UpdateRegistry()

    def _provider_base(self, adapter, name):
        try:
            version = adapter.version()
            select = getattr(adapter, 'select_model_profile', None)
            if callable(select):
                # Apply the saved verified profile so the card reports the model
                # future jobs will actually request. A stale or unverifiable
                # selection fails closed here exactly as it does at job launch:
                # the card reports UNAVAILABLE with a sanitized diagnostic and
                # the adapter keeps its reviewed default model.
                select(load_worker_model_profile(self.runtime, name))
            info = adapter.configuration_info()
            return {'worker': name, 'status': 'CONFIGURED', 'executable': str(adapter.executable),
                    'version': version, 'requested_model': adapter.requested_model,
                    'provider': info.get('provider'), 'endpoint_host': info.get('endpoint_host'),
                    'endpoint_url': info.get('base_url'), 'authentication':
                    ('Qwen-owned credential present' if info.get('credential_present') else 'Qwen credential missing')
                    if name == 'qwen' else 'Kimi Code managed OAuth; credential not inspected'}
        except Exception as exc:
            safe, _ = redact(str(exc))
            return {'worker': name, 'status': 'UNAVAILABLE', 'executable': str(adapter.executable),
                    'version': None, 'requested_model': adapter.requested_model,
                    'provider': 'Alibaba Model Studio Token Plan' if name == 'qwen' else 'Kimi Code',
                    'endpoint_host': None, 'endpoint_url': None,
                    'authentication': 'unknown', 'diagnostic': safe[:240]}

    def _load_provider_snapshot(self):
        qwen = self._provider_base(QwenAdapter(self.runtime / 'tmp'), 'qwen')
        kimi = self._provider_base(KimiAdapter(self.runtime / 'tmp'), 'kimi')
        claude_model = None
        settings = Path('/home/krakadin/.claude/settings.json')
        base_override = 'ANTHROPIC_BASE_URL' in os.environ
        settings_override = False
        try:
            if settings.is_file() and not settings.is_symlink():
                value = json.loads(settings.read_text(encoding='utf-8'))
                if isinstance(value, dict):
                    model = value.get('model')
                    claude_model = model if isinstance(model, str) else None
                    env = value.get('env')
                    settings_override = isinstance(env, dict) and 'ANTHROPIC_BASE_URL' in env
        except (OSError, UnicodeError, ValueError):
            pass
        return {'claude': {'worker': 'claude', 'status': 'CONFIGURED' if claude_model else 'UNKNOWN',
                           'requested_model': claude_model, 'provider': 'Anthropic', 'endpoint_host': 'api.anthropic.com',
                           'authentication': 'Claude Code-managed Max OAuth; not tested by ai-worker',
                           'routing': 'OVERRIDE_PRESENT' if base_override or settings_override else 'DIRECT'},
                'qwen': qwen, 'kimi': kimi}

    def _worker_concurrency(self, name: str) -> int:
        try:
            return load_worker_concurrency(self.runtime, name)
        except SettingsError:
            return SETTINGS_FIELDS[f'{name}_concurrency']['default']

    def model_catalog(self) -> dict:
        """Read-only safe model catalog: selected ID plus allowlisted options.

        Options carry ID/display name/context metadata only -- never URLs,
        keys, or credential fields. Discovery failure falls back to each
        worker's single reviewed default profile.
        """
        catalog = {}
        for name in ('qwen', 'kimi'):
            default = SETTINGS_FIELDS[f'{name}_model_profile']['default']
            try:
                selected = load_worker_model_profile(self.runtime, name)
            except SettingsError:
                selected = default
            try:
                options = verified_model_catalog(name)
            except Exception:
                options = [{'id': default, 'display_name': default, 'context_window': None}]
            catalog[name] = {'selected': selected, 'available': options}
        return catalog

    def providers(self):
        if self.provider_snapshot is None:
            self.provider_snapshot = self._load_provider_snapshot()
        with self._lock:
            starting_workers = {worker for worker, _proc in self._test_processes.values()}
        now = datetime.now(timezone.utc)
        activity = self.store.worker_activity()
        values = {}
        for name in ('qwen', 'kimi'):
            info = dict(self.provider_snapshot[name])
            tests = self.store.provider_tests(name, limit=1)
            last = tests[0] if tests else None
            status = info['status']
            if last:
                when = _parse_time(last.get('completed_at') or last.get('created_at'))
                fresh = when is not None and (now - when).total_seconds() <= 86400
                if last.get('status') in ('queued', 'running'): status = 'TESTING'
                elif last.get('error_code') == 'AUTH_ERROR': status = 'AUTH_REQUIRED'
                elif last.get('error_code') in ('RATE_LIMITED', 'QUOTA_OR_BILLING'): status = 'DEGRADED'
                elif last.get('status') == 'completed' and fresh: status = 'READY'
                elif fresh: status = 'DEGRADED'
                elif status == 'CONFIGURED': status = 'UNKNOWN'
            elif status == 'CONFIGURED':
                status = 'UNKNOWN'
            # The wrapper starts before its job is recorded in SQLite.
            if name in starting_workers:
                status = 'TESTING'
            info.update({'status': status,
                         'last_test_id': last.get('id') if last else None,
                         'last_test_at': (last.get('completed_at') or last.get('created_at')) if last else None,
                         'last_test_completed_at': last.get('completed_at') if last else None,
                         'last_test_status': last.get('status') if last else None,
                         'last_test_error': last.get('error_code') if last else None,
                         'last_success_at': self.store.last_successful_test(name),
                         'concurrency': self._worker_concurrency(name),
                         'queued_jobs': activity.get(name, {}).get('queued', 0),
                         'running_jobs': activity.get(name, {}).get('running', 0),
                         'role': 'Coder', 'default_mode': 'isolated-edit'})
            info.update(self.updates.status(name, info.get('version')).as_dict())
            values[name] = info
        values['claude'] = self.provider_snapshot['claude']
        return values

    def _installed_versions(self) -> dict:
        """Installed CLI versions from the already cached provider snapshot."""
        snapshot = self.provider_snapshot or {}
        versions = {}
        for name in WORKERS:
            info = snapshot.get(name)
            versions[name] = info.get('version') if isinstance(info, dict) else None
        return versions

    def updates_snapshot(self) -> dict:
        """Cache-only CLI version view; this never contacts a version source."""
        return {'cache_only': True, 'workers': list(WORKERS), 'sources': dict(SOURCES),
                'updates': self.updates.snapshot(self._installed_versions())}

    def request_update_check(self, workers=None) -> dict:
        """Queue bounded background version checks for the requested coders."""
        queued = self.updates.request_check(workers)
        return {'status': 'accepted', 'requested': queued['requested'],
                'already_checking': queued['already_checking'], 'deferred': queued['deferred'],
                'note': 'Read-only version metadata; nothing is downloaded or installed.',
                'updates': self.updates.snapshot(self._installed_versions())}

    def _slot_lock_paths(self, worker: str) -> list[Path]:
        """Every slot lock a supervisor could currently hold for this worker."""
        locks_dir = self.runtime / 'locks'
        paths = {locks_dir / f'{worker}.{index}.lock'
                 for index in range(max(1, self._worker_concurrency(worker)))}
        try:
            paths.update(locks_dir.glob(f'{worker}.*.lock'))
        except OSError:
            pass
        return sorted(paths)

    def _worker_has_active_job(self, worker: str) -> bool:
        if not self.store.active_jobs(worker):
            return False
        # A child can exit while its supervisor is still saving the result.
        # Only recover rows while holding every slot lock for that worker.
        held = []
        try:
            for path in self._slot_lock_paths(worker):
                try:
                    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
                except FileNotFoundError:
                    # A slot lock that no supervisor ever created cannot be
                    # held, so it must not keep an exited job's row from being
                    # recovered. A live process group is still checked below.
                    continue
                except OSError:
                    return True
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    # BlockingIOError means a supervisor holds this slot; any
                    # other error means the slot cannot be verified as free.
                    try: os.close(fd)
                    except OSError: pass
                    return True
                held.append(fd)
            for job in self.store.active_jobs(worker):
                pgid = job.get('process_group')
                if job['status'] == 'queued' or not isinstance(pgid, int) or pgid <= 0:
                    return True
                if Supervisor._group_exists(pgid):
                    return True
                self.store.mark_interrupted(job['id'])
        finally:
            for fd in held:
                try: os.close(fd)
                except OSError: pass
        return False

    def start_test(self, worker: str) -> str:
        if worker not in ('qwen', 'kimi'):
            raise ValueError('Unknown provider test.')
        with self._lock:
            if self._stopping:
                raise RuntimeError('Dashboard is shutting down.')
            if any(value[0] == worker for value in self._test_processes.values()):
                raise RuntimeError('A dashboard test for this provider is already running.')
            if self._worker_has_active_job(worker):
                raise RuntimeError('This provider already has an active worker job.')
            job_id = str(uuid.uuid4())
            command = [sys.executable, '-B', str(WORKER_SCRIPT), 'test', worker, '--json', '--job-id', job_id]
            proc = subprocess.Popen(command, cwd=PROJECT, env=common_child_environment(),
                                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, start_new_session=True, close_fds=True)
            self._test_processes[job_id] = (worker, proc)
            thread = threading.Thread(target=self._collect_test, args=(job_id, proc),
                                      name='ai-worker-dashboard-test', daemon=True)
            self._test_threads[job_id] = thread
            thread.start()
            return job_id

    def _collect_test(self, job_id: str, proc: subprocess.Popen) -> None:
        try:
            _stdout, _stderr = proc.communicate(timeout=150)
            # The worker's normalized/sanitized result is already in SQLite.
            # Discard captured outer CLI output; never create a second transcript.
        except subprocess.TimeoutExpired:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                try: proc.kill()
                except OSError: pass
        except OSError:
            pass
        finally:
            with self._lock:
                self._test_processes.pop(job_id, None)
                self._test_threads.pop(job_id, None)

    def cancel_job(self, job_id: str) -> dict:
        try:
            canonical = str(uuid.UUID(job_id))
        except (ValueError, AttributeError):
            raise ValueError('Job ID must be a UUID.') from None
        job = self.store.get_job(canonical)
        if job is None:
            raise LookupError('Job not found.')
        if job.get('status') not in ('running', 'queued'):
            raise RuntimeError('Only running or queued jobs can be cancelled.')
        command = [sys.executable, '-B', str(WORKER_SCRIPT), 'cancel', canonical, '--json']
        proc = subprocess.run(command, cwd=PROJECT, env=common_child_environment(),
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, timeout=8, check=False, close_fds=True)
        try:
            result = json.loads(proc.stdout.decode('utf-8', 'replace'))
        except ValueError:
            raise RuntimeError('Cancellation returned an invalid response.') from None
        return result

    def cleanup_preview(self):
        return preview_cleanup(self.store, self.runtime, retention_days=DEFAULT_RETENTION_DAYS)

    def cleanup_confirm(self):
        return apply_cleanup(self.store, self.runtime, retention_days=DEFAULT_RETENTION_DAYS)

    def shutdown_owned_tests(self):
        with self._lock:
            self._stopping = True
            tests = list(self._test_processes.items())
        for job_id, (_worker, proc) in tests:
            try:
                self.cancel_job(job_id)
            except Exception:
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                # This is only the dashboard-owned wrapper process. The
                # provider child is cancelled by its recorded worker PGID.
                try: proc.terminate()
                except OSError: pass
        with self._lock:
            threads = list(self._test_threads.values())
        for thread in threads:
            thread.join(timeout=5)
        # Bounded read-only version checks are dashboard-owned background work too.
        self.updates.shutdown(timeout=8)


def _parse_time(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _clean_job(row: dict, detail=False) -> dict:
    fields = ('id','worker','role','requested_model','worker_version','reported_model','cwd','mode','task_summary',
              'status','created_at','started_at','completed_at','duration_ms','exit_code','result','partial_result',
              'error_code','error_message','usage_json','parent_job_id','delegation_group_id','job_type')
    value = {key: row.get(key) for key in fields if key in row}
    if not detail:
        value.pop('result', None)
        value.pop('partial_result', None)
    for key in ('task_summary','result','partial_result','error_message'):
        if isinstance(value.get(key), str):
            value[key] = redact(value[key])[0]
    if 'usage_json' in value:
        try: value['usage'] = json.loads(value['usage_json']) if value['usage_json'] else None
        except ValueError: value['usage'] = None
        del value['usage_json']
    if detail:
        value['events'] = []
        for event in row.get('events', []):
            item = dict(event)
            if isinstance(item.get('detail'), str): item['detail'] = redact(item['detail'])[0]
            value['events'].append(item)
    return value


def _runtime_permissions(runtime: Path) -> dict:
    try:
        info = runtime.stat()
        private = (not runtime.is_symlink() and stat.S_ISDIR(info.st_mode)
                   and info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o700)
    except OSError:
        private = False
    return {'runtime_private': private, 'runtime_mode': '0700' if private else 'unsafe or missing',
            'dashboard_loopback': True, 'worker_mode': 'Qwen and Kimi coders · separate Git worktrees · diffs for review',
            'credentials_in_ai_router_config': False}


def make_handler(controller: DashboardController):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, format, *args):
            # Do not write URL query/task material to process logs.
            return

        def _headers(self, content_type='application/json; charset=utf-8', length=0):
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(length))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('X-Frame-Options', 'DENY')
            self.send_header('Content-Security-Policy', "default-src 'self'; connect-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.end_headers()

        def _host_ok(self):
            expected = {f'127.0.0.1:{controller.port}', f'localhost:{controller.port}'}
            return self.headers.get('Host', '') in expected

        def _body(self):
            raw_length = self.headers.get('Content-Length')
            try: length = int(raw_length or '0')
            except ValueError: raise ValueError('Invalid request length.') from None
            if length < 0 or length > MAX_HTTP_BODY:
                raise OverflowError('Request body exceeds the limit.')
            raw = self.rfile.read(length) if length else b'{}'
            try: value = json.loads(raw.decode('utf-8'))
            except (UnicodeError, ValueError): raise ValueError('Request must be valid JSON.') from None
            if not isinstance(value, dict): raise ValueError('Request must be a JSON object.')
            return value

        def _json(self, value, status=200):
            body = json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('X-Frame-Options', 'DENY')
            self.send_header('Content-Security-Policy', "default-src 'self'; connect-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.end_headers(); self.wfile.write(body)

        def _error(self, status, code, message):
            self._json({'error': {'code':code,'message':message}}, status)

        def _authorized_post(self):
            origin = self.headers.get('Origin', '')
            expected_origin = f'http://127.0.0.1:{controller.port}'
            token = self.headers.get('X-AI-Worker-CSRF', '')
            return (origin == expected_origin and secrets.compare_digest(token, controller.csrf_token)
                    and self.headers.get('Sec-Fetch-Site', 'same-origin') in ('same-origin','none'))

        def do_GET(self):
            if not self._host_ok(): return self._error(400,'BAD_HOST','Unexpected Host header.')
            parsed = urlsplit(self.path)
            if parsed.path == '/':
                body = (PROJECT/'dashboard'/'index.html').read_text(encoding='utf-8')
                body = body.replace('%%CSRF_TOKEN%%', controller.csrf_token).encode('utf-8')
                self._headers('text/html; charset=utf-8', len(body)); self.wfile.write(body); return
            if parsed.path in ('/static/app.js','/static/dashboard.css'):
                name = 'app.js' if parsed.path.endswith('.js') else 'dashboard.css'
                body = (PROJECT/'dashboard'/'static'/name).read_bytes()
                content = 'text/javascript; charset=utf-8' if name.endswith('.js') else 'text/css; charset=utf-8'
                self._headers(content,len(body)); self.wfile.write(body); return
            if parsed.path.startswith('/api/v1/'):
                self._get_api(parsed.path, parse_qs(parsed.query, keep_blank_values=False)); return
            self._error(404,'NOT_FOUND','Page not found.')

        def _get_api(self, path, query):
            try:
                if path == '/api/v1/session':
                    self._json({'csrf_token':controller.csrf_token}); return
                if path == '/api/v1/status':
                    jobs = controller.store.dashboard_jobs(limit=100)
                    active = [row for row in jobs if row['status'] in ('running','queued')]
                    recent = [_clean_job(row) for row in jobs[:8]]
                    failures = [_clean_job(row) for row in jobs if row.get('error_code')][:5]
                    self._json({'providers':controller.providers(),'active_jobs':[_clean_job(x) for x in active],
                                'recent_jobs':recent,'recent_failures':failures,
                                'counts':controller.store.dashboard_counts(),
                                'security':_runtime_permissions(controller.runtime)})
                    return
                if path == '/api/v1/providers':
                    self._json({'providers':controller.providers()}); return
                if path == '/api/v1/updates':
                    # Cache-only: a dashboard refresh must never reach a version source.
                    self._json(controller.updates_snapshot()); return
                if path == '/api/v1/permissions':
                    coding = {'role':'Coder','filesystem':'Create and edit source files in a separate Git worktree by default; read-only mode remains available',
                              'shell':'not exposed','git_writes':'worktree edits; diff returned for review',
                              'push':'off','deploy':'off'}
                    self._json({'qwen':dict(coding),'kimi':dict(coding),
                                'os_sandbox':'Both coders use a scoped file broker and Landlock write confinement. Workers run as the same user; no OS-level read sandbox.'})
                    return
                if path == '/api/v1/settings':
                    self._json({'allowed_roots':['/home/krakadin/myDev'],'qwen_timeout_seconds':DEFAULT_TIMEOUT['qwen'],'kimi_timeout_seconds':DEFAULT_TIMEOUT['kimi'],
                                'max_timeout_seconds':MAX_TIMEOUT,
                                'concurrency':{'qwen':controller._worker_concurrency('qwen'),'kimi':controller._worker_concurrency('kimi')},
                                'concurrency_limits':{'minimum':1,'maximum':SETTINGS_FIELDS['qwen_concurrency']['maximum']},
                                'models':controller.model_catalog(),
                                'default_mode':'isolated-edit','retention_days':DEFAULT_RETENTION_DAYS,
                                'dashboard_bind':'127.0.0.1','dashboard_port':controller.port,
                                'runtime_state':str(controller.runtime),'model_changes':'Use only locally verified provider model profiles; this dashboard does not edit provider URLs or credentials.'})
                    return
                if path == '/api/v1/jobs':
                    args = {key:(values[0] if values else '') for key,values in query.items()}
                    worker = args.get('worker') if args.get('worker') in ('qwen','kimi') else None
                    status = args.get('status') if args.get('status') else None
                    jobs = controller.store.dashboard_jobs(limit=200,worker=worker,status=status,
                                                           search=args.get('q'),date_utc=args.get('date'))
                    self._json({'jobs':[_clean_job(row) for row in jobs]}); return
                if path.startswith('/api/v1/jobs/'):
                    job_id = path.rsplit('/',1)[-1]
                    try: job_id = str(uuid.UUID(job_id))
                    except ValueError: return self._error(400,'INVALID_JOB_ID','Job ID must be a UUID.')
                    row = controller.store.get_job(job_id)
                    if row is None: return self._error(404,'NOT_FOUND','Job was not found.')
                    self._json({'job':_clean_job(row,detail=True)}); return
                if path == '/api/v1/logs':
                    args = {key:(values[0] if values else '') for key,values in query.items()}
                    worker = args.get('worker') if args.get('worker') in ('qwen','kimi') else None
                    events = controller.store.dashboard_events(limit=250,worker=worker,errors_only=args.get('errors')=='1')
                    for event in events:
                        event['detail'] = redact(event.get('detail',''))[0]
                    self._json({'events':events}); return
                if path == '/api/v1/cleanup/preview':
                    result = controller.cleanup_preview()
                    self._json({'retention_days':result['retention_days'],'cutoff':result['cutoff'],
                                'eligible_count':result['eligible_count'],'protected_count':result['protected_count'],
                                'eligible':[{'id':row['id'],'worker':row['worker'],'status':row['status'],'created_at':row['created_at']} for row in result['eligible']]})
                    return
                self._error(404,'NOT_FOUND','API endpoint not found.')
            except (OSError, RuntimeError, ValueError, sqlite3.Error):
                self._error(500,'LOCAL_STATE_ERROR','Local dashboard state could not be read safely.')

        def do_POST(self):
            if not self._host_ok(): return self._error(400,'BAD_HOST','Unexpected Host header.')
            if not self._authorized_post(): return self._error(403,'REQUEST_REJECTED','Origin or CSRF validation failed.')
            try:
                body = self._body()
            except OverflowError:
                return self._error(413,'BODY_TOO_LARGE','Request body exceeds the limit.')
            except ValueError as exc:
                return self._error(400,'INVALID_REQUEST',str(exc))
            path = urlsplit(self.path).path
            try:
                if path in ('/api/v1/test/qwen','/api/v1/test/kimi'):
                    worker = path.rsplit('/',1)[-1]
                    job_id = controller.start_test(worker)
                    self._json({'job_id':job_id,'status':'queued','worker':worker},202); return
                if path == '/api/v1/updates/check':
                    # Explicit operator action only. It queues bounded read-only
                    # version checks; nothing is downloaded, installed, or upgraded.
                    try:
                        result = controller.request_update_check(body.get('workers'))
                    except ValueError as exc:
                        return self._error(400,'INVALID_REQUEST',str(exc))
                    self._json(result,202); return
                if path == '/api/v1/settings/concurrency':
                    # One worker per request. Validation and the atomic 0600
                    # write reuse the CLI settings path, so malformed or unsafe
                    # existing configuration fails closed instead of being
                    # silently overwritten. Only operational integers are saved.
                    worker = body.get('worker')
                    if worker not in ('qwen','kimi'):
                        return self._error(400,'INVALID_REQUEST','Worker must be qwen or kimi.')
                    try:
                        value = validate_concurrency(body.get('concurrency'))
                    except SettingsError as exc:
                        return self._error(400,'INVALID_REQUEST',str(exc))
                    try:
                        saved = save_settings(controller.runtime, **{f'{worker}_concurrency': value})
                    except SettingsError:
                        # Unsafe/malformed existing config or runtime directory;
                        # nothing was overwritten and no detail is exposed.
                        return self._error(500,'LOCAL_ACTION_FAILED','The requested local action failed safely.')
                    maximum = SETTINGS_FIELDS['qwen_concurrency']['maximum']
                    concurrency = {'qwen':saved['qwen_concurrency'],'kimi':saved['kimi_concurrency']}
                    self._json({'status':'saved','worker':worker,'concurrency':concurrency,
                                'concurrency_limits':{'minimum':1,'maximum':maximum},
                                'note':f"{worker.capitalize()} concurrency is now {concurrency[worker]} "
                                        f"(allowed 1-{maximum}); extra jobs wait queued for a free slot. "
                                        f"Applies to future jobs only."})
                    return
                if path == '/api/v1/settings/model':
                    # Select one allowlisted, locally verified model profile.
                    # IDs only: endpoints and credentials are never accepted,
                    # shown, or stored here. Invalid, unknown, or unverifiable
                    # IDs are rejected and never write settings.
                    worker = body.get('worker')
                    if worker not in ('qwen','kimi'):
                        return self._error(400,'INVALID_REQUEST','Worker must be qwen or kimi.')
                    try:
                        selected = validate_model_profile_selection(worker, body.get('model'))
                    except SettingsError as exc:
                        return self._error(400,'INVALID_REQUEST',str(exc))
                    try:
                        save_settings(controller.runtime, **{f'{worker}_model_profile': selected})
                    except SettingsError:
                        # Unsafe/malformed existing config or runtime directory;
                        # nothing was overwritten and no detail is exposed.
                        return self._error(500,'LOCAL_ACTION_FAILED','The requested local action failed safely.')
                    snapshot = controller.provider_snapshot
                    if isinstance(snapshot, dict) and isinstance(snapshot.get(worker), dict):
                        # Keep the cached provider card consistent with the new
                        # saved selection without re-running local CLI checks.
                        snapshot[worker]['requested_model'] = selected
                    self._json({'status':'saved','worker':worker,'models':controller.model_catalog(),
                                'note':f"{worker.capitalize()} model profile is now {selected}. Only verified "
                                        f"local profiles are selectable; endpoints and credentials are unchanged. "
                                        f"Applies to future jobs only."})
                    return
                if path.startswith('/api/v1/jobs/') and path.endswith('/cancel'):
                    job_id = path.split('/')[-2]
                    try: job_id = str(uuid.UUID(job_id))
                    except ValueError: return self._error(400,'INVALID_JOB_ID','Job ID must be a UUID.')
                    job = controller.store.get_job(job_id)
                    if not job: return self._error(404,'NOT_FOUND','Job was not found.')
                    if job.get('status') not in ('running', 'queued'):
                        return self._error(409,'JOB_NOT_RUNNING','Only running or queued jobs can be cancelled.')
                    thread = threading.Thread(target=self._cancel_safely,args=(job_id,),daemon=True)
                    thread.start()
                    self._json({'job_id':job_id,'status':'cancellation_requested'},202); return
                if path == '/api/v1/cleanup/confirm':
                    if body.get('confirm') is not True: return self._error(400,'CONFIRMATION_REQUIRED','Explicit confirmation is required.')
                    result = controller.cleanup_confirm()
                    self._json({'status':'completed','retention_days':result['retention_days'],
                                'deleted_count':result['deleted_count'],'protected_count':result['protected_count']}); return
                if path == '/api/v1/cleanup/preview':
                    result = controller.cleanup_preview()
                    self._json({'status':'dry_run','retention_days':result['retention_days'],
                                'eligible_count':result['eligible_count'],'protected_count':result['protected_count']}); return
                self._error(404,'NOT_FOUND','API endpoint not found.')
            except RuntimeError as exc:
                self._error(409,'ACTION_NOT_AVAILABLE',redact(str(exc))[0])
            except LookupError as exc:
                self._error(404,'NOT_FOUND',str(exc))
            except (OSError, ValueError, sqlite3.Error):
                self._error(500,'LOCAL_ACTION_FAILED','The requested local action failed safely.')

        @staticmethod
        def _cancel_safely(job_id):
            try: controller.cancel_job(job_id)
            except Exception: pass

    return Handler


class LocalHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False


def serve(host=DEFAULT_HOST, port=DEFAULT_PORT):
    if host != '127.0.0.1' or not (1 <= int(port) <= 65535):
        raise ValueError('Dashboard may bind only to 127.0.0.1 and a valid TCP port.')
    controller = DashboardController(port=int(port))
    server = LocalHTTPServer((host,int(port)),make_handler(controller))
    actual_host = server.server_address[0]
    if actual_host != '127.0.0.1':
        server.server_close()
        raise OSError('Dashboard did not bind to IPv4 loopback.')
    print(f'Zorava Dashboard\nhttp://127.0.0.1:{port}/\nPress Ctrl+C to stop.', flush=True)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
        controller.shutdown_owned_tests()
