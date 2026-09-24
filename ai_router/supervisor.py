"""Synchronous process supervisor shared by CLI and dashboard entry points."""

from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import re
import selectors
import signal
import sqlite3
import subprocess
import threading
import time
import uuid

from .request import parse_request, WorkerRequest
from .security import MAX_TEXT, redact
from .state import StateStore


MAX_STDOUT = 2 * 1024 * 1024
MAX_STDERR = 2 * 1024 * 1024
TERMINATE_GRACE_SECONDS = 2.0
POLL_SECONDS = 0.1
_BUSY: set[str] = set()
_BUSY_LOCK = threading.Lock()
_ERROR_PATTERNS = (
    ('AUTH_ERROR', re.compile(r'401|unauthori[sz]ed|invalid[_ -]?api[_ -]?key|auth(?:entication)?[_ -]?(?:failed|required|expired)|login required|token expired', re.I)),
    ('RATE_LIMITED', re.compile(r'429|rate.?limit|too many requests', re.I)),
    ('QUOTA_OR_BILLING', re.compile(
        r'quota|billing|credits?.*(?:empty|exhausted|insufficient)|'
        r'(?:5[- ]hour|weekly|monthly).{0,50}(?:usage )?limit|'
        r'(?:usage )?limit.{0,50}(?:5[- ]hour|weekly|monthly)|'
        r'insufficient account balance', re.I)),
    ('MODEL_UNAVAILABLE', re.compile(r'model.*(?:not found|unavailable|unsupported)', re.I)),
    ('CONTEXT_TOO_LARGE', re.compile(r'context.*(?:too large|exceed|max(?:imum)? length)', re.I)),
    ('NETWORK_ERROR', re.compile(r'ECONNRESET|ENOTFOUND|ETIMEDOUT|connection refused|network error', re.I)),
)


@dataclass
class RunResult:
    job_id: str
    worker: str
    requested_model: str
    worker_version: str
    status: str
    started_at: str | None
    completed_at: str
    duration_ms: int
    exit_code: int | None
    result: str | None
    reported_model: str | None
    error: dict | None
    result_truncated: bool = False

    def json(self):
        return {'schema_version':1,'job_id':self.job_id,'worker':self.worker,
                'requested_model':self.requested_model,'worker_version':self.worker_version,
                'reported_model':self.reported_model,
                'status':self.status,'started_at':self.started_at,'completed_at':self.completed_at,
                'duration_ms':self.duration_ms,'exit_code':self.exit_code,'result':self.result,
                'error':self.error,'result_truncated':self.result_truncated}


