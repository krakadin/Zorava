"""User-private router runtime settings (saved operational presets; no credentials).

The settings file lives in the router runtime directory as ``settings.json``.
It stores only local operational defaults such as the Qwen isolated-edit
tool-call budget and the per-worker job concurrency. It never stores provider
credentials or endpoints.
"""

import json
import os
from pathlib import Path
import re
import stat
import tempfile


SETTINGS_VERSION = 1
UNLIMITED_TOOL_CALLS = -1
MAX_TOOL_CALLS_LIMIT = 1_000_000
# New installs default to unlimited coding budgets; read-only and provider
# smoke tests keep their fixed small policy.
DEFAULT_QWEN_CODING_BUDGET = UNLIMITED_TOOL_CALLS
READ_ONLY_QWEN_BUDGET = 24

# Per-worker cross-process slot concurrency. Qwen supports two simultaneous
# sessions by default; Kimi stays serialized at one. Extra jobs wait queued
# until a slot frees instead of failing immediately.
MAX_CONCURRENCY = 4
DEFAULT_QWEN_CONCURRENCY = 2
DEFAULT_KIMI_CONCURRENCY = 1

# Saved model profile selections store profile IDs only -- never endpoints,
# URLs, or credentials. These defaults mirror the reviewed adapter profiles
# (workers/qwen.py QWEN_MODEL and workers/kimi.py KIMI_MODEL) and must stay
# in sync with them.
DEFAULT_QWEN_MODEL_PROFILE = 'qwen3.8-max'
DEFAULT_KIMI_MODEL_PROFILE = 'kimi-code/k3'
MODEL_PROFILE_ID_PATTERN = re.compile(r'[A-Za-z0-9][A-Za-z0-9._/-]{0,199}')

# Introspectable field schema for the CLI and the upcoming dashboard Settings
# view. Values are validated with validate_setting before any write.
SETTINGS_FIELDS = {
    'qwen_coding_max_tool_calls': {
        'type': 'integer',
        'kind': 'budget',
        'default': DEFAULT_QWEN_CODING_BUDGET,
        'minimum': UNLIMITED_TOOL_CALLS,
        'maximum': MAX_TOOL_CALLS_LIMIT,
        'unlimited_value': UNLIMITED_TOOL_CALLS,
        'applies_to': 'qwen isolated-edit jobs without an explicit max_tool_calls',
        'description': 'Saved Qwen coding tool-call budget; -1 means unlimited.',
    },
    'qwen_concurrency': {
        'type': 'integer',
        'kind': 'concurrency',
        'default': DEFAULT_QWEN_CONCURRENCY,
        'minimum': 1,
        'maximum': MAX_CONCURRENCY,
        'applies_to': 'qwen jobs across all ai-worker processes',
        'description': 'Maximum simultaneous Qwen jobs; further jobs wait queued for a slot.',
    },
    'kimi_concurrency': {
        'type': 'integer',
        'kind': 'concurrency',
        'default': DEFAULT_KIMI_CONCURRENCY,
        'minimum': 1,
        'maximum': MAX_CONCURRENCY,
        'applies_to': 'kimi jobs across all ai-worker processes',
        'description': 'Maximum simultaneous Kimi jobs; further jobs wait queued for a slot.',
    },
    'qwen_model_profile': {
        'type': 'string',
        'kind': 'model',
        'worker': 'qwen',
        'default': DEFAULT_QWEN_MODEL_PROFILE,
        'applies_to': 'future qwen jobs and provider tests',
        'description': 'Saved Qwen model profile ID; only profiles verified against the '
                       'reviewed Token Plan endpoint are accepted.',
    },
    'kimi_model_profile': {
        'type': 'string',
        'kind': 'model',
        'worker': 'kimi',
        'default': DEFAULT_KIMI_MODEL_PROFILE,
        'applies_to': 'future kimi jobs and provider tests',
        'description': 'Saved Kimi model alias; only aliases from the managed Kimi Code '
                       'provider configuration are accepted.',
    },
}

_INTEGER_TOKEN = re.compile(r'[0-9]+')


class SettingsError(RuntimeError):
    def __init__(self, message: str, code: str = 'CONFIG_ERROR'):
        super().__init__(message)
        self.code = code


def settings_path(runtime: Path) -> Path:
    return Path(runtime) / 'settings.json'


