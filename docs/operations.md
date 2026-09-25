# Operations

## Normal use

Start Claude normally with `claude`. For user-directed work in that active session, ask Claude to delegate coding to Qwen or Kimi. Both default to independent Git worktrees and return diffs for review; request read-only mode for analysis. Claude invokes `ai-worker` synchronously, receives JSON, and remains responsible for checking the findings. Qwen is not run on a schedule or as unattended/bulk work. The optional dashboard is not required.

## Commands

```bash
ai-worker preflight                 # local executable/version/config metadata only
ai-worker preflight qwen --json
ai-worker preflight kimi --json
ai-worker preflight --json
ai-worker test qwen                 # explicit small live Token Plan request
ai-worker test kimi                 # explicit small live provider call
ai-worker test qwen --json
ai-worker test kimi --json
ai-worker dashboard                  # optional UI at http://127.0.0.1:8787/
ai-worker cleanup --dry-run          # default; preview eligible records older than 30 days
ai-worker cleanup --confirm          # permanently purge eligible records
printf '%s' 'Investigate the authentication flow; cite paths, do not modify.' |
  ai-worker delegate qwen --cwd "$PWD" --mode read-only --json
printf '%s' 'Investigate the configuration loader.' |
  ai-worker delegate kimi --cwd "$PWD" --mode read-only --json
printf '%s' 'Implement the requested change in this clean Git checkout; do not run tests.' |
  ai-worker delegate kimi --cwd "$PWD" --mode isolated-edit --json
ai-worker jobs
ai-worker show JOB_UUID
ai-worker diff JOB_UUID --json
ai-worker cancel JOB_UUID            # works for running jobs and queued jobs waiting for a slot
ai-worker discard JOB_UUID --confirm  # permanently removes this job's isolated worktree/history
ai-worker settings show                       # saved budget, slot capacity, and model profiles
ai-worker settings set qwen-concurrency 1-4   # simultaneous Qwen jobs (default: 2)
ai-worker settings set kimi-concurrency 1-4   # simultaneous Kimi jobs (default: 1)
ai-worker settings set qwen-model PROFILE_ID  # verified Qwen Token Plan profile (default: qwen3.8-max)
ai-worker settings set kimi-model PROFILE_ID  # verified managed Kimi Code alias (default: kimi-code/k3)
```

Each worker runs up to its configured slot capacity concurrently (cross-process flock slot locks under `~/.local/state/ai-workers/locks`). Jobs beyond capacity wait in `queued` state and start when a slot frees; cancelling a queued job marks it `cancelled` without ever starting a worker process. Capacity changes apply only to future jobs and can also be made from Dashboard → Settings → Worker concurrency (same 1–4 validation and private settings.json write). The dashboard Settings and provider cards show capacity plus live queued/running counts.

The dashboard is loopback-only and has Overview, Jobs, Providers, Permissions, Logs, and Settings views. It supports fixed small provider tests, cancellation, filtering history, a two-step retention purge, and an explicit read-only CLI version check. It accepts no arbitrary worker task and exposes no shell endpoint. Provider tests are live provider calls; page refreshes use cached/local state only. Start it with `ai-worker dashboard`; Ctrl+C stops the dashboard.

Provider cards also show usage. The Kimi card displays account quota windows (percent used/remaining, reset time, and an available/stale/unavailable/checking state) served from the cache-only QuotaCache; `GET /api/v1/usage` and dashboard polling are memory/local reads only. Only the CSRF/Origin-protected `POST /api/v1/usage/refresh` action asks the cache to read the already-running local Kimi server's usage endpoint once, subject to its 60-second cooldown, and it returns bounded state metadata (never the local server token or a raw provider body). Qwen exposes no reliable account-quota API, so its card shows per-job token totals (input/output/cached) summed over completed jobs in retained history and states plainly that account-level remaining quota is unavailable — no percentage is inferred or fabricated.

Provider cards show the configured model/backend and safe endpoint metadata. The UI never changes model URLs or accepts credentials. Model selection is limited to locally verified profiles on the Settings page (Dashboard → Settings → Model profiles) or `ai-worker settings set qwen-model|kimi-model`: Qwen profiles must come from a Qwen-owned provider entry on the exact reviewed Token Plan endpoint (pay-as-you-go DashScope entries are excluded), and Kimi profiles must be aliases from the managed Kimi Code provider in Kimi's own private config. Only profile IDs are stored in the private `settings.json`; the adapter re-verifies the profile/endpoint pairing at job and provider-test start and fails closed (`CONFIG_ERROR`) if a saved profile is no longer verifiable. If discovery is unavailable, only the reviewed defaults (`qwen3.8-max`, `kimi-code/k3`) remain selectable. Any other model requires a separately verified configuration change. Never put a provider token in ai-router.

Retention is 30 days for terminal ai-router jobs/results/events. The default cleanup previews; `--confirm` purges eligible records. Active jobs and per-job directories containing retained isolated-edit worktrees/session history are protected. Provider CLI histories/settings are never touched. Deletion is not guaranteed secure erasure, especially on SSDs or snapshot-backed filesystems.

Both coders default to `isolated-edit`. Use `--mode read-only` for investigation without edits. Coding requires a clean primary Git checkout and Linux Landlock; otherwise the job fails without starting the worker. Delegation task text is read from stdin. The exact supported timeout range is 10–1800 seconds. Both coders default to 1800 seconds (30 minutes), the largest accepted value; pass `--timeout SECONDS` for a shorter bound. Allowed working directories must canonicalize beneath `/home/krakadin/myDev`.

