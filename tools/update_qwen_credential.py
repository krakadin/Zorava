#!/usr/bin/python3
"""One-time, user-operated remediation; never used by the worker supervisor.

Accepts no credential arguments or environment-based credentials. Updates Qwen's
existing credential field, using hidden terminal input and an atomic 0600 file.
The explicit --configure-endpoint option proposes the matching supplied endpoint
and requires terminal confirmation before updating the active qwen3.8-max URLs.
No backups, network requests, provider launches, or credential logging.
"""

import getpass
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import warnings


SETTINGS = Path('/home/krakadin/.qwen/settings.json')
HISTORIES = tuple(
    SETTINGS.parent / 'projects/-home-krakadin-myDev-placeorbit-starter/chats' / name
    for name in (
        '36af78eb-9bb1-41fe-99cd-8df0545a7b24.jsonl',
        '32b706e0-884d-4374-904b-1b95da90608d.jsonl',
    )
)
MAX_SETTINGS_BYTES = 2 * 1024 * 1024
LEGACY_URL = 'https://dashscope-intl.aliyuncs.com/compatible-mode/v1'
TOKEN_PLAN_URL = 'https://token-plan.maas.qwencloudapi.com/compatible-mode/v1'
WORKSPACE_URL = 'https://ws-6ngvqa7xxgx1usvk.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1'
MODEL = 'qwen3.8-max'


class RemediationError(Exception):
    """Messages must be fixed text, never include input or parser diagnostics."""


def open_owned(path: Path):
    if path.resolve(strict=True) != path:
        raise RemediationError('Unexpected symlink; update refused.')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise RemediationError('Expected an owned regular file with mode 0600 and no hard links.')
        stream = os.fdopen(fd, 'rb')
    except BaseException:
        os.close(fd)
        raise
    return stream, info


def read_settings(path: Path):
    stream, info = open_owned(path)
    with stream:
        content = stream.read(MAX_SETTINGS_BYTES + 1)
    if len(content) > MAX_SETTINGS_BYTES:
        raise RemediationError('Settings file exceeds the update size limit.')
    return content, info


