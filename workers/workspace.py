"""Shared isolated Git worktrees, scoped file broker, and reviewable diffs."""

from dataclasses import replace
import fnmatch
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import threading

from ai_router.request import WorkerRequest
from ai_router.landlock import LandlockUnavailable, landlock_abi
from .base import WorkerSetupError


class IsolatedWorkspace:
    def _init_workspace(self, runtime_tmp: Path) -> None:
        self.runtime_tmp = runtime_tmp
        self.runtime_jobs = runtime_tmp.parent / 'jobs'
        self._job_dirs: dict[str, Path] = {}
        self._worktrees: dict[str, Path] = {}
        self._job_env: dict[str, dict[str, str]] = {}
        self._dirs_lock = threading.Lock()

    @staticmethod
    def _mcp_config(job_dir: Path, worktree: Path) -> dict:
        return {'mcpServers': {'aiworker': {
            'command': '/usr/bin/python3',
            'args': ['-B', str(Path(__file__).resolve().parent.parent / 'bin' / 'worktree_mcp.py')],
            'env': {'AI_WORKER_WORKTREE': str(worktree), 'AI_WORKER_JOB_DIR': str(job_dir)},
        }}}

    def prepare_request(self, request: WorkerRequest, job_id: str) -> WorkerRequest:
        if request.mode == 'read-only':
            return request
        try:
            landlock_abi()
        except LandlockUnavailable as exc:
            raise WorkerSetupError('SANDBOX_UNAVAILABLE', str(exc)) from None
        self._validate_executable()
        if not self.editor_profile.is_file() or self.editor_profile.is_symlink():
            raise WorkerSetupError('CONFIG_ERROR', f'{self.name.title()} editing profile is missing or unsafe.')
        repo = self._git(['rev-parse', '--show-toplevel'], request.cwd)
        if repo.returncode != 0:
            raise WorkerSetupError('NOT_A_GIT_REPOSITORY', 'Isolated editing requires a Git repository.')
        repo_root = Path(repo.stdout.decode('utf-8', 'replace').strip()).resolve(strict=True)
        dirty = self._git(['status', '--porcelain=v1', '--untracked-files=all'], repo_root)
        if dirty.returncode != 0:
            raise WorkerSetupError('GIT_ERROR', 'Could not verify repository state.')
        if dirty.stdout:
            raise WorkerSetupError('PROJECT_DIRTY', 'Isolated editing requires a clean primary checkout; no changes were made.')

        self.runtime_jobs.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.runtime_jobs.is_symlink() or self.runtime_jobs.stat().st_uid != os.getuid() or stat.S_IMODE(self.runtime_jobs.stat().st_mode) != 0o700:
            raise WorkerSetupError('CONFIG_ERROR', 'Private jobs directory is unsafe.')
        job_dir = self.runtime_jobs / job_id
        try:
            job_dir.mkdir(mode=0o700)
        except FileExistsError:
            raise WorkerSetupError('CONFIG_ERROR', 'Job runtime directory already exists.') from None
        worktree = job_dir / 'worktree'
        created_worktree = False
        try:
            result = self._git(['worktree', 'add', '--detach', str(worktree), 'HEAD'], repo_root, timeout=60)
            if result.returncode != 0:
                raise WorkerSetupError('GIT_WORKTREE_ERROR', 'Git could not create the isolated worktree.')
            created_worktree = True
            (job_dir / 'tmp').mkdir(mode=0o700)
            (job_dir / 'empty-skills').mkdir(mode=0o700)
            env = self._prepare_editor(job_dir, worktree)
            env['TMPDIR'] = str(job_dir / 'tmp')
            request_path = job_dir / 'request.json'
            payload = json.dumps({'job_id': job_id, 'cwd': str(request.cwd), 'mode': request.mode,
                                  'task': request.task, 'context': request.context},
                                 ensure_ascii=False, separators=(',', ':')).encode('utf-8')
            self._write_private(request_path, payload)
            with self._dirs_lock:
                self._job_dirs[job_id] = job_dir
                self._worktrees[job_id] = worktree
                self._job_env[job_id] = env
            return replace(request, cwd=worktree)
        except BaseException:
            if created_worktree:
                self._git(['worktree', 'remove', '--force', str(worktree)], repo_root, timeout=30)
            shutil.rmtree(job_dir, ignore_errors=True)
            raise

    @staticmethod
    def _write_private(path: Path, content: bytes) -> None:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())

    def _git(self, args: list[str], cwd: Path, timeout: int = 10):
        env = self.build_environment()
        env['GIT_OPTIONAL_LOCKS'] = '0'
        env['GIT_PAGER'] = 'cat'
        env['PAGER'] = 'cat'
        return subprocess.run(['/usr/bin/git', *args], cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
                              check=False, close_fds=True)

    def collect_edit_output(self, job_id: str) -> tuple[dict | None, str | None, bool]:
        with self._dirs_lock:
            worktree = self._worktrees.get(job_id)
        if worktree is None:
            candidate = self.runtime_jobs / job_id / 'worktree'
            worktree = candidate if candidate.is_dir() and not candidate.is_symlink() else None
        if worktree is None:
            return None, None, False
        diff_paths = self._changed_paths(worktree)
        untracked = self._git(['ls-files', '--others', '--exclude-standard', '-z'], worktree, timeout=10)
        if untracked.returncode != 0:
            return {'path':str(worktree),'error':'DIFF_UNAVAILABLE'}, None, False
        raw = bytearray()
        for name in diff_paths:
            if not self._safe_diff_path(name):
                continue
            part = self._git(['diff','--binary','--no-ext-diff','--no-textconv','HEAD','--',name],worktree,timeout=10)
            if part.returncode != 0:
                return {'path':str(worktree),'error':'DIFF_UNAVAILABLE'}, None, False
            raw.extend(part.stdout)
            if len(raw)>1024*1024: break
        untracked_paths = [part for part in untracked.stdout.decode('utf-8','replace').split('\x00') if part]
        for name in untracked_paths:
            try:
                if not self._safe_diff_path(name):
                    continue
                candidate = worktree / name
                if candidate.is_symlink():
                    continue
                target = candidate.resolve(strict=True)
                target.relative_to(worktree.resolve(strict=True))
                if not target.is_file() or target.stat().st_size > 1024*1024:
                    continue
                patch = self._git(['diff','--binary','--no-ext-diff','--no-textconv','--no-index','--','/dev/null',str(target)],worktree,timeout=10)
                # git diff --no-index returns 1 when it reports a difference.
                if patch.returncode in (0,1): raw.extend(patch.stdout)
            except (OSError,ValueError,RuntimeError):
                continue
            if len(raw)>1024*1024: break
        decoded = raw.decode('utf-8','replace')
        from ai_router.security import redact
        safe, _ = redact(decoded)
        truncated = len(raw)>128*1024 or len(safe)>=128*1024
        safe = safe[:128*1024]
        paths = sorted({p for p in (untracked_paths + diff_paths) if self._safe_diff_path(p)})
        safe_paths = [redact(p)[0] for p in paths[:500]]
        return {'path':str(worktree),'changed_files':safe_paths,'change_count':len(paths),
                'diff_available':bool(safe),'diff_truncated':truncated}, safe or None, truncated

    @staticmethod
    def _safe_diff_path(name: str) -> bool:
        path = Path(name)
        if path.is_absolute() or not name or '..' in path.parts:
            return False
        patterns = ('.env*', '*.pem', '*.key', '*.p12', '*.pfx', '*.token', '*.db',
                    '*.sqlite', '*.sqlite3', 'credentials*', 'secret*')
        blocked = {'.git','.claude','.qwen','.kimi-code'}
        return not any(part in blocked or part.startswith('.ai-router')
                       or any(fnmatch.fnmatch(part.lower(),pattern) for pattern in patterns)
                       for part in path.parts)

    def _changed_paths(self, worktree: Path) -> list[str]:
        result=self._git(['diff','--name-only','-z','HEAD','--'],worktree,timeout=10)
        if result.returncode!=0: return []
        return [part for part in result.stdout.decode('utf-8','replace').split('\x00') if part]

    def cleanup(self, job_id: str) -> None:
        with self._dirs_lock:
            job_dir = self._job_dirs.pop(job_id, None)
            self._job_env.pop(job_id, None)
            worktree = self._worktrees.get(job_id)
            if worktree is not None:
                try:
                    (job_dir / 'request.json').unlink(missing_ok=True)
                except OSError:
                    pass
        if job_dir is None:
            return
        if worktree is not None:
            # Retain edits and per-job runtime data for parent/user review.
            return
        if (job_dir.parent != self.runtime_tmp or job_dir.is_symlink()
                or job_dir.stat().st_uid != os.getuid()):
            raise RuntimeError('Refusing to remove an unsafe worker task directory.')
        shutil.rmtree(job_dir)

