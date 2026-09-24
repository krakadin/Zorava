# Operations

## Normal use

Start Claude normally with `claude`. When useful, ask Claude to delegate a bounded investigation to Kimi. Claude invokes `ai-worker` synchronously, receives JSON, and remains responsible for checking the findings. The dashboard is deferred and is not required.

## Commands

```bash
ai-worker preflight                 # local executable/version/config metadata only
ai-worker preflight --json
ai-worker test kimi                 # explicit small live provider call
ai-worker test kimi --json
printf '%s' 'Investigate the configuration loader.' |
  ai-worker delegate kimi --cwd "$PWD" --json
printf '%s' 'Implement the requested change in this clean Git checkout; do not run tests.' |
  ai-worker delegate kimi --cwd "$PWD" --mode isolated-edit --json
ai-worker jobs
ai-worker show JOB_UUID
ai-worker diff JOB_UUID --json
ai-worker cancel JOB_UUID
ai-worker discard JOB_UUID --confirm  # permanently removes this job's isolated worktree/history
```

Read-only is the default. Use `isolated-edit` only when the user explicitly asks Kimi to implement a change. It requires a clean primary Git checkout and Linux Landlock; otherwise the job fails without starting Kimi. Delegation task text is read from stdin. The exact supported timeout range is 10–1800 seconds. Default Kimi timeout is 600 seconds. Allowed working directories must canonicalize beneath `/home/krakadin/myDev`.

An edit job creates a detached worktree at `~/.local/state/ai-workers/jobs/JOB_UUID/worktree` and runs Kimi with path-scoped MCP tools only. No tests or shell commands are run by Kimi. The returned `workspace` and `diff` are proposals for Claude to review; they are not applied to the primary checkout. Use `ai-worker diff JOB_UUID` to inspect again. To remove the job worktree and its Kimi session history, review the diff first and then explicitly run `ai-worker discard JOB_UUID --confirm`. Without `--confirm`, discard only describes the deletion. Do not use discard if you need to retain the proposal.

## Health interpretation

`preflight` verifies the pinned executable and reports CLI version, configured model, provider hostname, and that auth configuration is owned by Kimi. It does not verify OAuth or make a provider call. A successful `test kimi` is evidence of a live response at that time, not a lasting guarantee. The Kimi 2.0.2 smoke test completed on 2026-09-23; requested model was `kimi-code/k3`, marker matched, duration about 12.5 seconds. The reported model was not supplied in the CLI output.

## Errors

- `AUTH_ERROR`: Kimi rejected its managed authentication. Use the official Kimi CLI authentication flow; ai-router never captures login credentials.
- `RATE_LIMITED` / `QUOTA_OR_BILLING`: provider reported a limit; do not repeatedly retry.
- `TIMEOUT`: worker exceeded its requested limit; owned process group is terminated.
- `WORKER_BUSY`: another Kimi worker is active.
- `INVALID_OUTPUT`: the CLI output did not match the expected stream-JSON shape; rerun `preflight` and inspect the job's sanitized details.
- `PATH_NOT_ALLOWED`: working directory is outside configured roots.
- `PROJECT_DIRTY`: isolated editing requires a clean primary checkout; commit or otherwise preserve your changes yourself, then retry.
- `SANDBOX_UNAVAILABLE`: Landlock could not be enabled; no less-restricted fallback is used.
- `WORKTREE_REMOVE_FAILED`: explicit discard could not remove the worktree safely.
- `CONFIG_ERROR`: local Kimi executable/profile/runtime setup needs attention.

## Tests and versions

Offline tests: `/usr/bin/python3 -B -m unittest discover -s tests`. This never calls a provider. Run `ai-worker doctor` is not implemented yet; use `preflight` and the explicit live test instead.

Tested locally: Python standard library; Kimi CLI 2.0.2 at `/home/krakadin/.kimi-code/bin/kimi`; Kimi Code provider host `api.kimi.com`; requested CLI model `kimi-code/k3` (API model `k3`). Two controlled isolated-edit tests completed on 2026-09-24 UTC (25.6s and 35.3s), each changed only `hello.txt` in a disposable worktree, returned the expected diff, and left the primary checkout clean. Both fixtures and job worktrees were explicitly discarded afterward. Qwen and Claude live provider tests are not performed by ai-router.

## Rollback

To disable the Claude integration, remove `~/.claude/skills/delegate-workers/SKILL.md` and its now-empty directory, then remove the `~/.local/bin/ai-worker` symlink. The source repository and `~/.local/state/ai-workers` job history can be archived or removed separately after review. Do not remove or alter Claude, Qwen, or Kimi installations or their provider-owned history/credentials. A rotated/revoked credential must never be restored.
