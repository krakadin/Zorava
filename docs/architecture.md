# Architecture

## Current worker routes

```text
Claude Code --direct--> Anthropic / Claude (existing Max OAuth)
     |
     +-- ai-worker --> pinned Qwen Code CLI --> Token Plan / qwen3.8-max
     |                   Qwen-owned local config
     `-- ai-worker --> pinned Kimi Code CLI --> Kimi Code / K3
                         existing Kimi OAuth
```

Claude remains the orchestrator. ai-worker invokes Kimi only when Claude or the user explicitly chooses delegation. The worker is a separate process with bounded task/context; it is not a native Claude subagent. The normalized JSON result returns to Claude, which checks important claims and makes the final decision. The same contract applies to any host agent that can run `ai-worker`: the Codex CLI host path is documented in the [README](../README.md) and its repository skill at `../.agents/skills/delegate-workers/SKILL.md`, and it changes no adapter, sandbox, or credential behavior.

The project has no provider proxy. Claude's Anthropic traffic does not pass through ai-worker. Qwen delegation is limited to synchronous, user-directed work from the active Claude session. The Token Plan interpretation and its limits are recorded in `SECURITY_CHECKPOINT.md`; Alibaba has not expressly endorsed this exact nested arrangement.

Qwen's adapter verifies that the current active model matches the saved selected profile (default `qwen3.8-max`) and that both configured base URLs use `https://token-plan.maas.qwencloudapi.com/compatible-mode/v1`. It reports only endpoint hostname and credential presence; the key remains in Qwen's settings and is neither inherited nor copied by ai-router. Its requested model is explicit. No fallback model, alternate endpoint, scheduler, background worker service, or unattended/bulk path exists.

Each worker's requested model comes from a saved model profile selection in the private `settings.json` (IDs only; defaults `qwen3.8-max` and `kimi-code/k3`). A selection is accepted only from the worker's verified allowlist: Qwen profiles must have a Qwen-owned provider entry on the exact reviewed Token Plan endpoint, and Kimi profiles must be aliases under the managed Kimi Code provider at its expected endpoint in Kimi's own private config. The supervisor applies the saved profile before the job snapshot is recorded, and the adapter re-verifies the profile/endpoint pairing when building the launch command or provider test, failing closed on any mismatch.

## Coding and read-only invocation

`ai-worker delegate qwen|kimi --cwd PATH --json` reads the task from stdin and defaults to coding in a separate Git worktree. Both adapters share worktree creation, the scoped file broker, diff collection, and cleanup in `workers/workspace.py`. The parent reviews the returned diff; the primary checkout is not automatically changed. Use `--mode read-only` to select the analysis path described below. The supervisor validates UTF-8, payload size, timeout, worker, mode, and canonical working-directory containment beneath `/home/krakadin/myDev`.

For read-only analysis, Qwen is launched using `/home/krakadin/.local/bin/qwen`, the saved selected model profile (default `qwen3.8-max`), plan approval mode, bounded tool calls and wall time, JSON output, and `--no-chat-recording`. Its core allowlist comprises `read_file`, `list_directory`, `glob`, and `grep_search`; the adapter excludes the installed CLI's shell, write/edit, agent, network, tool-bridge, MCP, and workflow tools. The task/context are JSON on stdin; only a fixed parent instruction is in argv. Its CLI session is synchronous, and Qwen's user-owned settings supply its own Token Plan auth.

For read-only analysis, Kimi is launched using `/home/krakadin/.kimi-code/bin/kimi` with the saved selected managed Kimi Code alias (default `kimi-code/k3`), `profiles/kimi-coder.md`, and stream-JSON output.

Kimi CLI prompt arguments cannot accept stdin as the task channel, so ai-router writes a short-lived request file in `~/.local/state/ai-workers/tmp/<random-job-dir>/request.json`, mode 0600 inside a 0700 directory. Kimi gets access to that one temporary directory and is asked to read the request file. The task itself is not put in process arguments. The file is removed after completion. Kimi CLI may separately retain the conversation in its own session history under `~/.kimi-code/sessions`.

Each job runs in its own process group, with a bounded timeout and output, captured stdout/stderr, normalized provider JSON parsing, sanitized result persistence, and process-group cleanup. Per-worker slot locks (user-private `<worker>.<index>.lock` flock files in the runtime `locks` directory) bound simultaneous jobs per worker across processes: Qwen defaults to two concurrent sessions, Kimi to one, both configurable with `ai-worker settings set qwen-concurrency|kimi-concurrency` (1–4). Jobs beyond capacity remain `queued` and start when a slot frees rather than failing; a queued job can be cancelled before it starts. The CLI is synchronous; no scheduler or daemon is required.