class Supervisor:
    def __init__(self, runtime: Path, allowed_roots: tuple[Path, ...], adapters: dict,
                 stdout_limit=MAX_STDOUT, stderr_limit=MAX_STDERR):
        self.runtime = runtime
        self.allowed_roots = allowed_roots
        self.adapters = adapters
        self.stdout_limit = stdout_limit
        self.stderr_limit = stderr_limit
        self.state = StateStore(runtime/'workers.db')
        for directory in ('jobs','logs','tmp','locks'):
            target = runtime/directory
            target.mkdir(mode=0o700, exist_ok=True)
            if target.is_symlink() or target.stat().st_uid != os.getuid() or (target.stat().st_mode & 0o777) != 0o700:
                raise RuntimeError('Unsafe runtime subdirectory permissions.')

    @staticmethod
    def _proc_start(pid: int, expected_pgid: int) -> str | None:
        try:
            stat_path = Path('/proc')/str(pid)/'stat'
            stat_text = stat_path.read_text()
            fields = stat_text[stat_text.rfind(')')+2:].split()
            proc_pgid = int(fields[2])
            start = fields[19]
            if proc_pgid != expected_pgid or stat_path.stat().st_uid != os.getuid():
                return None
            return start
        except (OSError,ValueError,IndexError):
            return None

    @staticmethod
    def _kill_group(pid: int, start: str, sig: int) -> bool:
        current = Supervisor._proc_start(pid, pid)
        if current is None or current != start:
            return False
        try:
            os.killpg(pid, sig)
            return True
        except ProcessLookupError:
            return False

    @staticmethod
    def _group_exists(pgid: int) -> bool:
        try:
            os.killpg(pgid,0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    def cancel(self, job_id: str) -> tuple[bool, str]:
        try:
            job_id = str(uuid.UUID(job_id))
        except (ValueError,AttributeError):
            return False, 'INVALID_JOB_ID'
        job = self.state.get_job(job_id)
        if job is None:
            return False, 'NOT_FOUND'
        if job['status'] != 'running':
            return False, 'NOT_RUNNING'
        if not self.state.request_cancel(job_id):
            return False, 'NOT_RUNNING'
        pid, start = job.get('pid'), job.get('process_start')
        if not isinstance(pid,int) or not isinstance(start,str):
            return True, 'CANCEL_REQUESTED_OWNED_PROCESS_UNVERIFIED'
        if not self._kill_group(pid,start,signal.SIGTERM):
            return True, 'CANCEL_REQUESTED_PROCESS_ALREADY_EXITED'
        deadline = time.monotonic()+TERMINATE_GRACE_SECONDS
        while time.monotonic() < deadline and self._group_exists(pid):
            time.sleep(0.05)
        # This process group was verified against the persisted PID/start time
        # before SIGTERM. A surviving descendant can outlive its group leader.
        if self._group_exists(pid):
            try: os.killpg(pid,signal.SIGKILL)
            except ProcessLookupError: pass
        return True, 'CANCEL_REQUESTED'

    def run(self, raw_request: bytes, adapter_override=None, job_type='delegation') -> RunResult:
        request = parse_request(raw_request,self.allowed_roots)
        adapter = adapter_override or self.adapters.get(request.worker)
        if adapter is None or adapter.name != request.worker:
            raise ValueError('No matching worker adapter is configured.')
        worker_version = adapter.version()
        if not isinstance(worker_version, str) or not worker_version:
            raise ValueError('Worker version could not be determined.')
        job_id = str(uuid.uuid4())
        self.state.create_job(job_id,request,adapter.requested_model,adapter.role,worker_version,job_type)
        lock_path = self.runtime/'locks'/f'{request.worker}.lock'
        lock_fd = os.open(lock_path,os.O_CREAT|os.O_RDWR|os.O_CLOEXEC|os.O_NOFOLLOW,0o600)
        lock_file = os.fdopen(lock_fd,'a+b')
        started_mono = time.monotonic()
        started_at = None
        proc = None
        stdout_data = bytearray()
        stderr_data = bytearray()
        try:
            try:
                fcntl.flock(lock_file.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:
                self.state.fail_queued(job_id,'WORKER_BUSY','A job for this worker is already running.')
                return self._failure(job_id,request,adapter,'failed','WORKER_BUSY','Worker is busy.',None,started_mono,None,worker_version)
            command = adapter.build_command(request,job_id)
            if not command or any(not isinstance(part,str) or '\x00' in part for part in command):
                raise ValueError('Invalid worker command.')
            payload = adapter.build_payload(request,job_id)
            if not isinstance(payload,bytes) or len(payload)>320*1024:
                raise ValueError('Worker input exceeds the bounded payload size.')
            env = adapter.build_environment()
            if not isinstance(env,dict) or any(not isinstance(k,str) or not isinstance(v,str) or '\x00' in k+v for k,v in env.items()):
                raise ValueError('Invalid child environment.')
            proc = subprocess.Popen(command,cwd=request.cwd,env=env,stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True,close_fds=True)
            proc_start = self._proc_start(proc.pid,proc.pid)
            if proc_start is None:
                self._terminate(proc)
                raise RuntimeError('Could not verify owned worker process.')
            started_at = self._now()
            self.state.start_job(job_id,proc.pid,proc.pid,proc_start)
            result = self._communicate(proc,payload,request.timeout_seconds,job_id,stdout_data,stderr_data)
            duration = int((time.monotonic()-started_mono)*1000)
            stdout = bytes(stdout_data)
            stderr = bytes(stderr_data)
            stdout_text,_ = redact(stdout.decode('utf-8','replace'))
            stderr_text,_ = redact(stderr.decode('utf-8','replace'))
            parsed = None
            parse_error = None
            if stdout and not result['oversized']:
                try:
                    parsed = adapter.parse_output(stdout)
                except Exception:
                    parse_error = 'INVALID_OUTPUT'
            status,code,message = self._classify(result,proc.returncode,stderr_text+'\n'+stdout_text,parsed,parse_error)
            parsed_text = parsed.text if parsed else ''
            safe_text, redactions = redact(parsed_text)
            if redactions:
                self.state.event(job_id,'security redaction',f'{redactions} sensitive-looking value(s) removed')
            partial = safe_text if status != 'completed' and safe_text else None
            final = safe_text if status == 'completed' else None
            err = {'code':code,'message':message} if code else None
            self.state.finish_job(job_id,status,duration_ms=duration,exit_code=proc.returncode,
                result=final,partial_result=partial,reported_model=parsed.reported_model if parsed else None,
                error_code=code,error_message=message,usage=parsed.usage if parsed and status=='completed' else None)
            persisted=self.state.get_job(job_id)
            if persisted is not None:
                status=persisted['status']
                if status=='cancelled':
                    final=None
                    err={'code':'CANCELLED','message':'Cancelled by user.'}
                elif status!='completed' and persisted.get('error_code'):
                    err={'code':persisted['error_code'],'message':persisted.get('error_message')}
            completed = self._now()
            return RunResult(job_id,request.worker,adapter.requested_model,worker_version,status,started_at,completed,
                duration,proc.returncode,final,parsed.reported_model if parsed else None,err,
                bool(parsed and len(parsed.text)>MAX_TEXT))
        except Exception:
            if proc is not None and proc.poll() is None:
                self._terminate(proc)
            detail='Internal worker supervisor error.'
            try:
                job=self.state.get_job(job_id)
                if job and job['status']=='running':
                    self.state.finish_job(job_id,'failed',duration_ms=int((time.monotonic()-started_mono)*1000),
                                          exit_code=proc.returncode if proc else None,
                                          error_code='INTERNAL_ERROR',error_message=detail)
                else:
                    self.state.fail_queued(job_id,'INTERNAL_ERROR',detail)
            except sqlite3.Error:
                pass
            return self._failure(job_id,request,adapter,'failed','INTERNAL_ERROR',detail,
                                 proc.returncode if proc else None,started_mono,started_at,worker_version)
        finally:
            cleanup = getattr(adapter, 'cleanup', None)
            if callable(cleanup):
                try:
                    cleanup(job_id)
                except Exception:
                    try: self.state.event(job_id, 'warning', 'Temporary worker input cleanup failed.')
                    except sqlite3.Error: pass
            lock_file.close()

    def _communicate(self,proc,payload,timeout,job_id,stdout_data,stderr_data):
        selector=selectors.DefaultSelector()
        writer_error=threading.Event()
        for stream,buffer,limit,label in ((proc.stdout,stdout_data,self.stdout_limit,'stdout'),(proc.stderr,stderr_data,self.stderr_limit,'stderr')):
            os.set_blocking(stream.fileno(),False)
            selector.register(stream,selectors.EVENT_READ,(buffer,limit,label))
        def send_input():
            try:
                proc.stdin.write(payload)
                proc.stdin.flush()
            except (BrokenPipeError,OSError):
                writer_error.set()
            finally:
                try: proc.stdin.close()
                except OSError: pass

        writer=threading.Thread(target=send_input,name='ai-worker-stdin',daemon=True)
        writer.start()
        try:
            deadline=time.monotonic()+timeout
            timed_out=False
            cancelled=False
            oversized=False
            while proc.poll() is None or selector.get_map():
                if self.state.is_cancel_requested(job_id):
                    cancelled=True
                    break
                if time.monotonic()>=deadline:
                    timed_out=True
                    break
                for key,_ in selector.select(POLL_SECONDS):
                    stream=key.fileobj
                    buffer,limit,label=key.data
                    try: chunk=os.read(stream.fileno(),65536)
                    except BlockingIOError: continue
                    if not chunk:
                        selector.unregister(stream); stream.close(); continue
                    remaining=limit-len(buffer)
                    if remaining>0: buffer.extend(chunk[:remaining])
                    if len(chunk)>remaining:
                        oversized=True
                        break
                if oversized: break
            if timed_out or cancelled or oversized:
                self._terminate(proc)
            else:
                proc.wait()
            # Drain/close after the worker exits, without persisting excess data.
            for key in list(selector.get_map().values()):
                stream=key.fileobj
                while True:
                    try: chunk=os.read(stream.fileno(),65536)
                    except (BlockingIOError,OSError): break
                    if not chunk: break
                    buffer,limit,label=key.data
                    remaining=limit-len(buffer)
                    if remaining>0: buffer.extend(chunk[:remaining])
                    if len(chunk)>remaining: oversized=True
                try: selector.unregister(stream)
                except Exception: pass
                try: stream.close()
                except OSError: pass
            writer.join(timeout=1)
            return {'timed_out':timed_out,'cancelled':cancelled,'oversized':oversized,'writer_error':writer_error.is_set()}
        finally:
            selector.close()

    @staticmethod
    def _terminate(proc):
        # The PGID belongs to this started process, including surviving children.
        try: os.killpg(proc.pid,signal.SIGTERM)
        except ProcessLookupError: pass
        deadline=time.monotonic()+TERMINATE_GRACE_SECONDS
        while time.monotonic()<deadline and Supervisor._group_exists(proc.pid):
            time.sleep(0.05)
        if Supervisor._group_exists(proc.pid):
            try: os.killpg(proc.pid,signal.SIGKILL)
            except ProcessLookupError: pass
        try: proc.wait(timeout=TERMINATE_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            try: os.killpg(proc.pid,signal.SIGKILL)
            except ProcessLookupError: pass
            proc.wait()

    @staticmethod
    def _classify(result,exit_code,diagnostic,parsed,parse_error):
        if result['cancelled']: return 'cancelled','CANCELLED','Cancelled by user.'
        if result['timed_out']: return 'timed_out','TIMEOUT','Worker exceeded its time limit.'
        if result['oversized']: return 'failed','OUTPUT_LIMIT','Worker output exceeded the configured limit.'
        if exit_code != 0:
            for code,pattern in _ERROR_PATTERNS:
                if pattern.search(diagnostic):
                    status='auth_error' if code=='AUTH_ERROR' else 'rate_limited' if code=='RATE_LIMITED' else 'failed'
                    return status,code,'Worker reported a provider or execution error.'
            if parse_error: return 'invalid_output',parse_error,'Worker returned an unexpected output format.'
            return 'failed','WORKER_CRASH','Worker exited unsuccessfully.'
        if parse_error: return 'invalid_output',parse_error,'Worker returned an unexpected output format.'
        if parsed is None:
            return 'invalid_output','INVALID_OUTPUT','Worker did not produce a valid structured result.'
        return 'completed',None,None

    def _failure(self,job_id,request,adapter,status,code,message,exit_code,started_mono,started_at,worker_version='unknown'):
        try:
            current=self.state.get_job(job_id)
            if current and current['status']=='queued':
                self.state.fail_queued(job_id,code,message)
        except sqlite3.Error:
            pass
        return RunResult(job_id,request.worker,adapter.requested_model,worker_version,status,started_at,self._now(),
            int((time.monotonic()-started_mono)*1000),exit_code,None,None,{'code':code,'message':message})

    @staticmethod
    def _now():
        from .state import utc_now
        return utc_now()
