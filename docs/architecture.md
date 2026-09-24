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

Claude remains the orchestrator. ai-worker invokes Kimi only when Claude or the user explicitly chooses delegation. The worker is a separate process with bounded task/context; it is not a native Claude subagent. The normalized JSON result returns to Claude, which checks important claims and makes the final decision.

The project has no provider proxy. Claude's Anthropic traffic does not pass through ai-worker. Qwen delegation is limited to synchronous, user-directed work from the active Claude session. The Token Plan interpretation and its limits are recorded in `SECURITY_CHECKPOINT.md`; Alibaba has not expressly endorsed this exact nested arrangement.

Qwen's adapter verifies that the current active model is `qwen3.8-max` and that both configured base URLs use `https://token-plan.maas.qwencloudapi.com/compatible-mode/v1`. It reports only endpoint hostname and credential presence; the key remains in Qwen's settings and is neither inherited nor copied by ai-router. Its requested model is explicit. No fallback model, alternate endpoint, scheduler, background worker service, or unattended/bulk path exists.

## Read-only invocation

`ai-worker delegate qwen|kimi --cwd PATH --json` reads the task from stdin. The supervisor validates UTF-8, payload size, timeout, worker, mode, and canonical working-directory containment beneath `/home/krakadin/myDev`.

Qwen is launched using `/home/krakadin/.local/bin/qwen`, `qwen3.8-max`, plan approval mode, bounded tool calls and wall time, JSON output, and `--no-chat-recording`. Its core allowlist comprises `read_file`, `list_directory`, `glob`, and `grep_search`; the adapter excludes the installed CLI's shell, write/edit, agent, network, tool-bridge, MCP, and workflow tools. The task/context are JSON on stdin; only a fixed parent instruction is in argv. Its CLI session is synchronous, and Qwen's user-owned settings supply its own Token Plan auth.

Kimi is launched using `/home/krakadin/.kimi-code/bin/kimi` with explicit `kimi-code/k3`, `profiles/kimi-coder.md`, and stream-JSON output.

Kimi CLI prompt arguments cannot accept stdin as the task channel, so ai-router writes a short-lived request file in `~/.local/state/ai-workers/tmp/<random-job-dir>/request.json`, mode 0600 inside a 0700 directory. Kimi gets access to that one temporary directory and is asked to read the request file. The task itself is not put in process arguments. The file is removed after completion. Kimi CLI may separately retain the conversation in its own session history under `~/.kimi-code/sessions`.

Each job runs in its own process group, with a bounded timeout and output, captured stdout/stderr, normalized provider JSON parsing, sanitized result persistence, and process-group cleanup. Per-worker file locks limit each worker to one active job. Another simultaneous same-provider request returns `WORKER_BUSY`. The CLI is synchronous; no scheduler or daemon is required.

The supervisor gives workers a small environment with ordinary runtime variables and a deliberate PATH. Neither provider receives the other providers' credentials. `HOME` remains the user's home so each CLI can use its own configuration/authentication. `KIMI_CODE_NO_AUTO_UPDATE=1` applies only to the supervised Kimi invocation; standalone `kimi` behavior is unchanged. Qwen reads its own configured Token Plan credential from its settings; ai-router does not put that credential in the child environment.

Runtime metadata and sanitized results live in `~/.local/state/ai-workers`, separate from Git. SQLite stores operational job fields and short redacted task summaries, not provider credentials.

## Explicit Kimi isolated editing

Editing is opt-in with `--mode isolated-edit`; read-only remains the default. The supervisor requires a clean Git checkout, creates a detached worktree at `~/.local/state/ai-workers/jobs/<job-id>/worktree`, and starts a per-job Kimi data home. The user's Kimi configuration and OAuth directory are symlinked into that private data home so Kimi retains ownership of authentication; credentials are neither copied nor parsed. Kimi session history for edit jobs stays under the job directory until explicitly discarded.

The editing profile exposes only six local MCP tools: `get_task`, `list_files`, `read_file`, `search_text`, `write_file`, and `replace_in_file`. The MCP file broker validates every relative path, rejects symlinks and sensitive/control paths, and scopes operations to the worktree. Kimi has no shell, native filesystem tools, or subagents. Its fixed prompt obtains task text through MCP; the task is not placed in process arguments.

The Kimi process is additionally launched under Linux Landlock (tested locally at ABI 8). Writes are allowed only inside that job's private runtime directory and Kimi's own OAuth directory, which the CLI may need to refresh. The model has no file tool targeting the OAuth directory. Reads remain unrestricted at the OS level: this is write confinement, not a complete filesystem sandbox. If Landlock is unavailable, edit mode fails closed. The worktree protects the primary checkout from edits, but is not itself a security boundary.

On completion, ai-worker returns the worktree location and a bounded sanitized diff. Nothing is copied into the primary checkout. Claude reviews the diff; the user controls any later import. `ai-worker diff JOB_UUID` re-reads it, and `ai-worker discard JOB_UUID --confirm` explicitly removes only that job's worktree and per-job Kimi history. There is no automatic commit, merge, push, test execution, or deployment.

## Optional local dashboard

`ai-worker dashboard` starts a standard-library HTTP server on `127.0.0.1:8787`. It reads the same SQLite job state and invokes only predefined provider-test, cancellation, and retention actions. It does not accept arbitrary prompts, expose a shell, or serve arbitrary filesystem paths. It is not required for synchronous Claude delegation; no daemon or systemd service is required.

The UI shows safe provider configuration metadata, cached health, jobs, sanitized event logs, permission summaries, and retention settings. Model and endpoint changes are not exposed as form inputs. Only the locally verified Qwen Token Plan and Kimi Code K3 profiles are enabled. Add/switch models only through a separately verified configuration change; credentials remain provider-owned.

Local job records have 30-day retention. `ai-worker cleanup --dry-run` previews eligible records and `ai-worker cleanup --confirm` removes eligible terminal job/event rows. Active jobs and retained Kimi worktree/session directories are protected. Native provider histories remain provider-owned. ACP, persistent sessions, and OS-level read confinement remain future work.
