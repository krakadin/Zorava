# Zorava

Zorava is a local, user-triggered delegation system for Qwen and Kimi coding workers, driven from Claude Code. Both Qwen and Kimi are coding workers. Each coding job gets its own Git worktree and returns a diff for review.

Zorava is the public product name. The repository directory, the `ai_router` Python package, the `ai-worker` command, and the `~/.local/state/ai-workers` runtime paths keep their existing technical names, and the text below still refers to that codebase as `ai-router`.

> Do not put provider credentials in ai-router. Claude, Qwen, and Kimi retain ownership of their own authentication.

## Architecture

Claude Code remains the primary agent and connects directly to Anthropic using its existing Max OAuth session. For a user-directed task in the active Claude session, Claude invokes `ai-worker` synchronously. The supervisor launches the pinned Qwen Code or Kimi Code CLI, each using its existing provider-owned configuration. Claude's endpoint and model routing are not changed. No LiteLLM, model proxy, scheduler, background service, or daemon is used.

```text
User -> Claude Code -> Anthropic (direct, existing Max OAuth)
                    -> ai-worker -> Qwen CLI -> Alibaba Model Studio Token Plan
                    -> ai-worker -> Kimi CLI -> Kimi Code / K3
```

Qwen uses `/home/krakadin/.local/bin/qwen`, `qwen3.8-max`, and the existing Alibaba Model Studio Token Plan URL/key stored in Qwen's own configuration. The adapter verifies the reviewed model/endpoint pair but never receives or stores the key. Qwen coding jobs use the scoped aiworker file tools, a configurable tool-call budget (unlimited by default; see below), bounded wall time, JSON output, and disabled chat recording. Its runtime files stay in the job directory while its own settings retain the credential.

Kimi uses `/home/krakadin/.kimi-code/bin/kimi` and `kimi-code/k3`. Both coders default to a clean detached Git worktree, a path-scoped MCP file broker, and tested Linux Landlock write confinement. Coding jobs receive no shell or native filesystem tools. Explicit `--mode read-only` remains available for analysis. The primary checkout is never automatically changed; Claude reviews the returned diff.

The Qwen Token Plan path is limited in ai-router to synchronous user-initiated tasks in the active Claude session. This is a reasoned reading of Alibaba's interactive coding-agent rules and examples; Alibaba has not expressly endorsed this exact nested Claude-to-Qwen arrangement. ai-router does not support scheduled, unattended, bulk, or standalone service calls. See [SECURITY_CHECKPOINT.md](SECURITY_CHECKPOINT.md).

## Quick start

```bash
ai-worker preflight qwen
ai-worker preflight kimi
ai-worker test qwen
ai-worker test kimi
ai-worker status
```

Use Qwen for a bounded investigation:

```bash
printf '%s' 'Find where authentication is configured. Cite relevant paths and do not modify files.' |
  ai-worker delegate qwen --cwd "$PWD" --mode read-only --json
```

Use Kimi for coding analysis:

```bash
printf '%s' 'Trace this bug and propose the smallest patch.' |
  ai-worker delegate kimi --cwd "$PWD" --mode read-only --json
```

The task goes through stdin rather than the process command line. Coding in `isolated-edit` mode is the default for both workers; it requires a clean Git checkout and Landlock availability. For implementation, run `ai-worker delegate qwen --cwd "$PWD" --json` or use `kimi` in place of `qwen`. Review the returned `workspace` and `diff`; changes are not automatically applied. Allowed working directories must resolve beneath `/home/krakadin/myDev`.

### Qwen coding tool-call budget

Qwen `isolated-edit` coding jobs default to an **unlimited** tool-call budget. A per-request override wins over the saved default: add `"max_tool_calls": -1` (unlimited) or an integer `0`–`1000000` to the JSON request, or pass `--max-tool-calls unlimited|N` to `ai-worker delegate qwen`. To save a default applied to future coding jobs that omit `max_tool_calls`:

