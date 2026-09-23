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
ai-worker jobs
ai-worker show JOB_UUID
ai-worker cancel JOB_UUID
```

All real worker requests must use `mode: read-only`; delegation task text is read from stdin. The exact supported timeout range is 10–1800 seconds. Default Kimi timeout is 600 seconds. Allowed working directories must canonicalize beneath `/home/krakadin/myDev`.

## Health interpretation

`preflight` verifies the pinned executable and reports CLI version, configured model, provider hostname, and that auth configuration is owned by Kimi. It does not verify OAuth or make a provider call. A successful `test kimi` is evidence of a live response at that time, not a lasting guarantee. The Kimi 2.0.2 smoke test completed on 2026-09-23; requested model was `kimi-code/k3`, marker matched, duration about 12.5 seconds. The reported model was not supplied in the CLI output.

## Errors

- `AUTH_ERROR`: Kimi rejected its managed authentication. Use the official Kimi CLI authentication flow; ai-router never captures login credentials.
- `RATE_LIMITED` / `QUOTA_OR_BILLING`: provider reported a limit; do not repeatedly retry.
- `TIMEOUT`: worker exceeded its requested limit; owned process group is terminated.
- `WORKER_BUSY`: another Kimi worker is active.
- `INVALID_OUTPUT`: the CLI output did not match the expected stream-JSON shape; rerun `preflight` and inspect the job's sanitized details.
- `PATH_NOT_ALLOWED`: working directory is outside configured roots.
- `CONFIG_ERROR`: local Kimi executable/profile/runtime setup needs attention.

## Tests and versions

Offline tests: `/usr/bin/python3 -B -m unittest discover -s tests`. This never calls a provider. Run `ai-worker doctor` is not implemented yet; use `preflight` and the explicit live test instead.

Tested locally: Python standard library; Kimi CLI 2.0.2 at `/home/krakadin/.kimi-code/bin/kimi`; Kimi Code provider host `api.kimi.com`; requested CLI model `kimi-code/k3` (API model `k3`). Qwen and Claude live provider tests are not performed by ai-router.

## Rollback

To disable the Claude integration, remove `~/.claude/skills/delegate-workers/SKILL.md` and its now-empty directory, then remove the `~/.local/bin/ai-worker` symlink. The source repository and `~/.local/state/ai-workers` job history can be archived or removed separately after review. Do not remove or alter Claude, Qwen, or Kimi installations or their provider-owned history/credentials. A rotated/revoked credential must never be restored.
