"""Qwen Code CLI adapter. Qwen Code retains ownership of Token Plan auth."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat
import subprocess

from ai_router.request import WorkerRequest
from ai_router.settings import READ_ONLY_QWEN_BUDGET, UNLIMITED_TOOL_CALLS
from .base import ParsedOutput, WorkerSetupError, common_child_environment
from .workspace import IsolatedWorkspace


QWEN_EXECUTABLE = Path('/home/krakadin/.local/bin/qwen')
QWEN_SETTINGS = Path('/home/krakadin/.qwen/settings.json')
QWEN_MODEL = 'qwen3.8-max'
QWEN_TOKEN_PLAN_URL = 'https://token-plan.maas.qwencloudapi.com/compatible-mode/v1'
QWEN_PROVIDER = 'Alibaba Model Studio Token Plan'
QWEN_PROFILE = Path(__file__).resolve().parent.parent / 'profiles' / 'qwen-researcher.md'
_VERSION = re.compile(r'(?<!\d)(\d+\.\d+\.\d+)(?!\d)')

# The built-in filesystem readers are the only tools allow-listed. The deny
# list is defense in depth for tools that are synthetic, deferred, or outside
# the core-tools allowlist semantics of this Qwen Code release.
_READ_TOOLS = ('read_file', 'list_directory', 'glob', 'grep_search')
_DENY_TOOLS = (
    'shell', 'run_shell_command', 'exec', 'write_file', 'edit', 'notebook_edit',
    'agent', 'skill', 'task_create', 'task_update', 'task_stop', 'task_list',
    'team_create', 'team_delete', 'send_message', 'create_sub_session',
    'tool_search', 'tool_call', 'web_search', 'web_fetch', 'image_gen',
    'cron_create', 'cron_list', 'cron_delete', 'loop_wakeup', 'monitor',
    'read_mcp_resource', 'enter_worktree', 'exit_worktree', 'workflow',
    'save_memory', 'request_shutdown', 'ask_user_question', 'structured_output',
    'todo_write', 'zoom_image', 'lsp', 'exit_plan_mode', 'team_plan_approval',
    'get_goal', 'update_goal', 'list_agents', 'report_findings', 'record_artifact',
)
class QwenAdapter(IsolatedWorkspace):
    name = 'qwen'
    role = 'Qwen Coder'
    requested_model = QWEN_MODEL

    def __init__(self, runtime_tmp: Path, executable: Path = QWEN_EXECUTABLE,
                 settings_path: Path = QWEN_SETTINGS):
        self._init_workspace(runtime_tmp)
        self.editor_profile = QWEN_PROFILE.with_name('qwen-editor.md')
        self.executable = executable
        self.settings_path = settings_path

    def build_environment(self, job_id: str | None = None) -> dict[str, str]:
        if job_id is not None:
            with self._dirs_lock:
                job_env = self._job_env.get(job_id)
            if job_env is not None:
                return dict(job_env)
        # Qwen reads its own credential from ~/.qwen/settings.json. In
        # particular, do not inherit DASHSCOPE_API_KEY or any other provider
        # secret from the Claude parent environment.
        return common_child_environment()

    def _prepare_editor(self, job_dir: Path, worktree: Path) -> dict[str, str]:
        self.configuration_info()
        self._write_private(job_dir / 'mcp.json', json.dumps(self._mcp_config(job_dir, worktree)).encode())
        runtime = job_dir / 'qwen-runtime'
        runtime.mkdir(mode=0o700)
        env = self.build_environment()
        env['QWEN_RUNTIME_DIR'] = str(runtime)
        return env

    def version(self) -> str:
        self._validate_executable()
        proc = subprocess.run([str(self.executable), '--version'], cwd='/home/krakadin/myDev/ai-router',
                              env=self.build_environment(), stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=10, check=False, close_fds=True)
        if proc.returncode != 0:
            raise RuntimeError('Qwen version check failed.')
        match = _VERSION.search(proc.stdout.decode('utf-8', 'replace'))
        if not match:
            raise RuntimeError('Qwen returned an unrecognized version.')
        return match.group(1)

    def _validate_executable(self) -> None:
        try:
            resolved = self.executable.resolve(strict=True)
            expected = QWEN_EXECUTABLE.resolve(strict=True)
            info = resolved.stat()
        except OSError:
            raise WorkerSetupError('CONFIG_ERROR', 'Pinned Qwen executable is unavailable.') from None
        if resolved != expected or not stat.S_ISREG(info.st_mode) or not os.access(resolved, os.X_OK):
            raise WorkerSetupError('CONFIG_ERROR', 'Pinned Qwen executable failed path verification.')

    @staticmethod
    def _profile_prompt() -> str:
        if QWEN_PROFILE.is_symlink() or not QWEN_PROFILE.is_file():
            raise WorkerSetupError('CONFIG_ERROR', 'Qwen read-only profile is missing or unsafe.')
        try:
            return QWEN_PROFILE.read_text(encoding='utf-8')
        except (OSError, UnicodeError):
            raise WorkerSetupError('CONFIG_ERROR', 'Qwen read-only profile could not be read.') from None

    def configuration_info(self) -> dict[str, str | bool]:
        """Read only safe model/endpoint metadata; never return credential fields."""
        self._validate_executable()
        try:
            if self.settings_path.is_symlink() or not self.settings_path.is_file():
                raise ValueError
            info = self.settings_path.stat()
            if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
                raise WorkerSetupError('CONFIG_ERROR', 'Qwen settings must be user-owned and private.')
            settings = json.loads(self.settings_path.read_text(encoding='utf-8'))
        except WorkerSetupError:
            raise
        except (OSError, UnicodeError, ValueError):
            raise WorkerSetupError('CONFIG_ERROR', 'Qwen settings could not be validated safely.') from None
        model = settings.get('model') if isinstance(settings, dict) else None
        model_name = model.get('name') if isinstance(model, dict) else None
        base_url = model.get('baseUrl') if isinstance(model, dict) else None
        providers = settings.get('modelProviders') if isinstance(settings, dict) else None
        openai = providers.get('openai') if isinstance(providers, dict) else None
        matches = [p for p in openai if isinstance(p, dict) and p.get('id') == QWEN_MODEL] if isinstance(openai, list) else []
        if model_name != QWEN_MODEL or base_url != QWEN_TOKEN_PLAN_URL or len(matches) != 1 or matches[0].get('baseUrl') != QWEN_TOKEN_PLAN_URL:
            raise WorkerSetupError('CONFIG_ERROR', 'Active Qwen model or endpoint differs from the reviewed Token Plan configuration.')
        env_cfg = settings.get('env') if isinstance(settings, dict) else None
        credential_present = isinstance(env_cfg, dict) and isinstance(env_cfg.get('DASHSCOPE_API_KEY'), str) and bool(env_cfg.get('DASHSCOPE_API_KEY'))
        return {'provider': QWEN_PROVIDER, 'endpoint_host': 'token-plan.maas.qwencloudapi.com',
                'base_url': QWEN_TOKEN_PLAN_URL, 'cli_model': QWEN_MODEL,
                'credential_present': credential_present}

    def build_command(self, request: WorkerRequest, job_id: str) -> list[str]:
        self.configuration_info()
        if request.mode == 'isolated-edit':
            with self._dirs_lock:
                job_dir = self._job_dirs.get(job_id)
                worktree = self._worktrees.get(job_id)
            if job_dir is None or worktree is None:
                raise WorkerSetupError('CONFIG_ERROR', 'Isolated Qwen workspace was not prepared.')
            tool_names = [f'mcp__aiworker__{name}' for name in
                          ('get_task', 'list_files', 'read_file', 'search_text', 'write_file', 'replace_in_file')]
            # Isolated-edit coding defaults to unlimited (-1); the supervisor
            # applies the saved preset or explicit per-request override first.
            inner = [str(self.executable), '--model', self.requested_model, '--safe-mode',
                     '--auth-type', 'openai',
                     '--approval-mode', 'auto-edit', '--output-format', 'json', '--no-chat-recording',
                     '--max-tool-calls', str(UNLIMITED_TOOL_CALLS if request.max_tool_calls is None else request.max_tool_calls),
                     '--max-wall-time', f'{request.timeout_seconds}s',
                     '--mcp-config', str(job_dir / 'mcp.json'),
                     '--allowed-mcp-server-names', 'aiworker',
                     '--allowed-tools', *tool_names,
                     '--exclude-tools', *_READ_TOOLS, *_DENY_TOOLS,
                     '--system-prompt', self.editor_profile.read_text(encoding='utf-8'),
                     'Call mcp__aiworker__get_task to receive and implement the delegated task.']
            sandbox = Path(__file__).resolve().parent.parent / 'bin' / 'sandbox_exec.py'
            return ['/usr/bin/python3', '-B', str(sandbox), '--write-root', str(job_dir), '--', *inner]
        # Read-only analysis keeps the fixed small budget policy unless the
        # request explicitly overrides it.
        command = [str(self.executable), '--model', self.requested_model,
                   '--approval-mode', 'plan', '--max-tool-calls', str(READ_ONLY_QWEN_BUDGET if request.max_tool_calls is None else request.max_tool_calls),
                   '--max-wall-time', f'{request.timeout_seconds}s',
                   '--output-format', 'json', '--no-chat-recording',
                   '--core-tools', *_READ_TOOLS,
                   '--exclude-tools', *_DENY_TOOLS,
                   self._profile_prompt()]
        return command

    @staticmethod
    def build_payload(request: WorkerRequest, job_id: str) -> bytes:
        return json.dumps({'job_id': job_id, 'task': request.task, 'context': request.context},
                          ensure_ascii=False, separators=(',', ':')).encode('utf-8')

    def parse_output(self, raw: bytes) -> ParsedOutput:
        try:
            events = json.loads(raw.decode('utf-8', 'strict'))
        except (UnicodeError, ValueError):
            raise ValueError('Malformed Qwen JSON output.') from None
        if not isinstance(events, list):
            raise ValueError('Unexpected Qwen JSON output shape.')
        results = [event for event in events if isinstance(event, dict) and event.get('type') == 'result']
        if not results:
            raise ValueError('Qwen JSON output contained no result event.')
        event = results[-1]
        text = event.get('result')
        if not isinstance(text, str) or not text.strip():
            raise ValueError('Qwen result event contained no text.')
        reported_model = None
        usage = event.get('usage')
        input_tokens = output_tokens = cached_tokens = None
        if isinstance(usage, dict):
            for key, attr in (('input_tokens', 'input'), ('output_tokens', 'output'), ('cached_tokens', 'cached')):
                value = usage.get(key)
                if key == 'cached_tokens' and value is None:
                    value = usage.get('cache_read_input_tokens')
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    if attr == 'input': input_tokens = value
                    elif attr == 'output': output_tokens = value
                    else: cached_tokens = value
        for item in events:
            if not isinstance(item, dict):
                continue
            model = item.get('model')
            if not model and isinstance(item.get('message'), dict):
                model = item['message'].get('model')
            if isinstance(model, str) and model.strip():
                reported_model = model.strip()[:200]
        usage_result = None
        if any(value is not None for value in (input_tokens, output_tokens, cached_tokens)):
            usage_result = {'input_tokens': input_tokens, 'output_tokens': output_tokens,
                            'cached_tokens': cached_tokens}
        return ParsedOutput(text.strip(), reported_model=reported_model, usage=usage_result)
