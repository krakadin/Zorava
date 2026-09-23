"""Bounded redaction and output sanitization used before persistence."""

import re


MAX_TEXT = 128 * 1024
_ANSI = re.compile(r'\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))')
_PEM = re.compile(r'-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----', re.S)
_COOKIE = re.compile(r'(?im)(?:set-cookie|cookie)\s*:\s*[^\r\n]+')
_BEARER = re.compile(r'(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+')
_ASSIGNMENT = re.compile(
    r'''(?i)(["']?(?:DASHSCOPE_API_KEY|ANTHROPIC_API_KEY|ANTHROPIC_AUTH_TOKEN|KIMI_API_KEY|API[_-]?KEY|ACCESS[_-]?TOKEN|REFRESH[_-]?TOKEN|AUTHORIZATION|PASSWORD|COOKIE|SECRET|PRIVATE[_-]?KEY|CLIENT[_-]?SECRET)["']?\s*[:=]\s*)(?:["']([^"'\r\n]*)["']|((?:(?:Bearer|Basic)\s+)?[^\s,;}]+))'''
)
_QUERY_SECRET = re.compile(r'(?i)([?&](?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret)=)[^&#\s]+')
_KEY_VALUE = re.compile(r'\b(?:sk-(?:ws-|sp-)?)[A-Za-z0-9._~+/=-]{12,}\b')
_CONTROL = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')


def redact(value: str) -> tuple[str, int]:
    """Redact common credential forms; return sanitized text and count."""
    text = _ANSI.sub('', str(value))
    count = 0

    def block(match):
        nonlocal count
        count += 1
        return '[REDACTED PRIVATE KEY]'

    text = _PEM.sub(block, text)

    def cookie(match):
        nonlocal count
        count += 1
        return match.group(0).split(':', 1)[0] + ': [REDACTED]'

    text = _COOKIE.sub(cookie, text)

    # Preserve labels to help diagnose an authentication failure without
    # retaining the credential value. Count every replacement without storing
    # a copy of the matched secret in diagnostics.
    def assignment(match):
        nonlocal count
        count += 1
        return match.group(1) + '[REDACTED]'

    text = _ASSIGNMENT.sub(assignment, text)

    def bearer(match):
        nonlocal count
        count += 1
        return 'Bearer [REDACTED]'

    text = _BEARER.sub(bearer, text)

    def query(match):
        nonlocal count
        count += 1
        return match.group(1) + '[REDACTED]'

    text = _QUERY_SECRET.sub(query, text)

    def key(match):
        nonlocal count
        count += 1
        return '[REDACTED]'

    text = _KEY_VALUE.sub(key, text)
    text = _CONTROL.sub('', text)
    return text[:MAX_TEXT], count


def summarize(task: str, limit: int = 180) -> str:
    safe, _ = redact(task)
    compact = ' '.join(safe.split())
    return compact if len(compact) <= limit else compact[:limit - 1] + '…'