```bash
ai-worker settings show                                # inspect the saved budget (default: unlimited)
ai-worker settings set qwen-coding-budget unlimited    # unlimited (the default)
ai-worker settings set qwen-coding-budget 100          # or any integer 0-1000000
```

The preset is stored as user-private (0600) JSON under `~/.local/state/ai-workers/settings.json` with no credentials, and applies only to future Qwen coding jobs. Read-only analysis keeps its fixed small budget (24) unless a request overrides it, and smoke tests keep their fixed zero-tool policy. If a bounded budget is exhausted (Qwen exit 55), the job fails with `BUDGET_EXHAUSTED`; the saved worktree diff is retained and can be continued by a new job. Kimi does not support `max_tool_calls`.

### Worker concurrency

Each worker has a bounded number of cross-process slots, enforced with user-private flock files under `~/.local/state/ai-workers/locks`. Qwen supports **two** simultaneous jobs by default (so this session and another Claude session can delegate at the same time); Kimi defaults to one. Jobs beyond capacity stay `queued` and start automatically when a slot frees instead of failing. Queued jobs can be cancelled cleanly before they start (`ai-worker cancel JOB_UUID` or the dashboard). Per-job Git worktrees, runtime directories, and worker environment state remain isolated under concurrency, so simultaneous isolated-edit jobs return separate diffs.

```bash
ai-worker settings show                          # shows budget plus per-worker slot capacity
ai-worker settings set qwen-concurrency 2        # Qwen slots, 1-4 (default: 2)
ai-worker settings set kimi-concurrency 1        # Kimi slots, 1-4 (default: 1)
```

Capacity changes apply to future jobs only and are stored in the same private `settings.json`; a settings file saved before these fields existed keeps working with the defaults above. Concurrency can also be changed from the dashboard: **Dashboard → Settings → Worker concurrency** offers the same 1–4 selectors with a Save control per worker, validated and persisted through the same private `settings.json` write as the CLI.

### Model profiles

Each worker has a saved model profile selection (IDs only — never endpoints or credentials), defaulting to the reviewed profiles `qwen3.8-max` (Qwen) and `kimi-code/k3` (Kimi). Selections are validated against a worker-specific verified allowlist before they are saved, and the adapter re-verifies the selected profile/endpoint pairing when a job or provider test starts:

- **Qwen**: only model profiles whose Qwen-owned provider entry uses the exact reviewed Token Plan endpoint are selectable. DashScope pay-as-you-go entries are never offered from the Token Plan credential.
- **Kimi**: only aliases present in Kimi's own private `config.toml` under the managed Kimi Code provider at its expected endpoint are selectable. If discovery is unavailable, only the reviewed `kimi-code/k3` default remains.

```bash
ai-worker settings show                            # shows budget, capacity, and model profiles
ai-worker settings set qwen-model qwen3.8-max      # a verified Qwen Token Plan profile ID
ai-worker settings set kimi-model kimi-code/k3     # a verified managed Kimi Code alias
```

Changes apply to future jobs and provider tests only and live in the same private `settings.json`. The dashboard offers the same verified selectors at **Dashboard → Settings → Model profiles**; endpoints and credentials are never shown or editable there.

Claude integration instructions are installed at `~/.claude/skills/delegate-workers/SKILL.md`. Start or restart Claude Code after updating the skill. Claude remains responsible for checking worker findings and responding. A Claude Bash/tool timeout must exceed the worker timeout, which defaults to 1800 seconds (30 minutes) for both Qwen and Kimi and can be lowered with `--timeout SECONDS` (accepted range 10–1800).

## Commands