def validate_budget(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SettingsError('Budget must be an integer.', 'INVALID_REQUEST')
    if value != UNLIMITED_TOOL_CALLS and not 0 <= value <= MAX_TOOL_CALLS_LIMIT:
        raise SettingsError(
            f'Budget must be -1 (unlimited) or an integer from 0 to {MAX_TOOL_CALLS_LIMIT}.',
            'INVALID_REQUEST')
    return value


def validate_concurrency(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SettingsError('Concurrency must be an integer.', 'INVALID_REQUEST')
    if not 1 <= value <= MAX_CONCURRENCY:
        raise SettingsError(
            f'Concurrency must be an integer from 1 to {MAX_CONCURRENCY}.',
            'INVALID_REQUEST')
    return value


def validate_model_profile_id(value) -> str:
    """Structural check for a stored model profile ID (no provider I/O).

    Allowlist verification against the provider-owned configuration happens
    separately in validate_model_profile_selection before any write.
    """
    if not isinstance(value, str):
        raise SettingsError('Model profile must be a string ID.', 'INVALID_REQUEST')
    candidate = value.strip()
    if not MODEL_PROFILE_ID_PATTERN.fullmatch(candidate):
        raise SettingsError(
            'Model profile must be a short alphanumeric ID (dots, dashes, underscores, '
            'and a provider path separator are allowed).',
            'INVALID_REQUEST')
    return candidate


def validate_setting(name: str, value):
    field = SETTINGS_FIELDS.get(name)
    if field is None:
        raise SettingsError(f'Unknown setting {name!r}.', 'INVALID_REQUEST')
    if field['kind'] == 'budget':
        return validate_budget(value)
    if field['kind'] == 'model':
        return validate_model_profile_id(value)
    return validate_concurrency(value)


def parse_budget_token(text: str) -> int:
    """Parse a CLI budget token: 'unlimited' or a decimal integer 0..1000000."""
    if not isinstance(text, str):
        raise SettingsError('Budget must be unlimited or a nonnegative integer.', 'INVALID_REQUEST')
    token = text.strip().lower()
    if token == 'unlimited':
        return UNLIMITED_TOOL_CALLS
    if not _INTEGER_TOKEN.fullmatch(token):
        raise SettingsError(
            f'Budget must be unlimited or an integer from 0 to {MAX_TOOL_CALLS_LIMIT}.',
            'INVALID_REQUEST')
    return validate_budget(int(token, 10))


def parse_concurrency_token(text: str) -> int:
    """Parse a CLI concurrency token: a decimal integer 1..MAX_CONCURRENCY."""
    if not isinstance(text, str) or not _INTEGER_TOKEN.fullmatch(text.strip()):
        raise SettingsError(
            f'Concurrency must be an integer from 1 to {MAX_CONCURRENCY}.',
            'INVALID_REQUEST')
    return validate_concurrency(int(text.strip(), 10))


def _check_private_file(path: Path) -> None:
    try:
        info = path.stat()
    except OSError:
        raise SettingsError('Settings file could not be inspected safely.') from None
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077):
        raise SettingsError('Settings file must be a regular user-owned file with 0600 permissions.')


def load_settings(runtime: Path) -> dict:
    """Return saved settings merged over defaults.

    A missing file yields defaults. Older schema-1 files that predate the
    concurrency fields load with those fields at their defaults. A symlink,
    unsafe ownership/permissions, malformed JSON, or out-of-range values raise
    SettingsError; bad configuration is never silently ignored or overwritten.
    """
    path = settings_path(runtime)
    defaults = {name: field['default'] for name, field in SETTINGS_FIELDS.items()}
    if path.is_symlink():
        raise SettingsError('Settings file must not be a symbolic link.')
    if not path.exists():
        return defaults
    _check_private_file(path)
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, ValueError):
        raise SettingsError(
            'Settings file is malformed; fix or remove settings.json manually '
            '(it will not be overwritten automatically).') from None
    if not isinstance(data, dict) or data.get('schema_version') != SETTINGS_VERSION:
        raise SettingsError('Settings file has an unsupported schema; fix or remove it manually.')
    for name in SETTINGS_FIELDS:
        try:
            defaults[name] = validate_setting(name, data.get(name, defaults[name]))
        except SettingsError as exc:
            raise SettingsError(f'Settings file contains an invalid {name}: {exc}') from None
    return defaults


def load_qwen_coding_budget(runtime: Path) -> int:
    """Saved Qwen isolated-edit tool-call budget; -1 (unlimited) when unset."""
    return load_settings(runtime)['qwen_coding_max_tool_calls']


def load_worker_concurrency(runtime: Path, worker: str) -> int:
    """Saved cross-process slot capacity for one worker (qwen default 2, kimi 1)."""
    if worker not in ('qwen', 'kimi'):
        raise SettingsError(f'Unknown worker {worker!r}.', 'INVALID_REQUEST')
    return load_settings(runtime)[f'{worker}_concurrency']


_WORKER_MODEL_DEFAULTS = {'qwen': DEFAULT_QWEN_MODEL_PROFILE,
                          'kimi': DEFAULT_KIMI_MODEL_PROFILE}


