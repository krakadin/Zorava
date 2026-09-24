"""Read-only provider CLI version metadata for the local dashboard.

Two fixed official sources are the only URLs this module can ever request, and
only when the operator explicitly asks for a check:

* Qwen -- ``https://registry.npmjs.org/@qwen-code/qwen-code/latest`` (the JSON
  ``version`` field of the published package document)
* Kimi -- ``https://code.kimi.com/kimi-code/latest`` (a plain ``x.y.z`` line)

Every request is HTTPS-only, allowlisted by host, bounded in time and size, and
sent without environment proxies, redirects, cookies, or any Authorization or
provider credential header. A caller can never supply a URL. Nothing here
installs, upgrades, patches, or restarts a CLI: the published version is only
compared with the locally installed one and kept in an in-memory cache, so a
dashboard GET never touches the network.

``update_state`` values are:

``checking``
    A background check for this worker is in flight.
``unknown``
    The latest published version is not known (never checked, refused source,
    failed request, or unusable metadata), or the installed version could not be
    parsed as a stable ``x.y.z`` release.
``up-to-date``
    Installed equals latest.
``update-available``
    Latest is newer than installed.
``stale``
    Installed is newer than the published version last seen, so the cached
    metadata is behind this machine and cannot justify an upgrade.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import http.client
import json
import re
import threading
import urllib.error
import urllib.request
from urllib.parse import urlsplit


WORKERS = ('qwen', 'kimi')
SOURCES = {
    'qwen': 'https://registry.npmjs.org/@qwen-code/qwen-code/latest',
    'kimi': 'https://code.kimi.com/kimi-code/latest',
}
ALLOWED_HOSTS = frozenset({'registry.npmjs.org', 'code.kimi.com'})
REQUEST_TIMEOUT_SECONDS = 6.0
MAX_RESPONSE_BYTES = 8192
MAX_CONCURRENT_CHECKS = len(WORKERS)
USER_AGENT = 'ai-router-dashboard/version-check (loopback; read-only)'

STATE_UNKNOWN = 'unknown'
STATE_CHECKING = 'checking'
STATE_UP_TO_DATE = 'up-to-date'
STATE_UPDATE_AVAILABLE = 'update-available'
STATE_STALE = 'stale'
UPDATE_STATES = (STATE_UNKNOWN, STATE_CHECKING, STATE_UP_TO_DATE,
                 STATE_UPDATE_AVAILABLE, STATE_STALE)

ERROR_SOURCE_NOT_ALLOWED = 'SOURCE_NOT_ALLOWED'
ERROR_RESPONSE_TOO_LARGE = 'RESPONSE_TOO_LARGE'
ERROR_INVALID_METADATA = 'INVALID_METADATA'
ERROR_NETWORK = 'NETWORK_ERROR'
ERROR_CHECK_FAILED = 'CHECK_FAILED'

# Stable releases only: no leading zeros, no prerelease tag, no build metadata.
_STABLE_VERSION = re.compile(r'(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)')
# Failure codes are short fixed tokens, never response text.
_ERROR_CODE = re.compile(r'(?:HTTP_[0-9]{3}|[A-Z][A-Z0-9_]{0,31})')


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds')


def _safe_error_code(value) -> str | None:
    """Bound an error code before it is cached or shown; unknown text is generic."""
    if value is None:
        return None
    if isinstance(value, str):
        code = value.strip()
        if _ERROR_CODE.fullmatch(code):
            return code
    return ERROR_CHECK_FAILED


def parse_version(value) -> tuple[int, int, int] | None:
    """Strictly parse a stable ``x.y.z`` version; anything else is unknown.

    The value must already be normalized: surrounding whitespace, a ``v``
    prefix, a prerelease or build suffix, and leading zeros are all rejected.
    The only deliberate trimming of a source's own padding happens in
    ``parse_plain_semver``, which is documented there.
    """
    if not isinstance(value, str):
        return None
    match = _STABLE_VERSION.fullmatch(value)
    if not match:
        return None
    major, minor, patch = match.group(0).split('.')
    return int(major), int(minor), int(patch)


def format_version(value) -> str | None:
    """Normalized ``x.y.z`` text for a parsable version, otherwise None."""
    parsed = parse_version(value)
    return '.'.join(str(part) for part in parsed) if parsed else None


def compare_versions(left, right) -> int | None:
    """Return -1/0/1 for ``left`` versus ``right``; None when either is unknown."""
    first, second = parse_version(left), parse_version(right)
    if first is None or second is None:
        return None
    return (first > second) - (first < second)


def classify_update_state(installed, latest, *, checking: bool = False) -> str:
    """Classify an installed/latest pair into one of UPDATE_STATES."""
    if checking:
        return STATE_CHECKING
    order = compare_versions(installed, latest)
    if order is None:
        return STATE_UNKNOWN
    if order < 0:
        return STATE_UPDATE_AVAILABLE
    if order == 0:
        return STATE_UP_TO_DATE
    return STATE_STALE


def parse_npm_metadata(raw: bytes) -> str | None:
    """Latest version from an npm registry package document's ``version``."""
    if not isinstance(raw, bytes):
        return None
    try:
        document = json.loads(raw.decode('utf-8'))
    except (UnicodeError, ValueError):
        return None
    if not isinstance(document, dict):
        return None
    return format_version(document.get('version'))


