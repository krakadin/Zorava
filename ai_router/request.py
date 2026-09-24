"""Request parsing and canonical working-directory validation."""

from dataclasses import dataclass
import json
from pathlib import Path
import uuid

from .settings import MAX_TOOL_CALLS_LIMIT, UNLIMITED_TOOL_CALLS


MAX_REQUEST_BYTES = 320 * 1024
MAX_TASK_BYTES = 32 * 1024
MAX_CONTEXT_BYTES = 256 * 1024
# Both coders default to a 30-minute wall budget, which is also the largest
# accepted request value; MIN_TIMEOUT/MAX_TIMEOUT bounds are unchanged.
DEFAULT_TIMEOUT = {'qwen': 1800, 'kimi': 1800}
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
    max_tool_calls: int | None = None


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
    mode = payload.get('mode', 'isolated-edit')
    if mode not in ('read-only', 'isolated-edit'):
        raise RequestError('INVALID_MODE', 'Mode must be read-only or isolated-edit.')
    task = payload.get('task')
    context = payload.get('context', '')
    if not isinstance(task, str) or not task.strip() or len(task.encode('utf-8')) > MAX_TASK_BYTES:
        raise RequestError('INVALID_TASK', 'Task must be nonempty and within the size limit.')
    if not isinstance(context, str) or len(context.encode('utf-8')) > MAX_CONTEXT_BYTES:
        raise RequestError('REQUEST_TOO_LARGE', 'Context exceeds the size limit.')
    timeout = payload.get('timeout_seconds', DEFAULT_TIMEOUT[worker])
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not MIN_TIMEOUT <= timeout <= MAX_TIMEOUT:
        raise RequestError('INVALID_TIMEOUT', 'Timeout must be between 10 and 1800 seconds.')
    # An omitted Qwen max_tool_calls stays None here: the supervisor applies
    # the saved coding preset for isolated-edit jobs, while the read-only
    # adapter policy keeps its fixed small default.
    max_tool_calls = payload.get('max_tool_calls')
    if worker == 'qwen' and max_tool_calls is not None and (
            isinstance(max_tool_calls, bool) or not isinstance(max_tool_calls, int)
            or (max_tool_calls != UNLIMITED_TOOL_CALLS
                and not 0 <= max_tool_calls <= MAX_TOOL_CALLS_LIMIT)):
        raise RequestError('INVALID_REQUEST',
                           f'Qwen max_tool_calls must be -1 (unlimited) or an integer from 0 to {MAX_TOOL_CALLS_LIMIT}.')
    if worker == 'kimi' and max_tool_calls is not None:
        raise RequestError('INVALID_REQUEST', 'max_tool_calls is not supported by Kimi.')
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
    return WorkerRequest(worker, task, context, resolved, mode, timeout, parent, group, max_tool_calls)