def unique_object(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise RemediationError('Duplicate configuration fields; update refused.')
        result[name] = value
    return result


def parse_settings(content: bytes):
    try:
        text = content.decode('utf-8')
        data = json.loads(text, object_pairs_hook=unique_object)
    except (UnicodeError, ValueError):
        raise RemediationError('Settings are not valid UTF-8 JSON; update refused.') from None
    if not isinstance(data, dict) or not isinstance(data.get('env'), dict):
        raise RemediationError('Expected Qwen environment configuration is missing.')
    old = data['env'].get('DASHSCOPE_API_KEY')
    if not isinstance(old, str) or not old:
        raise RemediationError('Expected existing Qwen credential field is missing.')
    return text, data, old


def history_contains(path: Path, value: bytes) -> bool:
    stream, _ = open_owned(path)
    with stream:
        carry = b''
        while chunk := stream.read(1024 * 1024):
            block = carry + chunk
            if value in block:
                return True
            carry = block[-(len(value) - 1):]
    return False


def value_span(text: str, path: tuple) -> tuple[int, int]:
    """Locate an existing JSON value without rewriting surrounding settings."""
    decoder = json.JSONDecoder()

    def whitespace(position):
        while position < len(text) and text[position] in ' \t\r\n':
            position += 1
        return position

    position = whitespace(0)
    for component in path:
        expected = '{' if isinstance(component, str) else '['
        if text[position] != expected:
            raise RemediationError('Unexpected configuration structure; update refused.')
        position = whitespace(position + 1)
        index = 0
        while text[position] not in '}]':
            if isinstance(component, str):
                member, position = decoder.raw_decode(text, position)
                position = whitespace(position)
                if text[position] != ':':
                    raise RemediationError('Unexpected configuration structure; update refused.')
                position = whitespace(position + 1)
            else:
                member = index
            if member == component:
                break
            _, position = decoder.raw_decode(text, position)
            position = whitespace(position)
            if text[position] == ',':
                position = whitespace(position + 1)
            index += 1
        else:
            raise RemediationError('Required configuration field is missing.')
    _, end = decoder.raw_decode(text, position)
    return position, end


def endpoint_for_key(replacement: str) -> tuple[str, str]:
    """Recognize documented key classes without printing any key characters.

    A prefix establishes key class only, not validity, entitlement, or workspace
    ownership. The user must confirm the proposed endpoint; a live test follows.
    """
    if replacement.startswith('sk-sp-'):
        return TOKEN_PLAN_URL, 'Token Plan subscription key'
    if replacement.startswith('sk-ws-'):
        return WORKSPACE_URL, 'Model Studio standard API key (pay-as-you-go)'
    raise RemediationError('Key type is not recognized; endpoint selection needs review.')


def update_credential(path: Path, replacement: str, histories=HISTORIES, *, endpoint=None) -> None:
    if (
        not replacement.startswith('sk-')
        or not 16 <= len(replacement) <= 4096
        or '*' in replacement
        or any(ord(char) < 33 or ord(char) > 126 for char in replacement)
    ):
        raise RemediationError('Expected a complete unmasked API key without whitespace.')
    original, before = read_settings(path)
    text, data, old = parse_settings(original)
    if replacement == old:
        raise RemediationError('Entered key matches the existing value; no update performed.')
    if any(history_contains(history, replacement.encode('utf-8')) for history in histories):
        raise RemediationError('Entered key appears in an exposed transcript; update refused.')

    changes = [(('env', 'DASHSCOPE_API_KEY'), replacement)]
    model = data.get('model', {})
    if not isinstance(model, dict):
        raise RemediationError('Unexpected model configuration; update refused.')
    if endpoint is not None:
        expected_endpoint, _ = endpoint_for_key(replacement)
        if endpoint != expected_endpoint:
            raise RemediationError('Key type and reviewed endpoint do not match; update refused.')
        known_urls = (LEGACY_URL, TOKEN_PLAN_URL, WORKSPACE_URL)
        if model.get('name') != MODEL or model.get('baseUrl') not in known_urls:
            raise RemediationError('Active model or endpoint differs from the reviewed plan.')
        if data.get('security', {}).get('auth', {}).get('selectedType') != 'openai':
            raise RemediationError('Authentication type differs from the reviewed OpenAI configuration.')
        providers = data.get('modelProviders', {}).get('openai', [])
        if not isinstance(providers, list):
            raise RemediationError('Expected OpenAI model definitions are missing.')
        indices = [i for i, entry in enumerate(providers) if isinstance(entry, dict) and entry.get('id') == MODEL]
        if len(indices) != 1:
            raise RemediationError('Active model definition is missing or ambiguous.')
        index = indices[0]
        entry = providers[index]
        if entry.get('baseUrl') not in known_urls or entry.get('envKey') != 'DASHSCOPE_API_KEY':
            raise RemediationError('Model credential reference or endpoint differs from the reviewed plan.')
        changes += [
            (('model', 'baseUrl'), endpoint),
            (('modelProviders', 'openai', index, 'baseUrl'), endpoint),
        ]
    elif replacement.startswith('sk-sp-') and model.get('baseUrl') != TOKEN_PLAN_URL:
        raise RemediationError('Token Plan key needs the reviewed --configure-endpoint update.')

    # Preserve every byte outside the explicitly selected JSON values.
    spans = []
    for field, value in changes:
        start, end = value_span(text, field)
        spans.append((start, end, json.dumps(value)))
        parent = data
        for component in field[:-1]:
            parent = parent[component]
        parent[field[-1]] = value
    for start, end, encoded in sorted(spans, reverse=True):
        text = text[:start] + encoded + text[end:]
    updated = text.encode('utf-8')
    if json.loads(updated) != data:
        raise RemediationError('Unrelated configuration would change; update refused.')

    # Staging stays beside Qwen's settings, never in ai-router or a shared /tmp
    # file. This is an atomic replacement, not a credential backup.
    fd, name = tempfile.mkstemp(prefix='.settings-key-update-', suffix='.tmp', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'wb') as stream:
            temp_info = os.fstat(stream.fileno())
            if (temp_info.st_uid, temp_info.st_gid) != (before.st_uid, before.st_gid):
                raise RemediationError('Replacement ownership would differ; update refused.')
            os.fchmod(stream.fileno(), 0o600)
            stream.write(updated)
            stream.flush()
            os.fsync(stream.fileno())
        current, current_info = read_settings(path)
        if current != original or (current_info.st_dev, current_info.st_ino) != (before.st_dev, before.st_ino):
            raise RemediationError('Settings changed during entry; retry after reviewing the change.')
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        verified, after = read_settings(path)
        if verified != updated or (after.st_uid, after.st_gid) != (before.st_uid, before.st_gid):
            raise RemediationError('Update could not be verified; inspect configuration privately.')
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    if sys.argv[1:] not in ([], ['--configure-endpoint']):
        print('Only optional --configure-endpoint is accepted. Enter the key only at the hidden terminal prompt.', file=sys.stderr)
        return 2
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        print('Run this helper directly in your own terminal. Piped input is not accepted.', file=sys.stderr)
        return 2
    os.umask(0o077)
    configure_endpoint = sys.argv[1:] == ['--configure-endpoint']
    try:
        # Never allow getpass to fall back to visible input if terminal control
        # fails. Validate the local configuration before asking for the key.
        parse_settings(read_settings(SETTINGS)[0])
        with warnings.catch_warnings():
            warnings.simplefilter('error', getpass.GetPassWarning)
            replacement = getpass.getpass('Replacement Qwen API key (hidden): ')
            confirmation = getpass.getpass('Enter it again (hidden): ')
        if replacement != confirmation:
            raise RemediationError('Entries did not match; no update performed.')
        endpoint = None
        if configure_endpoint:
            endpoint, key_type = endpoint_for_key(replacement)
            print('Key class: ' + key_type)
            print('Proposed Qwen endpoint: ' + endpoint)
            print('Fields: env.DASHSCOPE_API_KEY (hidden), model.baseUrl, qwen3.8-max modelProviders entry baseUrl.')
            print('Other models, provider metadata, Claude, Kimi, and transcript contents will not change.')
            if endpoint == TOKEN_PLAN_URL:
                print('Other saved models still use pay-as-you-go URLs; the Token Plan key will not work with those URLs.')
            print('Key class does not prove validity or workspace ownership. Confirm this matches the key you obtained.')
            if input('Approve these specific changes? Type UPDATE to apply: ') != 'UPDATE':
                print('No settings changed.')
                return 1
        update_credential(SETTINGS, replacement, HISTORIES, endpoint=endpoint)
    except RemediationError as error:
        print(str(error), file=sys.stderr)
        return 1
    except (EOFError, KeyboardInterrupt):
        print('\nInterrupted. Checkpoint A is not yet verified.', file=sys.stderr)
        return 1
    except Exception:
        # Parser/OS exception text can contain input. Never print it or a trace.
        print('Unable to complete the update safely. No credential value was printed.', file=sys.stderr)
        return 1
    print('Qwen credential updated in its existing settings file; mode 0600 verified.')
    if configure_endpoint:
        print('Only the credential and the two reviewed endpoint fields changed.')
    else:
        print('Other settings are unchanged.')
    print('No backup or provider request was made.')
    print('Tell Codex the update completed so it can verify Qwen safely.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