def parse_plain_semver(raw: bytes) -> str | None:
    """Latest version from a body that is exactly one plain ``x.y.z`` line.

    Trimming this endpoint's own line padding is deliberate, source-specific
    normalization; afterwards the strict ``parse_version`` contract applies, so
    a body of ``"  2.0.2  \\n"`` is accepted while ``"v2.0.2"`` is not.
    """
    if not isinstance(raw, bytes):
        return None
    try:
        text = raw.decode('utf-8')
    except UnicodeError:
        return None
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) != 1:
        return None
    return format_version(lines[0])


PARSERS = {'qwen': parse_npm_metadata, 'kimi': parse_plain_semver}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect instead of following it to an unreviewed host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, 'Redirects are not followed.', headers, fp)


def build_opener():
    """HTTPS-only opener: empty proxy map, no redirects, no auth handler."""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(),
                                       urllib.request.HTTPSHandler())


def _source_url(worker) -> str | None:
    """The pinned HTTPS source for ``worker``, or None.

    The URL always comes from SOURCES, so no request path can be chosen by a
    caller, a job, or repository content; a source whose scheme or host is not
    on the fixed allowlist is refused.
    """
    url = SOURCES.get(worker) if isinstance(worker, str) else None
    if not isinstance(url, str):
        return None
    parts = urlsplit(url)
    if parts.scheme != 'https' or parts.hostname not in ALLOWED_HOSTS:
        return None
    return url


def fetch_latest_version(worker, *, opener=None, timeout=REQUEST_TIMEOUT_SECONDS,
                         max_bytes=MAX_RESPONSE_BYTES) -> tuple[str | None, str | None]:
    """Return ``(version, None)`` or ``(None, error_code)`` for one worker.

    Only the pinned source is requested. The request carries no credential, the
    response is bounded, and every failure becomes a short fixed error code
    rather than an exception or raw provider text.
    """
    url = _source_url(worker)
    if url is None:
        return None, ERROR_SOURCE_NOT_ALLOWED
    request = urllib.request.Request(url, method='GET', headers={
        'Accept': 'application/json' if worker == 'qwen' else 'text/plain',
        'User-Agent': USER_AGENT,
        'Cache-Control': 'no-store',
    })
    raw = b''
    try:
        with (opener if opener is not None else build_opener()).open(request, timeout=timeout) as response:
            status = getattr(response, 'status', None)
            if status is None:
                status = response.getcode()
            if status != 200:
                return None, f'HTTP_{status}'
            final = urlsplit(str(response.geturl() or url))
            if final.scheme != 'https' or final.hostname not in ALLOWED_HOSTS:
                return None, ERROR_SOURCE_NOT_ALLOWED
            headers = getattr(response, 'headers', None)
            declared = headers.get('Content-Length') if headers is not None else None
            try:
                if declared is not None and int(declared) > max_bytes:
                    return None, ERROR_RESPONSE_TOO_LARGE
            except (TypeError, ValueError):
                pass
            raw = response.read(max_bytes + 1)
    except urllib.error.HTTPError as exc:
        return None, f'HTTP_{exc.code}'
    except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError, ValueError):
        return None, ERROR_NETWORK
    if not isinstance(raw, (bytes, bytearray)) or len(raw) > max_bytes:
        return None, ERROR_RESPONSE_TOO_LARGE
    version = PARSERS[worker](bytes(raw))
    if version is None:
        return None, ERROR_INVALID_METADATA
    return version, None


def requested_workers(value=None) -> list[str]:
    """Validate an optional worker list from a request body.

    ``None`` means both coders. Anything else must be a short list of distinct
    known worker names; there is no way to name a URL or another target.
    """
    if value is None:
        return list(WORKERS)
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise ValueError('workers must be a JSON list of qwen/kimi.')
    if not value or len(value) > len(WORKERS):
        raise ValueError('workers must name one or both of qwen/kimi.')
    selected: list[str] = []
    for item in value:
        if not isinstance(item, str) or item not in WORKERS or item in selected:
            raise ValueError('workers must be a JSON list of distinct qwen/kimi values.')
        selected.append(item)
    return selected


