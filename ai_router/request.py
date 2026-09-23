"""Request parsing and canonical working-directory validation."""

from dataclasses import dataclass
import json
from pathlib import Path
import uuid


MAX_REQUEST_BYTES = 320 * 1024
MAX_TASK_BYTES = 32 * 1024
MAX_CONTEXT_BYTES = 256 * 1024
DEFAULT_TIMEOUT = {'qwen': 300, 'kimi': 600}
MIN_TIMEOUT = 10
MAX_TIMEOUT = 1800


class RequestError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class WorkerRequest:
    worker: str
    task: str
    context: str
    cwd: Path
    mode: str
    timeout_seconds: int
    parent_job_id: str | None = None
    delegation_group_id: str | None = None


def _safe_id(value, name):
    if value is None:
        return None
    try:
        return str(uuid.UUID(value)) if isinstance(value, str) else None
    except (ValueError, AttributeError):
        return None


def parse_request(raw: bytes, allowed_roots: tuple[Path, ...]) -> WorkerRequest:
    if len(raw) > MAX_REQUEST_BYTES:
        raise RequestError('REQUEST_TOO_LARGE', 'Request exceeds the size limit.')
    try:
        payload = json.loads(raw.decode('utf-8'))
    except (UnicodeError, ValueError):
        raise RequestError('INVALID_REQUEST', 'Request must be valid UTF-8 JSON.') from None
    if not isinstance(payload, dict):
        raise RequestError('INVALID_REQUEST', 'Request must be a JSON object.')
    if payload.get('worker') not in ('qwen', 'kimi'):
        raise RequestError('INVALID_WORKER', 'Worker must be qwen or kimi.')
    worker = payload['worker']
    if payload.get('mode', 'read-only') != 'read-only':
        raise RequestError('INVALID_MODE', 'Only read-only mode is enabled.')
    task = payload.get('task')
    context = payload.get('context', '')
    if not isinstance(task, str) or not task.strip() or len(task.encode('utf-8')) > MAX_TASK_BYTES:
        raise RequestError('INVALID_TASK', 'Task must be nonempty and within the size limit.')
    if not isinstance(context, str) or len(context.encode('utf-8')) > MAX_CONTEXT_BYTES:
        raise RequestError('REQUEST_TOO_LARGE', 'Context exceeds the size limit.')
    timeout = payload.get('timeout_seconds', DEFAULT_TIMEOUT[worker])
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not MIN_TIMEOUT <= timeout <= MAX_TIMEOUT:
        raise RequestError('INVALID_TIMEOUT', 'Timeout must be between 10 and 1800 seconds.')
    cwd = payload.get('cwd')
    if not isinstance(cwd, str) or '\x00' in cwd:
        raise RequestError('PATH_NOT_ALLOWED', 'Working directory is invalid.')
    try:
        resolved = Path(cwd).resolve(strict=True)
        if not resolved.is_dir():
            raise OSError
    except (OSError, RuntimeError):
        raise RequestError('PATH_ACCESS_ERROR', 'Working directory must exist and be accessible.') from None
    roots = []
    for root in allowed_roots:
        try:
            roots.append(root.resolve(strict=True))
        except (OSError, RuntimeError):
            continue
    if not any(resolved == root or root in resolved.parents for root in roots):
        raise RequestError('PATH_NOT_ALLOWED', 'Working directory is outside configured project roots.')
    parent = _safe_id(payload.get('parent_job_id'), 'parent_job_id')
    group = _safe_id(payload.get('delegation_group_id'), 'delegation_group_id')
    if payload.get('parent_job_id') is not None and parent is None:
        raise RequestError('INVALID_REQUEST', 'Parent job ID must be a UUID.')
    if payload.get('delegation_group_id') is not None and group is None:
        raise RequestError('INVALID_REQUEST', 'Delegation group ID must be a UUID.')
    return WorkerRequest(worker, task, context, resolved, 'read-only', timeout, parent, group)