```text
ai-worker --help
ai-worker preflight [qwen|kimi] [--json]
ai-worker test qwen [--json]
ai-worker test kimi [--json]
ai-worker delegate qwen --cwd PATH [--timeout SECONDS] [--max-tool-calls unlimited|N] [--json]  # isolated coding; task from stdin
ai-worker delegate kimi --cwd PATH [--timeout SECONDS] [--json]  # isolated coding; task from stdin
ai-worker delegate qwen|kimi --cwd PATH --mode read-only --json # analysis without edits
ai-worker settings show                                # saved budget, slot capacity, and model profiles
ai-worker settings set qwen-coding-budget unlimited|N  # save default for future Qwen coding jobs
ai-worker settings set qwen-concurrency 1-4            # simultaneous Qwen jobs (default: 2)
ai-worker settings set kimi-concurrency 1-4            # simultaneous Kimi jobs (default: 1)
ai-worker settings set qwen-model PROFILE_ID           # verified Qwen Token Plan profile (default: qwen3.8-max)
ai-worker settings set kimi-model PROFILE_ID           # verified managed Kimi Code alias (default: kimi-code/k3)
ai-worker diff JOB_UUID [--json]
ai-worker discard JOB_UUID --confirm
ai-worker run [--json]  # structured request from stdin
ai-worker jobs [--json]
ai-worker show JOB_UUID [--json]
ai-worker cancel JOB_UUID [--json]
ai-worker status [--json]
ai-worker dashboard                  # optional UI at http://127.0.0.1:8787/
ai-worker cleanup --dry-run          # preview expired local records (default)
ai-worker cleanup --confirm          # purge eligible terminal records after review
```

The Zorava dashboard has Overview, Jobs, Providers, Permissions, Logs, and Settings views. It shows safe provider/model/endpoint metadata, supports fixed small Qwen/Kimi smoke tests, cancellation, per-worker concurrency changes (1–4, same validation as the CLI), and a two-step retention purge. It accepts no arbitrary worker prompt or shell command. Refreshes do not contact providers. Provider cards show the installed CLI version plus cached update metadata; `Check for upgrades` is an explicit, read-only comparison against that CLI's fixed official version source, and a pending upgrade is only labeled, never installed. Test buttons track the new job through completion, display its completion timestamp and the last successful test, and refresh their action token after dashboard restarts. Model profiles can be selected on the Settings page, but only from the locally verified allowlists (Qwen Token Plan profiles on the reviewed endpoint; managed Kimi Code aliases); endpoint and credential edits are intentionally not available in the UI. Do not enter credentials into ai-router.

Local job records use a 30-day retention period. The default cleanup is a dry run. It protects active jobs and retained isolated-edit worktrees/runtime data and never deletes Qwen/Kimi provider histories. Purging records is not guaranteed secure erasure on SSDs or snapshot-backed filesystems.

## Security and limits

Workers run as the same Unix user. Qwen plan mode and CLI tool restrictions are application-level protections, not an OS read sandbox; do not delegate material that must remain private from the selected external provider. Repository instructions are untrusted data and cannot expand permissions. ai-router stores sanitized operational metadata/results under `~/.local/state/ai-workers`; task summaries and results may contain proprietary information. Qwen and Kimi may have provider-side retention governed by their own services and account terms.

Claude's Anthropic traffic never passes through ai-worker. ai-router does not read Claude credentials, copy provider credentials, make provider inference calls itself, or provide a general-purpose shell endpoint. Its only outbound request is the dashboard's explicit CLI version check: one bounded, read-only HTTPS GET per worker to two fixed official sources (`registry.npmjs.org` for Qwen, `code.kimi.com` for Kimi), sent with no environment proxies, no redirects, no cookies, no `Authorization` or provider credential, and no caller-supplied URL. It compares versions and never installs or upgrades a CLI. The worker supervisor works without the optional dashboard.

## Tests

Run offline tests without provider calls:

```bash
cd /home/krakadin/myDev/ai-router
/usr/bin/python3 -B -m unittest discover -s tests
```

Live provider calls are explicit: `ai-worker test qwen` and `ai-worker test kimi` make small requests. Normal tests never contact a provider.

See [architecture](docs/architecture.md), [security](docs/security.md), [operations](docs/operations.md), and [the security checkpoint](SECURITY_CHECKPOINT.md).