@dataclass(frozen=True)
class UpdateStatus:
    """One worker's cached version metadata; safe to serialize for the UI."""

    worker: str
    installed_version: str | None
    latest_version: str | None
    update_state: str
    checking: bool
    source: str | None
    last_check_at: str | None
    last_success_at: str | None
    error: str | None

    def as_dict(self) -> dict:
        return {'installed_version': self.installed_version,
                'latest_version': self.latest_version,
                'update_state': self.update_state,
                'update_checking': self.checking,
                'update_source': self.source,
                'last_update_check_at': self.last_check_at,
                'last_update_success_at': self.last_success_at,
                'last_update_error': self.error}


@dataclass
class _Entry:
    worker: str
    latest_version: str | None = None
    checking: bool = False
    last_check_at: str | None = None
    last_success_at: str | None = None
    error: str | None = None


class UpdateRegistry:
    """In-memory version cache with explicitly requested, bounded checks.

    At most one daemon thread per worker and at most ``max_checks`` threads in
    total, so a repeated button press cannot queue unbounded outbound requests.
    A failed check keeps the previously published version and records the error
    code with its timestamp.
    """

    def __init__(self, fetcher=fetch_latest_version, max_checks=MAX_CONCURRENT_CHECKS):
        self._fetcher = fetcher
        self._max_checks = max(1, min(int(max_checks), MAX_CONCURRENT_CHECKS))
        self._lock = threading.RLock()
        self._entries = {worker: _Entry(worker) for worker in WORKERS}
        self._threads: dict[str, threading.Thread] = {}
        self._stopping = False

    def request_check(self, workers=None) -> dict:
        """Queue background checks; returns what was queued, skipped, or deferred."""
        selected = requested_workers(workers)
        queued: list[threading.Thread] = []
        result = {'requested': [], 'already_checking': [], 'deferred': []}
        with self._lock:
            if self._stopping:
                raise RuntimeError('Dashboard is shutting down.')
            for worker in selected:
                entry = self._entries[worker]
                if entry.checking or worker in self._threads:
                    result['already_checking'].append(worker)
                    continue
                if len(self._threads) >= self._max_checks:
                    result['deferred'].append(worker)
                    continue
                entry.checking = True
                entry.error = None
                thread = threading.Thread(target=self._run, args=(worker,),
                                          name=f'ai-worker-update-{worker}', daemon=True)
                self._threads[worker] = thread
                queued.append(thread)
                result['requested'].append(worker)
        for thread in queued:
            thread.start()
        return result

    def _run(self, worker: str) -> None:
        try:
            latest, error = self._fetcher(worker)
        except Exception:
            latest, error = None, ERROR_CHECK_FAILED
        error = _safe_error_code(error)
        latest = format_version(latest) if error is None else None
        finished = _utc_now()
        with self._lock:
            entry = self._entries[worker]
            entry.checking = False
            entry.last_check_at = finished
            if latest is not None:
                entry.latest_version = latest
                entry.last_success_at = finished
                entry.error = None
            else:
                entry.error = error or ERROR_CHECK_FAILED
            self._threads.pop(worker, None)

    def status(self, worker, installed_version=None) -> UpdateStatus:
        """Cached status for one worker; never performs I/O."""
        with self._lock:
            entry = self._entries.get(worker)
            if entry is None:
                raise ValueError('Unknown worker for version metadata.')
            installed = format_version(installed_version)
            return UpdateStatus(worker=worker, installed_version=installed,
                                latest_version=entry.latest_version,
                                update_state=classify_update_state(installed, entry.latest_version,
                                                                   checking=entry.checking),
                                checking=entry.checking, source=_source_url(worker),
                                last_check_at=entry.last_check_at,
                                last_success_at=entry.last_success_at, error=entry.error)

    def snapshot(self, installed_versions=None) -> dict:
        """Cached status for every worker; never performs I/O."""
        installed = installed_versions or {}
        return {worker: self.status(worker, installed.get(worker)).as_dict() for worker in WORKERS}

    def shutdown(self, timeout: float = 8.0) -> None:
        """Refuse new checks and give in-flight bounded requests time to end."""
        with self._lock:
            self._stopping = True
            threads = list(self._threads.values())
        for thread in threads:
            thread.join(timeout=timeout)
