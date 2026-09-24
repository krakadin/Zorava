"""Read quota through an existing local Kimi server, without provider credentials."""

from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import stat
import threading
import time
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


KIMI_HOME = Path('/home/krakadin/.kimi-code')
KIMI_BINARY = KIMI_HOME / 'bin' / 'kimi'
WINDOW_NAMES = {'limit5h': '5-hour', 'limit7d': '7-day',
                'monthTotal': 'Monthly total', 'monthCode': 'Monthly coding'}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('Unexpected redirect from local Kimi server.')


def _read_owned(root, path, limit, private=False):
    """Read bounded local metadata; reject links and writable parent directories."""
    relative = path.relative_to(root)
    current = root
    for part in (None, *relative.parts[:-1]):
        if part is not None:
            current /= part
        info = current.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise ValueError('Unsafe Kimi server metadata directory.')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & (0o077 if private else 0o022) or info.st_size > limit):
            raise ValueError('Unsafe Kimi server metadata file.')
        raw = stream.read(limit + 1)
        if len(raw) > limit:
            raise ValueError('Kimi server metadata is too large.')
        return raw


def _server_port(home):
    directory = home / 'server' / 'instances'
    if not directory.exists():
        return None
    for path in sorted(directory.glob('*.json'))[:32]:
        try:
            record = json.loads(_read_owned(home, path, 8192))
            if not isinstance(record, dict) or record.get('host') not in ('127.0.0.1', 'localhost'):
                continue
            pid, port = record.get('pid'), record.get('port')
            if (type(pid) is not int or pid <= 0 or type(port) is not int
                    or not 1 <= port <= 65535):
                continue
            process = Path('/proc') / str(pid)
            if process.stat().st_uid != os.getuid():
                continue
            executable = process.joinpath('cmdline').read_bytes().split(b'\0', 1)[0].decode()
            if Path(executable).resolve(strict=True) != KIMI_BINARY.resolve(strict=True):
                continue
            return port
        except (OSError, ValueError, UnicodeError):
            continue
    return None


def parse_quota(payload):
    """Accept only documented quota windows, omitting profile/wallet information."""
    if not isinstance(payload, dict) or payload.get('code') != 0:
        raise ValueError('Kimi quota response is unavailable.')
    data = payload.get('data')
    if not isinstance(data, dict) or data.get('kind') != 'ok':
        raise ValueError('Kimi could not retrieve account quota.')
    quota = data.get('quota')
    usages = quota.get('usages') if isinstance(quota, dict) else None
    if not isinstance(usages, dict):
        raise ValueError('Kimi quota format is not supported by this version.')
    windows = []
    for key, label in WINDOW_NAMES.items():
        item = usages.get(key)
        if not isinstance(item, dict):
            continue
        ratio = item.get('usedRatio')
        if type(ratio) not in (int, float) or not math.isfinite(ratio) or not 0 <= ratio <= 1:
            continue
        reset = item.get('resetAt')
        if isinstance(reset, str):
            try:
                parsed = datetime.fromisoformat(reset.replace('Z', '+00:00'))
                reset = parsed.isoformat() if parsed.tzinfo else None
            except ValueError:
                reset = None
        else:
            reset = None
        windows.append({'name': label, 'used_ratio': ratio, 'reset_at': reset})
    if not windows:
        raise ValueError('Kimi did not report supported quota windows.')
    return {'status': 'available', 'windows': windows,
            'message': 'Account quota reported by Kimi Code.'}


def fetch_kimi_quota(home=KIMI_HOME):
    """Explicit refresh only. Never starts a server, generates tokens, or logs them."""
    unavailable = {'status': 'unavailable', 'windows': [],
                   'message': 'Start kimi web --no-open on localhost, then refresh quota.'}
    try:
        port = _server_port(home)
        if port is None:
            return unavailable
        token = _read_owned(home, home / 'server.token', 512, private=True).decode().strip()
        if not token or any(not (c.isascii() and (c.isalnum() or c in '-_')) for c in token):
            raise ValueError('Invalid local Kimi server token.')
        request = Request(f'http://127.0.0.1:{port}/api/v1/oauth/usage',
                          headers={'Authorization': 'Bearer ' + token, 'Accept': 'application/json'})
        opener = build_opener(ProxyHandler({}), _NoRedirect())
        with opener.open(request, timeout=5) as response:
            raw = response.read(65537)
            if len(raw) > 65536:
                raise ValueError('Kimi quota response is too large.')
        return parse_quota(json.loads(raw))
    except (OSError, ValueError, UnicodeError):
        return {'status': 'unavailable', 'windows': [],
                'message': 'Kimi quota unavailable. Check the local server and Kimi login.'}


