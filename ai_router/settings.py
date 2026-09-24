"""User-private router runtime settings (saved budget presets; no credentials).

The settings file lives in the router runtime directory as ``settings.json``.
It stores only local operational defaults such as the Qwen isolated-edit
tool-call budget. It never stores provider credentials or endpoints.
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

# Introspectable field schema for the CLI and the upcoming dashboard Settings
# view. Values are validated with validate_budget before any write.
SETTINGS_FIELDS = {
    'qwen_coding_max_tool_calls': {
        'type': 'integer',
        'default': DEFAULT_QWEN_CODING_BUDGET,
        'minimum': UNLIMITED_TOOL_CALLS,
        'maximum': MAX_TOOL_CALLS_LIMIT,
        'unlimited_value': UNLIMITED_TOOL_CALLS,
        'applies_to': 'qwen isolated-edit jobs without an explicit max_tool_calls',
        'description': 'Saved Qwen coding tool-call budget; -1 means unlimited.',
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

    A missing file yields defaults. A symlink, unsafe ownership/permissions,
    malformed JSON, or out-of-range values raise SettingsError; bad
    configuration is never silently ignored or overwritten.
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
    value = data.get('qwen_coding_max_tool_calls', DEFAULT_QWEN_CODING_BUDGET)
    try:
        defaults['qwen_coding_max_tool_calls'] = validate_budget(value)
    except SettingsError as exc:
        raise SettingsError(f'Settings file contains an invalid budget: {exc}') from None
    return defaults


def load_qwen_coding_budget(runtime: Path) -> int:
    """Saved Qwen isolated-edit tool-call budget; -1 (unlimited) when unset."""
    return load_settings(runtime)['qwen_coding_max_tool_calls']


def save_settings(runtime: Path, *, qwen_coding_max_tool_calls) -> dict:
    """Atomically persist a budget preset with 0600 permissions.

    Existing configuration is validated first; malformed or unsafe files are
    never silently overwritten.
    """
    runtime = Path(runtime)
    budget = validate_budget(qwen_coding_max_tool_calls)
    try:
        info = runtime.stat()
    except OSError:
        raise SettingsError('Router runtime directory is unavailable.') from None
    if (runtime.is_symlink() or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700):
        raise SettingsError('Router runtime directory is missing or unsafe.')
    path = settings_path(runtime)
    if path.is_symlink() or path.exists():
        load_settings(runtime)  # raises on unsafe/malformed existing config
    payload = json.dumps({'schema_version': SETTINGS_VERSION,
                          'qwen_coding_max_tool_calls': budget},
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