An edit job creates a detached worktree at `~/.local/state/ai-workers/jobs/JOB_UUID/worktree` and runs the chosen coder with path-scoped MCP tools only. Workers do not run tests or shell commands. The returned `workspace` and `diff` are proposals for Claude to review; they are not applied to the primary checkout. Use `ai-worker diff JOB_UUID` to inspect again. To remove the job worktree and its per-job runtime data, review the diff first and then explicitly run `ai-worker discard JOB_UUID --confirm`. Without `--confirm`, discard only describes the deletion. Do not use discard if you need to retain the proposal.

## Health interpretation

`preflight qwen` verifies the pinned Qwen executable, the saved selected model profile (default `qwen3.8-max`), the reviewed Token Plan endpoint, private Qwen settings, and credential presence. It does not make a provider call or prove authentication is currently accepted. `preflight kimi` reports local Kimi CLI/configuration metadata without verifying OAuth. A successful test is evidence of a live response at that time, not a lasting guarantee. The dashboard follows the newly returned job ID and updates its completion timestamp and last-success time. `TESTING` means queued/running; `READY` requires a completed test. A response that does not match the fixed test marker is recorded as `SMOKE_TEST_MISMATCH`. An open tab refreshes its action token before a test so a dashboard restart does not silently block it. Qwen smoke testing completed on 2026-09-24 with CLI 0.24.4, requested and reported model `qwen3.8-max`, marker matched, and duration about 33.8 seconds. The controlled read-only fixture inspection completed about 16.4 seconds later; it reported the entry point and ignored a prompt-injection instruction. Fixture hashes and repository Git status were unchanged.

CLI version reporting is separate from provider health. `installed_version` comes from the pinned CLI's own `--version` output. `update_state` is cached metadata: `unknown` (never checked, or the check failed and only a fixed error code such as `NETWORK_ERROR`, `HTTP_404`, or `INVALID_METADATA` is shown), `checking`, `up-to-date`, `update-available` (a newer stable release is published), or `stale` (the installed CLI is newer than the published version last seen, so no upgrade is implied). `Check for upgrades` on a provider card is the only action that reads a version source, and it only compares versions: ai-router never installs or upgrades a CLI. Apply an upgrade yourself through that CLI's official installer after reviewing its release notes, then re-run `ai-worker preflight WORKER` and an explicit test. Kimi worker jobs run with `KIMI_CODE_NO_AUTO_UPDATE=1`, so a worker run never changes the pinned CLI.

## Errors

- `AUTH_ERROR`: the worker's provider rejected authentication. Use the official CLI/provider authentication flow; ai-router never captures login credentials.
- `RATE_LIMITED` / `QUOTA_OR_BILLING`: provider reported a limit; do not repeatedly retry.
- `TIMEOUT`: worker exceeded its requested limit; owned process group is terminated.
- `WORKER_BUSY`: legacy; jobs beyond the configured per-worker capacity now wait `queued` and start when a slot frees instead of failing.
- `INVALID_OUTPUT`: the CLI output did not match its expected JSON shape; rerun `preflight WORKER` and inspect the job's sanitized details.
- `PATH_NOT_ALLOWED`: working directory is outside configured roots.
- `PROJECT_DIRTY`: isolated editing requires a clean primary checkout; commit or otherwise preserve your changes yourself, then retry.
- `SANDBOX_UNAVAILABLE`: Landlock could not be enabled; no less-restricted fallback is used.
- `WORKTREE_REMOVE_FAILED`: explicit discard could not remove the worktree safely.
- `CONFIG_ERROR`: local worker executable/profile/runtime setup needs attention. Qwen also fails closed if the active model or endpoint no longer matches the reviewed Token Plan configuration.

## Tests and versions

Offline tests: `/usr/bin/python3 -B -m unittest discover -s tests`. This never calls a provider. Run `ai-worker doctor` is not implemented yet; use `preflight` and the explicit live test instead.

Tested locally: Python standard library; Qwen Code 0.24.4 at `/home/krakadin/.local/bin/qwen`, host `token-plan.maas.qwencloudapi.com`, model `qwen3.8-max`; Kimi CLI 2.0.2 at `/home/krakadin/.kimi-code/bin/kimi`, host `api.kimi.com`, model `kimi-code/k3` (API model `k3`). Qwen's credential stays in Qwen's own mode-0600 settings. Its live request succeeded, but no billing/credit console was queried. The exact nested active-session Claude → Qwen invocation is the user's reasoned interpretation of Alibaba's interactive-use rules, not an express Alibaba endorsement. Two controlled isolated-edit tests completed on 2026-09-24 UTC (25.6s and 35.3s), each changed only `hello.txt` in a disposable worktree, returned the expected diff, and left the primary checkout clean. Both fixtures and job worktrees were explicitly discarded afterward. Claude direct routing remains unchanged; ai-router does not generate a Claude provider request.

## Rollback

To disable the Claude integration, remove `~/.claude/skills/delegate-workers/SKILL.md` and its now-empty directory, then remove the `~/.local/bin/ai-worker` symlink. The source repository and `~/.local/state/ai-workers` job history can be archived or removed separately after review. Do not remove or alter Claude, Qwen, or Kimi installations or their provider-owned history/credentials. A rotated/revoked credential must never be restored.