QUOTA_REFRESH_COOLDOWN_S = 60.0
QUOTA_STALE_AFTER_S = 300.0
_MAX_WINDOWS = 8
_MAX_DETAIL = 200


def _utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


def _spawn_daemon(target):
    thread = threading.Thread(target=target, name='kimi-quota-refresh', daemon=True)
    thread.start()
    return thread


def _clean_windows(windows):
    """Keep only documented window fields, so nothing unexpected is cached."""
    cleaned = []
    if isinstance(windows, list):
        for item in windows[:_MAX_WINDOWS]:
            if not isinstance(item, dict):
                continue
            name, ratio, reset = item.get('name'), item.get('used_ratio'), item.get('reset_at')
            if (not isinstance(name, str) or type(ratio) not in (int, float)
                    or not math.isfinite(ratio) or not 0 <= ratio <= 1
                    or (reset is not None and not isinstance(reset, str))):
                continue
            cleaned.append({'name': name, 'used_ratio': ratio, 'reset_at': reset})
    return cleaned


class QuotaCache:
    """Thread-safe explicit-refresh cache around fetch_kimi_quota.

    No network access happens at construction or in describe(); a refresh runs
    only after request_check(), at most once per cooldown, on a daemon thread.
    A failed refresh keeps the last good windows and marks them stale; cached
    data also becomes stale with age and is never silently refetched.
    """

    def __init__(self, fetcher=fetch_kimi_quota, clock=_utc_now_iso,
                 now=time.monotonic, spawner=_spawn_daemon):
        self._fetcher = fetcher
        self._clock = clock
        self._now = now
        self._spawner = spawner
        self._lock = threading.Lock()
        self._checking = False
        self._windows = []
        self._detail = 'Kimi quota has not been checked yet.'
        self._failed = False  # latest refresh failed while older data survives
        self._last_attempt_mono = None
        self._last_attempt_at = None
        self._last_success_mono = None
        self._last_success_at = None

    def describe(self):
        """Cheap memory-only snapshot; never triggers a fetch."""
        with self._lock:
            if self._checking:
                state, detail = 'checking', 'Kimi quota refresh is in progress.'
            elif self._last_success_at is None:
                state, detail = 'unavailable', self._detail
            elif self._failed:
                state, detail = 'stale', self._detail
            elif self._now() - self._last_success_mono > QUOTA_STALE_AFTER_S:
                state = 'stale'
                detail = 'Cached Kimi quota is stale; request a refresh to update.'
            else:
                state, detail = 'available', self._detail
            return {'state': state,
                    'windows': [dict(window) for window in self._windows],
                    'detail': detail,
                    'checked_at': self._last_success_at,
                    'last_attempt_at': self._last_attempt_at,
                    'last_success_at': self._last_success_at}

    def request_check(self):
        """Start a nonblocking refresh; True only if a new attempt began."""
        with self._lock:
            if self._checking:
                return False
            mono = self._now()
            if (self._last_attempt_mono is not None
                    and mono - self._last_attempt_mono < QUOTA_REFRESH_COOLDOWN_S):
                return False
            self._checking = True
            self._last_attempt_mono = mono
            self._last_attempt_at = self._clock()
        try:
            self._spawner(self._refresh)
        except Exception:
            with self._lock:
                self._checking = False
                self._detail = 'Kimi quota refresh could not be started.'
            return False
        return True

    def _refresh(self):
        detail = 'Kimi quota refresh failed unexpectedly.'
        windows = None
        try:
            result = self._fetcher()
        except Exception:
            result = None  # never propagate raw exception text into the cache
        if isinstance(result, dict):
            message = result.get('message')
            if isinstance(message, str) and message:
                detail = message[:_MAX_DETAIL]
            if result.get('status') == 'available':
                windows = _clean_windows(result.get('windows'))
        with self._lock:
            self._checking = False
            if windows is not None:
                self._windows = windows
                self._detail = detail
                self._failed = False
                self._last_success_mono = self._now()
                self._last_success_at = self._clock()
            else:
                self._detail = detail
                if self._last_success_at is not None:
                    self._failed = True


KimiQuotaCache = QuotaCache
