"""Local-only metadata for the host/controller agents that drive Zorava.

Host agents -- Claude Code and the Codex CLI today, with a ChatGPT-hosted
controller on the roadmap -- own the conversation, delegate bounded jobs to the
managed Qwen/Kimi workers, and review what comes back. They are *not* workers:
Zorava never launches, tests, or calls a host agent, and no host provider
traffic passes through this repository.

What this module is allowed to look at, and nothing more:

* one fixed executable *name* per local host, resolved with ``shutil.which``
  inside the fixed ``SEARCH_PATH`` allowlist (the same reviewed PATH that worker
  children receive). No caller-supplied name, argument, or path is accepted, so
  a delegated repository can never select or run an executable of its own.
* only when the fixed PATH has no match, the same fixed names discovered under
  the fixed trusted NVM root ``/home/krakadin/.nvm/versions/node``: just
  ``vMAJOR.MINOR.PATCH`` installation directories and the allowlisted host
  names qualify, an active NVM bin from the inherited PATH is preferred solely
  after it validates as such, and otherwise the highest numeric installed Node
  version with the executable is chosen. Installation, bin, and executable
  paths are resolved and must stay inside the installation, so normal npm bin
  symlinks into its own ``lib`` tree work while directory or executable
  symlinks escaping it are rejected. Arbitrary inherited PATH entries and
  project directories are never searched.
* at most one bounded ``<resolved executable> --version`` probe per local host,
  with a sanitized environment, no stdin, a short timeout, and a short parsed
  version token as the only retained output. An NVM-resolved host additionally
  gets only its own validated NVM bin prepended to the sanitized PATH, so its
  ``#!/usr/bin/env node`` shebang uses the matching runtime; every other
  executable keeps the fixed base PATH unchanged.
* ``Path.is_file()`` existence checks of the two fixed delegation-skill paths.

Skill *content* is never read, and no Claude or Codex credential, OAuth, or
token file is ever opened; ``~/.codex`` is neither read nor written. There is no
network access and no provider inference anywhere in this module, and every
returned value is bounded plain metadata that is safe to cache and serialize.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import os
import re
import shutil
import subprocess

from workers.base import common_child_environment


PROJECT = Path(__file__).resolve().parents[1]
# Fixed search path, identical to the one worker children get: a same-named
# executable inside a delegated repository can never be resolved or probed.
SEARCH_PATH = '/home/krakadin/.local/bin:/usr/local/bin:/usr/bin:/bin'
# Fixed trusted NVM root, consulted only when SEARCH_PATH has no match. No
# specific Node version is ever assumed; discovered vMAJOR.MINOR.PATCH
# installation directories are validated before use.
NVM_ROOT = Path('/home/krakadin/.nvm/versions/node')
_NVM_VERSION_DIR = re.compile(r'v(\d+)\.(\d+)\.(\d+)')
PROBE_TIMEOUT_SECONDS = 5.0
MAX_OUTPUT_BYTES = 512
MAX_VERSION_LENGTH = 40
_VERSION = re.compile(r'\d+\.\d+(?:\.\d+)?')

# Reported integration state of a host agent. LOCAL means the fixed executable
# name resolved on this machine; nothing beyond that is claimed, and in
# particular no host session, login, or provider health is inferred.
STATUS_LOCAL = 'LOCAL'
STATUS_NOT_INSTALLED = 'NOT_INSTALLED'
STATUS_ROADMAP = 'ROADMAP'
HOST_STATUSES = (STATUS_LOCAL, STATUS_NOT_INSTALLED, STATUS_ROADMAP)

KIND_LOCAL_CLI = 'local-cli'
KIND_HOSTED = 'hosted'

CLAUDE_SKILL_PATH = Path('/home/krakadin/.claude/skills/delegate-workers/SKILL.md')
CODEX_SKILL_PATH = PROJECT / '.agents' / 'skills' / 'delegate-workers' / 'SKILL.md'

SKILL_DETECTED = 'Detected'
SKILL_MISSING = 'Not found'
SKILL_NOT_APPLICABLE = 'Not applicable'

CREDENTIAL_NOTE = 'Not read; owned by the host CLI'
SNAPSHOT_NOTE = ('Host/controller metadata from this machine only: fixed executable names, '
                 'fixed skill paths, no credential or token file read, and no provider or '
                 'network call.')


@dataclass(frozen=True)
class HostSpec:
    """One fixed host agent. Every field is a constant reviewed here."""

    host_id: str
    name: str
    role: str
    kind: str
    integration: str
    executable_name: str | None
    skill_path: Path | None
    detail: str


HOSTS = (
    HostSpec('claude-code', 'Claude Code', 'Host and controller', KIND_LOCAL_CLI,
             'Local CLI host', 'claude', CLAUDE_SKILL_PATH,
             'Runs on this machine and delegates through its Claude skill installed outside '
             'this repository. Claude keeps its direct Anthropic routing and its own OAuth '
             'session; Zorava never reads a Claude credential file and never calls Anthropic.'),
    HostSpec('codex-cli', 'Codex CLI', 'Host and controller', KIND_LOCAL_CLI,
             'Local CLI host', 'codex', CODEX_SKILL_PATH,
             'Runs on this machine inside a checkout of this repository and delegates through '
             'the repository skill at .agents/skills/delegate-workers/SKILL.md. Codex keeps its '
             'own ChatGPT/OpenAI login; nothing is written to ~/.codex and this entry does not '
             'imply a hosted ChatGPT session.'),
    HostSpec('chatgpt-hosted', 'ChatGPT-hosted controller', 'Host and controller (planned)',
             KIND_HOSTED, 'Hosted controller (roadmap)', None, None,
             'ROADMAP: a hosted ChatGPT controller integration is planned. There is no local '
             'executable, no credential, and no hosted session behind this entry, so it is '
             'reported as unavailable rather than as something this machine already runs.'),
)

# The only executable names this module can ever resolve or probe.
ALLOWED_EXECUTABLES = frozenset(spec.executable_name for spec in HOSTS if spec.executable_name)


def _resolved(path):
    """Fully resolved path; None when resolution itself fails (loop, error)."""
    try:
        return Path(path).resolve()
    except (OSError, RuntimeError):
        return None


def _inside(child, parent):
    """True when an already resolved ``child`` lies inside ``parent``."""
    try:
        Path(child).relative_to(parent)
        return True
    except (TypeError, ValueError):
        return False


def _valid_nvm_install(install, root=None):
    """(version tuple, resolved install) for a genuine NVM installation dir.

    Only ``vMAJOR.MINOR.PATCH`` directory names qualify, and the directory
    itself must resolve directly beneath the fixed NVM root, so a symlinked
    installation directory escaping the root is rejected. Anything else --
    non-directories, missing entries, other names -- returns None.
    """
    root = NVM_ROOT if root is None else Path(root)
    install = Path(install)
    match = _NVM_VERSION_DIR.fullmatch(install.name)
    if match is None:
        return None
    resolved_root = _resolved(root)
    resolved_install = _resolved(install)
    if resolved_root is None or resolved_install is None:
        return None
    if resolved_install.parent != resolved_root:
        return None
    try:
        if not install.is_dir():
            return None
    except OSError:
        return None
    return tuple(int(part) for part in match.groups()), resolved_install


def _valid_nvm_executable(name, install, resolved_install):
    """(bin dir, executable) for ``name`` inside a validated NVM install.

    The bin directory and the executable must both resolve inside the
    installation: a normal npm bin symlink into the installation's own ``lib``
    tree resolves there and qualifies, while directory or executable symlinks
    escaping the installation are rejected. The executable must additionally
    be an executable regular file. Any failure returns None.
    """
    bin_dir = Path(install) / 'bin'
    executable = bin_dir / name
    resolved_bin = _resolved(bin_dir)
    resolved_exe = _resolved(executable)
    if resolved_bin is None or resolved_exe is None:
        return None
    if not _inside(resolved_bin, resolved_install):
        return None
    if not _inside(resolved_exe, resolved_install):
        return None
    try:
        if not executable.is_file() or not os.access(executable, os.X_OK):
            return None
    except OSError:
        return None
    return bin_dir, executable


def _active_nvm_executable(name, root=None):
    """Executable from an active NVM bin on the inherited PATH, when valid.

    A PATH entry qualifies only when it validates as the ``bin`` directory of
    a genuine NVM installation under the fixed root; every other inherited
    PATH entry is ignored and never searched.
    """
    path = os.environ.get('PATH')
    if not path:
        return None
    for entry in path.split(os.pathsep):
        if not entry:
            continue
        bin_dir = Path(entry)
        if bin_dir.name != 'bin':
            continue
        validated = _valid_nvm_install(bin_dir.parent, root)
        if validated is None:
            continue
        offered = _valid_nvm_executable(name, bin_dir.parent, validated[1])
        if offered is not None:
            return offered[1]
    return None


def _nvm_candidates(name, root=None):
    """Every validated NVM installation offering ``name``.

    Each entry is ``(version tuple, install dir, bin dir, executable)``. Only
    ``vMAJOR.MINOR.PATCH`` directories beneath the fixed root that pass the
    symlink-containment and executability checks qualify.
    """
    root = NVM_ROOT if root is None else Path(root)
    candidates = []
    try:
        entries = sorted(Path(root).iterdir())
    except OSError:
        return candidates
    for install in entries:
        validated = _valid_nvm_install(install, root)
        if validated is None:
            continue
        offered = _valid_nvm_executable(name, install, validated[1])
        if offered is not None:
            candidates.append((validated[0], install, offered[0], offered[1]))
    return candidates


def _nvm_executable(name, root=None):
    """NVM fallback for one allowlisted name; None when nothing validates.

    An active NVM bin from the inherited PATH is preferred only after it
    validates as such; otherwise the highest numeric installed Node version
    with the executable is chosen, so the result stays deterministic.
    """
    active = _active_nvm_executable(name, root)
    if active is not None:
        return active
    candidates = _nvm_candidates(name, root)
    if not candidates:
        return None
    return max(candidates, key=lambda candidate: candidate[0])[3]


def _nvm_bin_for(executable, root=None):
    """Validated NVM bin dir of an NVM-resolved executable; None otherwise."""
    try:
        path = Path(executable)
    except TypeError:
        return None
    if path.parent.name != 'bin':
        return None
    install = path.parent.parent
    validated = _valid_nvm_install(install, root)
    if validated is None:
        return None
    offered = _valid_nvm_executable(path.name, install, validated[1])
    if offered is None or offered[1] != path:
        return None
    return path.parent


def resolve_executable(name):
    """Resolve a fixed host executable name; None when absent or untrusted.

    Names outside ``ALLOWED_EXECUTABLES`` are refused without any lookup. The
    fixed ``SEARCH_PATH`` keeps its precedence; only when no executable is
    found there does the validated NVM fallback under ``NVM_ROOT`` apply.
    Neither a request, a job, nor repository content can choose what is
    resolved.
    """
    if not isinstance(name, str) or name not in ALLOWED_EXECUTABLES:
        return None
    try:
        found = shutil.which(name, path=SEARCH_PATH)
    except (OSError, ValueError):
        return None
    if isinstance(found, str) and found:
        return Path(found)
    return _nvm_executable(name)


def probe_host_version(executable, runner=subprocess.run):
    """Bounded local ``--version`` text for an already resolved host executable.

    Only the resolved fixed path is executed, only with ``--version``, and only a
    short parsed version token survives. An NVM-resolved executable gets only
    its own validated NVM bin prepended to the sanitized PATH so its
    ``#!/usr/bin/env node`` shebang uses the matching runtime; every other
    executable keeps the fixed base PATH unchanged. Every failure becomes None:
    no exception text, environment value, or raw CLI output is returned,
    cached, or shown.
    """
    if executable is None:
        return None
    env = common_child_environment()
    nvm_bin = _nvm_bin_for(executable)
    if nvm_bin is not None:
        # Prepend only this validated NVM bin; the base PATH is untouched.
        env['PATH'] = f'{nvm_bin}{os.pathsep}{env["PATH"]}'
    try:
        proc = runner([str(Path(executable)), '--version'], cwd='/home/krakadin',
                      env=env, stdin=subprocess.DEVNULL,
                      stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                      timeout=PROBE_TIMEOUT_SECONDS, check=False)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if getattr(proc, 'returncode', 1) != 0:
        return None
    raw = getattr(proc, 'stdout', None)
    if not isinstance(raw, (bytes, bytearray)):
        return None
    match = _VERSION.search(bytes(raw)[:MAX_OUTPUT_BYTES].decode('utf-8', 'replace'))
    return match.group(0)[:MAX_VERSION_LENGTH] if match else None


def skill_state(path):
    """Fixed-path existence check for a delegation skill; content is never read."""
    if path is None:
        return SKILL_NOT_APPLICABLE
    try:
        return SKILL_DETECTED if Path(path).is_file() else SKILL_MISSING
    except OSError:
        return SKILL_MISSING


def host_entry(spec, resolver=None, probe=None):
    """Serialize one host agent into bounded, secret-free metadata."""
    resolve = resolver if resolver is not None else resolve_executable
    probe_version = probe if probe is not None else probe_host_version
    state = skill_state(spec.skill_path)
    if spec.kind == KIND_HOSTED:
        # Nothing to resolve and nothing to probe: this entry exists to report a
        # planned integration honestly instead of inventing a local executable.
        executable, version, status = None, None, STATUS_ROADMAP
    else:
        executable = resolve(spec.executable_name)
        version = probe_version(executable) if executable is not None else None
        status = STATUS_LOCAL if executable is not None else STATUS_NOT_INSTALLED
    return {'id': spec.host_id, 'name': spec.name, 'role': spec.role,
            'kind': spec.kind, 'integration': spec.integration, 'status': status,
            'executable': str(executable) if executable is not None else None,
            'executable_name': spec.executable_name,
            'version': version,
            'skill_path': str(spec.skill_path) if spec.skill_path is not None else None,
            'skill_present': state == SKILL_DETECTED,
            'skill_state': state,
            'credentials': CREDENTIAL_NOTE,
            'detail': spec.detail}


def detect_hosts(resolver=None, probe=None, generated_at=None):
    """Snapshot of every host agent; local metadata only and safe to cache.

    ``resolver`` and ``probe`` are injectable so tests never touch this
    machine's PATH or spawn a host CLI; both default to the fixed local
    implementations above.
    """
    resolve = resolver if resolver is not None else resolve_executable
    probe_version = probe if probe is not None else probe_host_version
    if generated_at is None:
        generated_at = datetime.now(timezone.utc).isoformat(timespec='seconds')
    return {'generated_at': generated_at,
            'hosts': [host_entry(spec, resolve, probe_version) for spec in HOSTS],
            'note': SNAPSHOT_NOTE}
