# Zorava

**Local-first AI agent orchestration and operations for delegated coding work.**

**Repository:** [github.com/krakadin/Zorava](https://github.com/krakadin/Zorava) — issues and discussion live there.

Zorava is a self-hosted supervisor that lets a *host* AI agent — Claude Code and the Codex CLI today, with ChatGPT-hosted and other controllers on the roadmap — hire *specialist coding workers* such as **Qwen** and **Kimi**. Every delegated coding job runs as a bounded subprocess in its own detached Git worktree, with no shell, a path-scoped MCP file broker, and Linux Landlock write confinement; read-only analysis jobs run instead with a small fixed read-tool allowlist. Either way the worker returns a sanitized result — a report or a reviewable diff — and nothing is committed, merged, pushed, or applied to your checkout automatically.

Zorava owns the operational layer around that delegation: worker capabilities and roles, verified model profiles, bounded concurrency slots, job lifecycle state and event logs, token usage and quota reporting, installed CLI versions and upgrade checks, 30-day retention cleanup, and reviewable diffs. It all runs on your machine, in your Unix account, with no daemon, no scheduler, no model proxy, and no provider credentials.

> **Naming.** Zorava is the public product name. The code keeps its technical names: the `ai_router` Python package, the `ai-worker` command, and the `~/.local/state/ai-workers` runtime directory. Some documents in this repository still call the codebase `ai-router`.

> **Credentials stay with their providers.** Never put an API key, OAuth token, or session credential into Zorava. Claude, Qwen, and Kimi each keep their own authentication in their own configuration files. Zorava reads only safe metadata — model IDs, endpoint hostnames, and credential *presence* — and fails closed when the reviewed configuration no longer matches.

## Contents

- [The problem](#the-problem)
- [What Zorava does](#what-zorava-does)
- [What Zorava is not](#what-zorava-is-not)
- [Architecture](#architecture)
- [Host agents and supported workers](#host-agents-and-supported-workers)
- [Isolation model](#isolation-model)
- [Job lifecycle, concurrency, and lineage](#job-lifecycle-concurrency-and-lineage)
- [Quick start](#quick-start)
- [CLI reference](#cli-reference)
- [Dashboard and local HTTP API](#dashboard-and-local-http-api)
- [Settings: budgets, concurrency, model profiles](#settings-budgets-concurrency-model-profiles)
- [Usage, quotas, and CLI versions](#usage-quotas-and-cli-versions)
- [Security and privacy](#security-and-privacy)
- [Development and testing](#development-and-testing)
- [Limitations](#limitations)
- [Roadmap](#roadmap)
- [Documentation](#documentation)

## The problem

A host agent such as Claude Code or ChatGPT is good at planning, reviewing, and holding context — but a single session cannot cheaply fan implementation work out to other providers' specialist coding CLIs, and letting another model edit your working checkout directly is hard to bound, hard to audit, and hard to undo.

The naive alternatives are all worse:

- **Model proxies and routers** put a service in front of provider APIs, which means credentials, a long-running process, and routing decisions you did not ask for.
- **Unrestricted subagents** inherit your shell, your filesystem, and your environment variables.
- **Copy-paste delegation** loses the audit trail: no job record, no timing, no token usage, no diff, no reproducibility.

Zorava takes the third path seriously: keep the host agent in charge, keep each provider's own CLI and credential, and wrap the delegated job in explicit, testable operational controls.

## What Zorava does

| Capability | What it means in practice |
| --- | --- |
| **Delegated coding workers** | The Qwen Code CLI and Kimi Code CLI are launched as supervised subprocesses for one task that you pipe into `ai-worker` on stdin. The task reaches Qwen as JSON on stdin and Kimi through a short-lived private request file served by the MCP broker — never as a process argument. |
| **Isolated Git worktrees** | Each coding job gets a detached worktree at `~/.local/state/ai-workers/jobs/<job-id>/worktree`. Your primary checkout is never modified automatically. |
| **Reviewable diffs** | Jobs return a redacted, size-bounded diff (tracked changes plus new untracked files). Secret-like paths (`.env*`, `*.pem`, `*.key`, `credentials*`, `secret*`) are excluded; oversized diffs are flagged as truncated. |
| **Scoped file tools only** | Six local MCP tools: `get_task`, `list_files`, `read_file`, `search_text`, `write_file`, `replace_in_file`. No shell, no native filesystem tools, no nested agents. |
| **Landlock write confinement** | Worker writes are restricted at the kernel level to the job's private runtime directory (Kimi additionally gets its own OAuth directory so it can refresh its own token). Coding mode fails closed if Landlock is unavailable. |
| **Bounded execution** | Wall-clock timeouts (10–1800 s, default 1800), a configurable Qwen tool-call budget, request/task/context size caps (320 KiB / 32 KiB / 256 KiB), bounded captured output, and process-group cleanup on timeout or cancellation. |
| **Concurrency and queueing** | Per-worker slot locks (user-private `flock` files) allow Qwen 2 and Kimi 1 simultaneous jobs by default, each configurable 1–4. Extra jobs wait `queued` and start when a slot frees instead of failing. |
| **Verified model profiles** | Saved profile IDs (`qwen3.8-max`, `kimi-code/k3`) are checked against an allowlist discovered from the provider's own configuration and re-verified when a job or provider test starts. |
| **Job history and event logs** | SQLite job/event state with sanitized results and redacted text under a 0700 runtime root; inspect with `jobs`, `show`, `diff`, or the dashboard Logs view. |
| **Usage and quota reporting** | Per-job token totals (input/output/cached) and, for Kimi, account quota windows refreshed only by an explicit, cooldown-limited loopback action. |
| **CLI version and upgrade checks** | Provider cards show the installed CLI version; `Check for upgrades` is one bounded read-only HTTPS GET to a pinned official source. Upgrades are labeled, never performed. |
| **Retention cleanup** | 30-day retention for terminal local records, dry-run by default, purged only with `--confirm`. Active jobs and retained worktrees are protected; provider histories are never touched. |
| **Loopback dashboard and JSON API** | A standard-library HTTP server on `127.0.0.1:8787` with Overview, Jobs, Providers, Permissions, Logs, and Settings views. |
| **Provider health tests** | `ai-worker test qwen|kimi` runs a small live usage-report request and records the marker-validated result as an ordinary job. |

## What Zorava is not

Accuracy matters more than marketing here, so the boundaries are explicit:

- **Not a model router or proxy.** Zorava never makes a provider inference call itself. There is no LiteLLM, no OpenAI-compatible endpoint, no API-key configuration, and no change to your host agent's model routing.
- **Not a scheduler, daemon, or background service.** Jobs are synchronous and user-initiated. There is no unattended, scheduled, or bulk execution path, and no job-submission API.
- **Not a credential store or login flow.** Zorava has no vault, no credential form, and no telemetry; it does not read `~/.claude/.credentials.json`, does not copy Qwen's key, and does not parse Kimi's OAuth material.
- **Not a general-purpose shell or code-execution service.** Workers receive no shell tool; the dashboard accepts no arbitrary prompt and exposes no command endpoint.
- **Not an automatic patcher.** Nothing is committed, merged, pushed, deployed, installed, or applied to your checkout. Workers do not run your tests or project commands.
- **Not a full security boundary.** Controls are defense in depth against accidents and injection-driven writes. They do not defend against malicious code already running as your Unix user, a compromised kernel, a malicious CLI or provider, or provider-side data retention.

## Architecture

```text
Host agents (your machine)
├── Claude Code ──direct──> Anthropic    its own OAuth session; never routed via Zorava
└── Codex CLI ──direct──> OpenAI         its own ChatGPT/OpenAI login; host skill in this checkout
      │
      │  ai-worker: synchronous CLI, task on stdin, JSON result on stdout
      v
Zorava supervisor  (ai_router/, bin/worker.py)
├── request validation, path containment, size and timeout limits
├── per-worker slot locks, queueing, cancellation
├── SQLite job + event state, redaction, 30-day retention
├── detached per-job Git worktree + scoped MCP file broker   (coding jobs)
└── Linux Landlock write confinement                         (fails closed)
      │
      ├──> Qwen Code CLI ──> Qwen-owned Token Plan credential and model
      └──> Kimi Code CLI ──> Kimi-owned OAuth and managed model alias
```

Component map:

| Path | Role |
| --- | --- |
| `bin/ai-worker`, `bin/worker.py` | The `ai-worker` command: argument parsing, exit codes, CLI output. |
| `ai_router/request.py` | Request schema, size/timeout validation, canonical working-directory containment. |
| `ai_router/supervisor.py` | Job orchestration: slot acquisition, launch, timeout, cancellation, result normalization, error classification. |
| `ai_router/state.py` | SQLite job/event/usage persistence and queries. |
| `ai_router/settings.py` | Private `settings.json` presets: coding budget, concurrency, model profiles. |
| `ai_router/landlock.py`, `bin/sandbox_exec.py` | Landlock ABI probing and the write-confined launcher that wraps each worker CLI. |
| `ai_router/security.py` | Redaction of secrets in persisted text, diffs, and events. |
| `ai_router/retention.py` | Retention preview and purge. |
| `ai_router/updates.py` | Pinned, read-only CLI version checks. |
| `ai_router/kimi_quota.py` | Cache-only Kimi quota window state and the explicit loopback refresh. |
| `workers/base.py`, `workers/qwen.py`, `workers/kimi.py` | Adapter contract and per-provider launch/parse logic. |
| `workers/workspace.py` | Shared worktree creation, MCP configuration, diff collection, and cleanup. |
| `bin/worktree_mcp.py` | The local stdio MCP file broker (`aiworker` server) exposing six scoped tools. |
| `profiles/*.md` | Worker role prompts: read-only researcher/coder profiles, coding editor profiles, and a zero-tool smoke profile. |
| `dashboard/` | Loopback-only operations dashboard (server, HTML, JS, CSS). |
| `tools/update_qwen_credential.py` | Optional operator-run helper that rotates Qwen's key **inside Qwen's own settings** via hidden terminal input. |

Runtime state lives in `~/.local/state/ai-workers` (0700 directories, 0600 files), entirely separate from Git: `jobs/<job-id>/` (worktree, request file, worker runtime data), `locks/`, `tmp/`, `settings.json`, and the SQLite database.

## Host agents and supported workers

**Host agents** own the conversation, decide what to delegate, and review the returned diff. They are *not* managed provider workers, and Zorava never calls their APIs.

| Agent | Relationship | Status in this repository |
| --- | --- | --- |
| **Claude / Claude Code** | Primary host and controller | **Supported.** Claude invokes `ai-worker` synchronously and keeps its direct Anthropic routing (its own Max OAuth). The integration instructions are a Claude Code skill installed outside this repository at `~/.claude/skills/delegate-workers/SKILL.md`; `ai-worker status` reports local Claude routing and cached worker test state. |
| **Codex CLI** | Host and controller | **Supported when Codex runs in a checkout of this repository.** Codex keeps its own ChatGPT/OpenAI login and model routing and drives `ai-worker` synchronously through its shell tool. The instructions ship in-repo as a Codex-discoverable skill at [`.agents/skills/delegate-workers/SKILL.md`](.agents/skills/delegate-workers/SKILL.md); see [Codex CLI host integration](#codex-cli-host-integration-repository-skill). No Codex adapter, provider call, endpoint, or credential path is added, and nothing is written to `~/.codex`. A **ChatGPT-hosted** controller remains roadmap work; the CLI contract is agent-agnostic (task on stdin, JSON on stdout), so any local controller that can run `ai-worker` can drive it today. |
| Other host agents | Host and controller | Roadmap (same CLI contract). |

**Managed coding workers** are specialist provider CLIs that Zorava launches under supervision.

| Worker | CLI as configured | Default model profile | Modes | Tools granted | Default slots |
| --- | --- | --- | --- | --- | --- |
| **Qwen** | `/home/krakadin/.local/bin/qwen` (verified with Qwen Code 0.24.4) | `qwen3.8-max`, re-verified against Qwen's own Token Plan endpoint | `isolated-edit` (default), `read-only` | Coding: the six `aiworker` MCP tools, CLI customizations disabled. Read-only: plan approval mode with `read_file`, `list_directory`, `glob`, `grep_search`, and explicit exclusions for shell/write/network/agent/MCP/workflow tools | 2 |
| **Kimi** | `/home/krakadin/.kimi-code/bin/kimi` (verified with Kimi CLI 2.0.2) | `kimi-code/k3`, an alias from Kimi's own managed provider configuration | `isolated-edit` (default), `read-only` | Coding: the same six MCP tools with a job-scoped Kimi home. Read-only: `Read`, `Grep`, `Glob` with `subagents: []` | 1 |

For both workers, output is parsed into normalized JSON and the child environment is built explicitly so no other provider's credentials are inherited. `KIMI_CODE_NO_AUTO_UPDATE=1` is set for supervised Kimi runs so a job never changes the pinned CLI.

Additional provider CLIs can be added behind the adapter contract in `workers/base.py`; none ship today.

### Codex CLI host integration (repository skill)

Codex CLI is a supported host agent when you run it inside a checkout of this repository. The integration is guidance, not code: this repository ships a Codex skill at [`.agents/skills/delegate-workers/SKILL.md`](.agents/skills/delegate-workers/SKILL.md) telling Codex to stay the orchestrator and to run the same `ai-worker` commands Claude runs. Zorava adds no Codex adapter, no OpenAI-compatible endpoint, and no credential path; Codex keeps its own ChatGPT/OpenAI login and model routing exactly as Claude keeps its Anthropic routing.

**Discovery and installation.** Codex loads repository skills from `.agents/skills/<skill-name>/SKILL.md` beneath the directory where it runs, so the skill itself needs no installation step — only `ai-worker` on your `PATH`:

```bash
cd /path/to/this/repository                        # the checkout that contains .agents/skills/
ln -s "$PWD/bin/ai-worker" ~/.local/bin/ai-worker  # once; skip if already installed
codex                                              # start Codex CLI inside this checkout
```

The skill is repository-scoped: nothing is written to `~/.codex`, and your other projects are unaffected. If your Codex build does not surface repository skills, tell Codex to read `.agents/skills/delegate-workers/SKILL.md` before delegating — the CLI contract does not depend on skill discovery. To stop using it, delete `.agents/skills/delegate-workers/` from the checkout, or simply stop asking Codex to delegate.

**Safe example.** A bounded, read-only investigation that cannot modify anything:

```bash
printf '%s' 'Find where the worker timeout is validated. Cite paths and line numbers; do not modify files.' |
  ai-worker delegate qwen --cwd "$PWD" --mode read-only --timeout 900 --json
```

Coding is the same command with `--mode isolated-edit` (the default): the worker edits a detached worktree under `~/.local/state/ai-workers/jobs/JOB_UUID/worktree` and returns `workspace` plus `diff` as a proposal. Codex must review that proposal, check the worker's claims against the real files, and explicitly apply approved changes to your checkout itself; the worker never auto-applies, commits, merges, pushes, deploys, or installs. Every other Zorava rule is unchanged — clean checkout and Linux Landlock for coding, task text on stdin, 10–1800 second timeouts, per-worker slot limits (Qwen 2, Kimi 1) with queueing, no credentials in Zorava, no endpoint or model override from a job, no recursive delegation, and no automatic commit, merge, push, or deploy. The worker role prompts in `profiles/` were written for a Claude parent; that wording says who reviews the result and does not change behavior for a Codex host. Command details: [Operations](docs/operations.md).

## Isolation model

1. **Clean checkout required.** `isolated-edit` refuses to start unless the primary repository is a Git repository and is clean (`PROJECT_DIRTY`).
2. **Detached worktree per job.** `git worktree add --detach` creates a private tree under the job directory, and the worker's `cwd` is replaced with it.
3. **Six scoped tools.** The MCP broker validates every worktree-relative path, rejects absolute paths, `..` traversal, symlinks, and sensitive/control paths.
4. **Landlock write confinement.** Writes succeed inside the job's private runtime directory and fail outside it, including through an escaping symlink (verified locally at Landlock ABI 8). Reads remain unrestricted at the OS level — this is write confinement, not a complete filesystem sandbox.
5. **Bounded lifetime.** Timeouts, cancellation, and process-group termination are enforced by the supervisor.
6. **Nothing lands automatically.** The worktree and its diff are retained for review; `ai-worker diff` re-reads them and `ai-worker discard JOB_UUID --confirm` removes them explicitly.

The Git worktree prevents accidental edits; it is not itself a security boundary. The broker plus Landlock provide the actual controls.

## Job lifecycle, concurrency, and lineage

```text
queued ──> running ──> completed
   │           │
   │           └────> failed (with an error code)
   └────────────────> cancelled
```

- **Slot locks.** Each worker has 1–4 cross-process slots backed by user-private `flock` files in `~/.local/state/ai-workers/locks`. Jobs beyond capacity stay `queued` and start automatically when a slot frees; per-job worktrees, runtime directories, and environments keep simultaneous jobs isolated, so two concurrent coding jobs return two separate diffs.
- **Cancellation.** `ai-worker cancel JOB_UUID` (or the dashboard) cancels a running job's process group, or marks a `queued` job cancelled without ever starting a worker.
- **Error codes.** Failures are classified rather than guessed: `AUTH_ERROR`, `RATE_LIMITED`, `QUOTA_OR_BILLING`, `TIMEOUT`, `BUDGET_EXHAUSTED`, `INVALID_OUTPUT`, `USAGE_REPORT_MISMATCH`, `PATH_NOT_ALLOWED`, `PROJECT_DIRTY`, `NOT_A_GIT_REPOSITORY`, `SANDBOX_UNAVAILABLE`, `CONFIG_ERROR`, `OUTPUT_LIMIT`, `WORKER_CRASH`, `GIT_ERROR`, `GIT_WORKTREE_ERROR`, `WORKTREE_REMOVE_FAILED`, and `CANCELLED`. Each maps to a stable CLI exit code (codes without an explicit mapping exit `2`); `WORKER_BUSY` is legacy now that jobs queue. See [operations](docs/operations.md) for remediation per code.
- **Job records.** Every job stores worker, role, requested and reported model, CLI version, `cwd`, mode, a redacted task summary, timestamps, duration, exit code, error detail, token usage, job type (`delegation` or `provider_test`), and optional lineage.
- **Delegation lineage.** The structured request accepted by `ai-worker run` can carry `parent_job_id` and `delegation_group_id` (UUIDs) so a job record points back at the delegation that produced it. The `delegate` shortcut does not set them.

## Quick start

### Requirements

- **Linux** with Landlock available (ABI 8 verified locally) — required for `isolated-edit`; coding jobs fail closed with `SANDBOX_UNAVAILABLE` otherwise.
- **Python 3.10 or newer** at `/usr/bin/python3`. Standard library only; there are no third-party dependencies and nothing to install from a package index.
- **Git** at `/usr/bin/git`.
- **Qwen Code CLI** and/or **Kimi Code CLI**, already installed and authenticated through their own official flows. Zorava does not log you in anywhere.
- A **clean Git checkout** under an allowed root. Allowed roots are configured in `bin/worker.py` and currently resolve to `/home/krakadin/myDev`.
- **Single-operator paths.** `bin/ai-worker`, `bin/worker.py`, `workers/qwen.py`, and `workers/kimi.py` contain absolute paths from the machine this was built on (`/home/krakadin/...`). Adjust them to your own layout before use.

### Install

```bash
# From your checkout of this repository.
# First edit bin/ai-worker so it points at your own bin/worker.py, then:
ln -s "$PWD/bin/ai-worker" ~/.local/bin/ai-worker
ai-worker --help
```

### Verify local configuration (no provider calls)

```bash
ai-worker preflight qwen --json
ai-worker preflight kimi --json
ai-worker status
```

`preflight` inspects the pinned executable, saved model profile, endpoint metadata, and credential *presence* locally. It makes no provider call and does not prove that authentication is currently accepted.

### Optional: one small live provider test

```bash
ai-worker test qwen     # explicit live call; asks the CLI for a bounded usage report
ai-worker test kimi
```

### Delegate analysis without edits

```bash
printf '%s' 'Find where authentication is configured. Cite relevant paths and do not modify files.' |
  ai-worker delegate qwen --cwd "$PWD" --mode read-only --json

printf '%s' 'Trace this bug and propose the smallest patch.' |
  ai-worker delegate kimi --cwd "$PWD" --mode read-only --json
```

### Delegate a coding job (the default mode)

```bash
printf '%s' 'Implement the requested change in this clean Git checkout; do not run tests.' |
  ai-worker delegate kimi --cwd "$PWD" --mode isolated-edit --json
```

Coding is the default for both workers, so `ai-worker delegate qwen --cwd "$PWD" --json` is equivalent to passing `--mode isolated-edit`. Read the returned `workspace` path and `diff`, review the proposal, and apply it yourself. Then either keep the worktree for reference or remove it explicitly:

```bash
ai-worker diff JOB_UUID --json
ai-worker discard JOB_UUID --confirm
```

### Host-agent timeouts

A host agent's own tool timeout must exceed the worker timeout. Workers default to 1800 seconds (30 minutes) and accept `--timeout SECONDS` between 10 and 1800.

## CLI reference

```text
ai-worker --help
ai-worker preflight [qwen|kimi] [--json]          # local checks only; defaults to kimi
ai-worker test qwen|kimi [--json]                 # small live provider usage-report test
ai-worker status [--json]                         # local host routing + cached worker test state

ai-worker delegate qwen --cwd PATH [--mode isolated-edit|read-only]
                      [--timeout SECONDS] [--max-tool-calls unlimited|N] [--json]
ai-worker delegate kimi --cwd PATH [--mode isolated-edit|read-only]
                      [--timeout SECONDS] [--json]   # task text comes from stdin

ai-worker run [--json]                            # full structured JSON request from stdin
ai-worker jobs [--json]
ai-worker show JOB_UUID [--json]
ai-worker diff JOB_UUID [--json]
ai-worker cancel JOB_UUID [--json]                # running or queued jobs
ai-worker discard JOB_UUID --confirm [--json]     # removes only that job's worktree/runtime data

ai-worker settings show [--json]                  # saved budget, slot capacity, model profiles
ai-worker settings set qwen-coding-budget unlimited|N   # 0-1000000 (default: unlimited)
ai-worker settings set qwen-concurrency 1-4       # simultaneous Qwen jobs (default: 2)
ai-worker settings set kimi-concurrency 1-4       # simultaneous Kimi jobs (default: 1)
ai-worker settings set qwen-model PROFILE_ID      # verified Qwen Token Plan profile
ai-worker settings set kimi-model PROFILE_ID      # verified managed Kimi Code alias

ai-worker dashboard [--port N]                    # loopback UI at http://127.0.0.1:8787/
ai-worker cleanup --dry-run                       # preview expired local records (the default)
ai-worker cleanup --confirm                       # purge eligible terminal records after review
```

`ai-worker run` accepts the full request schema: `worker` (`qwen`|`kimi`), `task`, optional `context`, `cwd`, `mode`, `timeout_seconds`, optional `max_tool_calls` (Qwen only), and optional `parent_job_id` / `delegation_group_id`.

## Dashboard and local HTTP API

```bash
ai-worker dashboard      # http://127.0.0.1:8787/ — Ctrl+C stops it
```

The dashboard is a standard-library HTTP server bound to IPv4 loopback (`127.0.0.1`, the only address `ai-worker dashboard` requests); it fails closed if the bound address is anything else, validates Host/Origin/CSRF on state-changing requests, and sends restrictive browser security headers. It is optional: CLI delegation works without it, and no daemon or systemd unit is involved.

Views: **Overview**, **Jobs**, **Providers**, **Permissions**, **Logs**, **Settings**. The Permissions view reports allowed project roots, default and maximum timeouts, per-worker concurrency, retention days, the loopback bind, whether any credential appears in Zorava configuration, and the filesystem capabilities granted in coding mode. Provider cards show safe model/backend/endpoint metadata, the installed CLI version, cached update state, health from the last provider test, and usage.

Endpoints:

| Method | Path | Behavior |
| --- | --- | --- |
| `GET` | `/api/v1/session`, `/api/v1/status`, `/api/v1/providers`, `/api/v1/updates`, `/api/v1/usage`, `/api/v1/permissions`, `/api/v1/settings`, `/api/v1/jobs`, `/api/v1/jobs/<id>`, `/api/v1/logs`, `/api/v1/cleanup/preview` | Local and cache reads only — page refreshes and polling never touch the network or a provider |
| `POST` | `/api/v1/test/qwen`, `/api/v1/test/kimi` | Start a fixed, small live provider usage-report test tracked as a normal job (fresh CSRF token per action) |
| `POST` | `/api/v1/jobs/<id>/cancel` | Cancel a running or queued job |
| `POST` | `/api/v1/settings/concurrency`, `/api/v1/settings/model` | Persist validated values through the same private `settings.json` write as the CLI |
| `POST` | `/api/v1/updates/check` | Explicit read-only CLI version comparison (Qwen and Kimi only) |
| `POST` | `/api/v1/usage/refresh` | Explicit, cooldown-limited Kimi quota refresh |
| `POST` | `/api/v1/cleanup/preview`, `/api/v1/cleanup/confirm` | Two-step retention purge |

There is no endpoint that accepts an arbitrary worker prompt, a shell command, a credential, a model endpoint URL, or a caller-supplied fetch target.

## Settings: budgets, concurrency, model profiles

Saved presets live in user-private (0600) JSON at `~/.local/state/ai-workers/settings.json`. It stores operational defaults only — never endpoints, never credentials. Settings written before a field existed keep working with the documented defaults, and a malformed or unsafe file is reported rather than silently overwritten.

```bash
ai-worker settings show                             # budget, slot capacity, model profiles
ai-worker settings set qwen-coding-budget unlimited # default: unlimited (-1)
ai-worker settings set qwen-coding-budget 100       # or any integer 0-1000000
ai-worker settings set qwen-concurrency 2           # 1-4 (default: 2)
ai-worker settings set kimi-concurrency 1           # 1-4 (default: 1)
ai-worker settings set qwen-model qwen3.8-max       # verified Token Plan profile ID
ai-worker settings set kimi-model kimi-code/k3      # verified managed Kimi alias
```

**Qwen tool-call budget.** Coding jobs default to an unlimited budget. A per-request value wins over the saved default: pass `--max-tool-calls unlimited|N` to `ai-worker delegate qwen`, or `"max_tool_calls": -1` / `0`–`1000000` in a JSON request. Read-only analysis keeps its fixed small budget (24) unless a request overrides it, and provider smoke tests keep a zero-tool policy. If a bounded budget is exhausted (Qwen exit 55), the job fails with `BUDGET_EXHAUSTED`; the saved worktree diff is retained and a new job can continue from it. Kimi does not support `max_tool_calls`.

**Concurrency.** Capacity changes apply to future jobs only. The same 1–4 selectors are available at **Dashboard → Settings → Worker concurrency**, validated and persisted through the identical private write.

**Model profiles.** Only IDs are stored. Selections are validated against a worker-specific verified allowlist before saving, and the adapter re-verifies the profile/endpoint pairing when a job or provider test starts, failing closed with `CONFIG_ERROR` on mismatch:

- **Qwen** — only profiles whose Qwen-owned provider entry uses the exact reviewed Token Plan endpoint are selectable. Pay-as-you-go DashScope entries are never offered from a Token Plan credential.
- **Kimi** — only aliases present in Kimi's own private `config.toml` under the managed Kimi Code provider at its expected endpoint. If discovery is unavailable, only the reviewed `kimi-code/k3` default remains.

## Usage, quotas, and CLI versions

- **Token usage.** Per-job input/output/cached token totals are parsed from each CLI's own output and summed over completed jobs in retained local history.
- **Kimi account quota.** The Kimi card shows quota windows (percent used, percent remaining, reset time) with an honest available/stale/unavailable state, served from a memory-only cache. `Refresh usage` is an explicit CSRF-protected action that performs one loopback `GET http://127.0.0.1:<port>/api/v1/oauth/usage` against an **already-running** local Kimi server, using the Kimi-owned local server token, at most once per 60 seconds. Zorava never starts `kimi web`, never generates or stores that token, and never returns it or the raw provider body.
- **Qwen account quota.** There is no reliable account-quota API, so the Qwen card states plainly that account-level remaining quota is unavailable and shows only local per-job token totals. No percentage or balance is ever inferred or fabricated.
- **CLI versions and upgrades.** `installed_version` comes from the pinned CLI's own `--version`. `update_state` is cached metadata: `unknown`, `checking`, `up-to-date`, `update-available`, or `stale`. `Check for upgrades` performs a single bounded read-only HTTPS GET against two pinned official sources (`registry.npmjs.org` for Qwen, `code.kimi.com` for Kimi) with no environment proxies, no redirects, no cookies, no `Authorization` header, no caller-supplied URL, a 6-second timeout, and an 8192-byte response cap. Zorava compares versions and reports; it never installs, patches, or restarts a CLI. Apply upgrades yourself through the provider's official installer, then re-run `preflight` and an explicit test.

## Security and privacy

- **Credential boundaries.** Claude owns Claude Max OAuth, Qwen owns its Token Plan key in its own 0600 settings, Kimi owns its OAuth directory. Zorava parses only safe metadata, never copies a credential into the child environment, and never passes another provider's credential across a boundary. `HOME` is retained so each CLI can read its own configuration.
- **Untrusted input.** Task text, repository content, and worker output are treated as untrusted data. They cannot expand permissions, and prompt-injection instructions found in a repository do not grant tools the profile does not already allow (verified with a controlled fixture whose injected instruction Qwen correctly ignored).
- **Local data.** Runtime root and subdirectories are 0700; SQLite, settings, and request files are 0600. Results, diagnostics, events, and diffs are redacted before persistence (bearer tokens, API keys, cookies, private keys); raw provider output is not stored. Redaction is defense in depth — do not intentionally put secrets in a task.
- **Retention.** Local job records, results, and events are retained for 30 days. `cleanup --dry-run` is the default and previews; `--confirm` purges eligible terminal rows while protecting active jobs and retained worktrees/runtime data. Provider CLI histories and settings are never touched. Deletion is not guaranteed secure erasure on SSDs or snapshot-backed filesystems.
- **Outbound network.** Zorava makes exactly two kinds of outbound requests, both explicit user actions from the dashboard: the pinned CLI version check and the loopback Kimi quota refresh. Neither can reach a provider inference endpoint. There is no telemetry.
- **Provider-side retention.** What Qwen and Kimi keep about a session is governed by their own services and account terms, not by Zorava. Do not delegate material that must remain private from the selected external provider.
- **Qwen Token Plan scope.** Zorava limits the Qwen path to synchronous, user-initiated tasks from an active host session; there is no scheduled, unattended, bulk, or standalone service path. That is a reasoned reading of Alibaba's interactive coding-agent terms, and Alibaba has not expressly endorsed this exact nested arrangement. Read [SECURITY_CHECKPOINT.md](SECURITY_CHECKPOINT.md) before enabling Qwen delegation.
- **Credential rotation helper.** `tools/update_qwen_credential.py` is an optional, operator-run administrative tool that writes a replacement key into Qwen's own settings using hidden terminal input, an explicit typed confirmation, and an atomic 0600 write. It is not a credential store, never prints the value, and does not call any provider.

Full details: [Security model](docs/security.md), [Security checkpoint](SECURITY_CHECKPOINT.md), and the operational security notes in [docs/operations.md](docs/operations.md).

## Development and testing

```bash
# Offline suite — never contacts a provider
cd /path/to/this/repository
/usr/bin/python3 -B -m unittest discover -s tests
```

The suite uses `unittest` with synthetic fixtures and temporary directories only. It covers request/path validation and symlink escape, child-environment filtering, redaction, database safety, state transitions, process-group timeout and cancellation, output bounds and error classification, concurrency slots and queueing, the worktree MCP broker, edit-mode sandbox behavior, both provider adapters, model-profile verification, settings parsing, usage collection, Kimi quota caching, CLI version checks, retention, the worker CLI, and the dashboard (including CSRF/Origin and loopback-bind behavior).

Live provider calls happen only when you ask for them: `ai-worker test qwen` and `ai-worker test kimi` each make one small request for a bounded usage report. `ai-worker doctor` is not implemented; use `preflight` plus an explicit test.

Conventions worth knowing before changing code: no third-party dependencies, no daemon or scheduler, no provider HTTP client, no credential handling, and every new failure mode needs a stable error code plus an exit-code mapping in `bin/worker.py`.

## Limitations

- **Single-operator build.** Absolute paths for the runtime root, allowed project roots, and both worker executables are configured in source rather than in a config file. There is no multi-user or multi-machine support, and no packaging.
- **Linux only.** Landlock is required for coding mode; there is no macOS or Windows sandbox equivalent implemented, and no less-restricted fallback.
- **Same Unix user.** Workers run as you. Reads are not restricted at the OS level, so this is not isolation from a hostile local process.
- **Synchronous only.** No queue service, retry policy, or webhook; a host agent must wait for the result.
- **Codex support is documentation, not code.** The repository skill teaches Codex CLI to drive the existing `ai-worker` contract; there is no Codex adapter or credential path, discovery depends on your Codex build supporting `.agents/skills`, and no live Codex-hosted delegation is recorded as verified in this repository.
- **Two workers.** Qwen and Kimi only. Other provider CLIs need a new adapter plus verified model/endpoint review.
- **No release packaging yet.** The canonical public repository is [github.com/krakadin/Zorava](https://github.com/krakadin/Zorava) with a public issue tracker, but there is no release feed, package registry entry, or installer beyond cloning the repository.

## Roadmap

Planned direction — **none of the items below are implemented in this repository**:

- **Packaged host integrations beyond Claude Code and the Codex CLI**, including a ChatGPT-hosted controller path and a reusable integration contract for other host agents.
- **Additional specialist workers** behind the existing adapter contract, each with its own verified model/endpoint allowlist and role profiles.
- **Richer worker relationship modeling**: delegation trees and groups surfaced in the CLI and dashboard, cost rollups per delegation group, and per-role capability metadata.
- **Stronger isolation**: OS-level read confinement (or a container/namespace option) and per-project policy profiles.
- **ACP support and persistent worker sessions**, as noted in [docs/architecture.md](docs/architecture.md).
- **`ai-worker doctor`** for one-command local health diagnosis.
- **Configuration-driven deployment**: runtime root, allowed roots, and worker executable paths read from a config file instead of source constants; packaging and a `LICENSE` file.

## Documentation

- [Repository on GitHub](https://github.com/krakadin/Zorava) — canonical source, issue tracker, and project links.
- [Architecture](docs/architecture.md) — worker routes, invocation details, sandboxing, dashboard internals.
- [Operations](docs/operations.md) — daily use, full command list, health interpretation, error codes, version handling, rollback.
- [Codex host skill](.agents/skills/delegate-workers/SKILL.md) — the repository-scoped delegation instructions Codex CLI discovers in a checkout.
- [Security model](docs/security.md) — credential boundaries, read-only and isolated-edit controls, redaction, threat model, and limitations.
- [Security checkpoint](SECURITY_CHECKPOINT.md) — the credential-remediation record, credential boundaries, and the Qwen Token Plan usage-scope decision.
- [Audit](AUDIT.md) and [implementation spec](IMPLEMENTATION_SPEC.md) — historical source records for the system's design and requirements.
