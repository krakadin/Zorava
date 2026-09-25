"""Common adapter contract; provider authentication stays with each CLI."""

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Protocol

from ai_router.request import WorkerRequest


USAGE_REPORT_LIMIT = 240
_USAGE_REPORT_LINE = re.compile(r'[A-Za-z0-9 ,.:;=_/()%+\-]*')


def usage_report_marker(worker: str) -> str:
    """Per-worker marker so a reply from the wrong provider cannot pass."""
    return f'{worker.upper()}_USAGE_REPORT'


def usage_report_prompt(worker: str) -> str:
    """Fixed provider-test prompt: the provider reports its own session usage.

    The provider is asked for its native usage/stats information (for example
    token counts), not account quota, and must answer in one short marked line.
    """
    marker = usage_report_marker(worker)
    return ('This is a connectivity and usage check. Using your native usage/stats '
            'information for this session (for example token counts), reply with exactly '
            f'one line starting with {marker} followed by the reported values, like '
            f'"{marker} input=12 output=3". Do not inspect files, call tools, or include '
            'any other text.')


def validate_usage_report(worker: str, text: str) -> str | None:
    """Bounded validation of a provider-test usage report; None when acceptable.

    The reply must be a single short line carrying this worker's marker in a
    plain safe charset, so the stored test result stays sanitized and bounded.
    Reported values are the provider's own self-report; nothing here invents
    or infers account quota.
    """
    marker = usage_report_marker(worker)
    stripped = (text or '').strip()
    if not stripped.startswith(marker):
        return 'Worker response did not report its usage with the expected marker.'
    if (len(stripped) > USAGE_REPORT_LIMIT or '\n' in stripped or '\r' in stripped
            or not _USAGE_REPORT_LINE.fullmatch(stripped)):
        return 'Worker usage report did not fit the bounded single-line test format.'
    return None


class WorkerSetupError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ParsedOutput:
    text: str
    reported_model: str | None = None
    usage: dict | None = None
    partial: bool = False


class WorkerAdapter(Protocol):
    name: str
    role: str
    requested_model: str
    executable: Path

    def version(self) -> str: ...
    def build_command(self, request: WorkerRequest, job_id: str) -> list[str]: ...
    def build_payload(self, request: WorkerRequest, job_id: str) -> bytes: ...
    def parse_output(self, raw: bytes) -> ParsedOutput: ...
    def build_environment(self, job_id: str | None = None) -> dict[str, str]: ...
    def cleanup(self, job_id: str) -> None: ...


def common_child_environment(home='/home/krakadin') -> dict[str, str]:
    """Small safe baseline. Adapters must add provider credentials only if needed."""
    import os
    env = {}
    for name in ('HOME','USER','LOGNAME','LANG','TERM','TMPDIR'):
        value = os.environ.get(name)
        if value is not None and '\x00' not in value:
            env[name] = value
    for name, value in os.environ.items():
        if name.startswith('LC_') and name.replace('_','').isalnum() and '\x00' not in value:
            env[name] = value
    env['HOME'] = home
    env['USER'] = 'krakadin'
    env['LOGNAME'] = 'krakadin'
    # Avoid resolving a same-named executable from the delegated repository.
    env['PATH'] = '/home/krakadin/.local/bin:/usr/local/bin:/usr/bin:/bin'
    return env