The supervisor gives workers a small environment with ordinary runtime variables and a deliberate PATH. Neither provider receives the other providers' credentials. `HOME` remains the user's home so each CLI can use its own configuration/authentication. `KIMI_CODE_NO_AUTO_UPDATE=1` applies only to the supervised Kimi invocation; standalone `kimi` behavior is unchanged. Qwen reads its own configured Token Plan credential from its settings; ai-router does not put that credential in the child environment.

Runtime metadata and sanitized results live in `~/.local/state/ai-workers`, separate from Git. SQLite stores operational job fields and short redacted task summaries, not provider credentials.

## Explicit Kimi isolated editing

Editing is opt-in with `--mode isolated-edit`; read-only remains the default. The supervisor requires a clean Git checkout, creates a detached worktree at `~/.local/state/ai-workers/jobs/<job-id>/worktree`, and starts a per-job Kimi data home. The user's Kimi configuration and OAuth directory are symlinked into that private data home so Kimi retains ownership of authentication; credentials are neither copied nor parsed. Kimi session history for edit jobs stays under the job directory until explicitly discarded.

The editing profile exposes only six local MCP tools: `get_task`, `list_files`, `read_file`, `search_text`, `write_file`, and `replace_in_file`. The MCP file broker validates every relative path, rejects symlinks and sensitive/control paths, and scopes operations to the worktree. Kimi has no shell, native filesystem tools, or subagents. Its fixed prompt obtains task text through MCP; the task is not placed in process arguments.

The Kimi process is additionally launched under Linux Landlock (tested locally at ABI 8). Writes are allowed only inside that job's private runtime directory and Kimi's own OAuth directory, which the CLI may need to refresh. The model has no file tool targeting the OAuth directory. Reads remain unrestricted at the OS level: this is write confinement, not a complete filesystem sandbox. If Landlock is unavailable, edit mode fails closed. The worktree protects the primary checkout from edits, but is not itself a security boundary.

On completion, ai-worker returns the worktree location and a bounded sanitized diff. Nothing is copied into the primary checkout. Claude reviews the diff; the user controls any later import. `ai-worker diff JOB_UUID` re-reads it, and `ai-worker discard JOB_UUID --confirm` explicitly removes only that job's worktree and per-job Kimi history. There is no automatic commit, merge, push, test execution, or deployment.

## Optional local dashboard

`ai-worker dashboard` starts a standard-library HTTP server on `127.0.0.1:8787`. It reads the same SQLite job state and invokes only predefined provider-test, cancellation, and retention actions. It does not accept arbitrary prompts, expose a shell, or serve arbitrary filesystem paths. It is not required for synchronous Claude delegation; no daemon or systemd service is required.

The UI shows safe provider configuration metadata, cached health, jobs, sanitized event logs, permission summaries, and retention settings. Endpoint and credential changes are never exposed as form inputs. Model profiles can be selected on the Settings page, but only from the locally verified allowlists (Qwen Token Plan profiles on the reviewed endpoint; managed Kimi Code aliases); any other model requires a separately verified configuration change, and credentials remain provider-owned.

Local job records have 30-day retention. `ai-worker cleanup --dry-run` previews eligible records and `ai-worker cleanup --confirm` removes eligible terminal job/event rows. Active jobs and retained Kimi worktree/session directories are protected. Native provider histories remain provider-owned. ACP, persistent sessions, and OS-level read confinement remain future work.

## Coding tools and test status

Coding uses `qwen-editor.md` or `kimi-editor.md` and the same six aiworker MCP tools. Qwen uses an explicit MCP configuration, customizations disabled, and a job-scoped `QWEN_RUNTIME_DIR`; Kimi uses a job-scoped home linked to its own configuration/auth. Both run under Landlock write confinement. Every job has its own runtime directory, environment, and detached worktree keyed by job ID, so concurrent same-worker jobs stay isolated and return separate diffs. Git worktrees and diffs are retained for review.

The dashboard retrieves provider tests directly from SQLite, independently of the recent-jobs list. Each test action gets a fresh CSRF token and follows the returned job ID until completion, then displays that completion timestamp. Each provider test asks the worker to report its session usage/statistics in one bounded marked line; the supervisor records an invalid or mismatched usage report as a failure before persisting a final job state. Kimi smoke tests use a separate profile with no tools.