def _discover_worker_model_profiles(worker: str):
    """Verified model profiles from the worker's provider-owned configuration.

    Returns None when discovery is unavailable (missing/unsafe/unparseable
    provider config). Only safe metadata (ID, display name, context size) is
    kept; URLs, keys, and credential fields never enter the result. Imported
    lazily so this module never depends on the worker adapters at import time.
    """
    if worker == 'qwen':
        from workers.qwen import verified_model_profiles as discover
    elif worker == 'kimi':
        from workers.kimi import verified_model_profiles as discover
    else:
        raise SettingsError(f'Unknown worker {worker!r}.', 'INVALID_REQUEST')
    try:
        discovered = discover()
    except Exception:
        return None
    profiles = []
    seen = set()
    for profile in discovered or []:
        if not isinstance(profile, dict):
            continue
        profile_id = profile.get('id')
        if (not isinstance(profile_id, str)
                or not MODEL_PROFILE_ID_PATTERN.fullmatch(profile_id)
                or profile_id in seen):
            continue
        seen.add(profile_id)
        display = profile.get('display_name')
        context = profile.get('context_window')
        profiles.append({
            'id': profile_id,
            'display_name': display.strip()[:120] if isinstance(display, str) and display.strip() else profile_id,
            'context_window': context if type(context) is int and context > 0 else None,
        })
    return profiles or None


def verified_model_catalog(worker: str) -> list:
    """Safe model catalog for UIs: IDs, display names, and context metadata only.

    When discovery is unavailable the catalog falls back to the worker's
    single reviewed default profile.
    """
    profiles = _discover_worker_model_profiles(worker)
    if profiles is not None:
        return profiles
    default = _WORKER_MODEL_DEFAULTS[worker]
    return [{'id': default, 'display_name': default, 'context_window': None}]


def validate_model_profile_selection(worker: str, value) -> str:
    """Verify a model profile ID against the worker's verified allowlist.

    Used before any settings write. When the provider-owned configuration
    cannot be read, only the reviewed default profile remains selectable.
    """
    candidate = validate_model_profile_id(value)
    profiles = _discover_worker_model_profiles(worker)
    if profiles is None:
        allowed = {_WORKER_MODEL_DEFAULTS[worker]}
    else:
        allowed = {profile['id'] for profile in profiles}
    if candidate not in allowed:
        raise SettingsError(
            f'{candidate!r} is not a verified {worker} model profile; only locally '
            'verified profiles are selectable.',
            'INVALID_REQUEST')
    return candidate


def load_worker_model_profile(runtime: Path, worker: str) -> str:
    """Saved model profile ID for one worker (reviewed default when unset)."""
    if worker not in ('qwen', 'kimi'):
        raise SettingsError(f'Unknown worker {worker!r}.', 'INVALID_REQUEST')
    return load_settings(runtime)[f'{worker}_model_profile']


def save_settings(runtime: Path, *, qwen_coding_max_tool_calls=None,
                  qwen_concurrency=None, kimi_concurrency=None,
                  qwen_model_profile=None, kimi_model_profile=None) -> dict:
    """Atomically persist settings with 0600 permissions.

    Only the fields given an explicit value change; every other saved field is
    preserved. Model profile selections are validated against the worker's
    verified allowlist before anything is written. Existing configuration is
    validated first; malformed or unsafe files are never silently overwritten.
    """
    runtime = Path(runtime)
    overrides = {'qwen_coding_max_tool_calls': qwen_coding_max_tool_calls,
                 'qwen_concurrency': qwen_concurrency,
                 'kimi_concurrency': kimi_concurrency,
                 'qwen_model_profile': qwen_model_profile,
                 'kimi_model_profile': kimi_model_profile}
    try:
        info = runtime.stat()
    except OSError:
        raise SettingsError('Router runtime directory is unavailable.') from None
    if (runtime.is_symlink() or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700):
        raise SettingsError('Router runtime directory is missing or unsafe.')
    path = settings_path(runtime)
    if path.is_symlink() or path.exists():
        # Raises on unsafe/malformed existing config; never overwritten silently.
        merged = load_settings(runtime)
    else:
        merged = {name: field['default'] for name, field in SETTINGS_FIELDS.items()}
    for name, value in overrides.items():
        if value is None:
            continue
        field = SETTINGS_FIELDS[name]
        if field['kind'] == 'model':
            # Selections are checked against the worker's verified allowlist
            # (provider-owned configuration) before anything is written.
            merged[name] = validate_model_profile_selection(field['worker'], value)
        else:
            merged[name] = validate_setting(name, value)
    payload = json.dumps({'schema_version': SETTINGS_VERSION, **merged},
                         ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    fd, tmp_name = tempfile.mkstemp(dir=runtime, prefix='.settings-', suffix='.tmp')
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'wb') as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except OSError:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise SettingsError('Settings could not be written safely.') from None
    return load_settings(runtime)
