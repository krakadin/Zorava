"""Common adapter contract; provider authentication stays with each CLI."""

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ai_router.request import WorkerRequest


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
