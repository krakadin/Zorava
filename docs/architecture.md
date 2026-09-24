# Architecture

## Kimi-first phase

```text
Claude Code --direct--> Anthropic / Claude (existing Max OAuth)
     |
     `-- ai-worker --> pinned Kimi Code CLI --> Kimi Code / K3
                         existing Kimi OAuth
```

Claude remains the orchestrator. ai-worker invokes Kimi only when Claude or the user explicitly chooses delegation. The worker is a separate process with bounded task/context; it is not a native Claude subagent. The normalized JSON result returns to Claude, which checks important claims and makes the final decision.

The project has no provider proxy. Claude's Anthropic traffic does not pass through ai-worker. Qwen is not enabled pending a decision about Alibaba Token Plan restrictions; see `SECURITY_CHECKPOINT.md`.

## Read-only invocation

`ai-worker delegate kimi --cwd PATH --json` reads the task from stdin. The supervisor validates UTF-8, payload size, timeout, worker, mode, and canonical working-directory containment beneath `/home/krakadin/myDev`. It launches `/home/krakadin/.kimi-code/bin/kimi` using an argument array, its explicit `kimi-code/k3` model, `profiles/kimi-coder.md`, and stream-JSON output.

Kimi CLI prompt arguments cannot accept stdin as the task channel, so ai-router writes a short-lived request file in `~/.local/state/ai-workers/tmp/<random-job-dir>/request.json`, mode 0600 inside a 0700 directory. Kimi gets access to that one temporary directory and is asked to read the request file. The task itself is not put in process arguments. The file is removed after completion. Kimi CLI may separately retain the conversation in its own session history under `~/.kimi-code/sessions`.

Each job runs in its own process group, with a bounded timeout and output, captured stdout/stderr, normalized JSONL parsing, sanitized result persistence, and process-group cleanup. Per-worker file locks limit Kimi to one active job. Another simultaneous Kimi request returns `WORKER_BUSY`. The CLI is synchronous; no daemon is required.

The supervisor gives Kimi a small environment with ordinary runtime variables and a deliberate PATH. It does not pass Anthropic or Qwen credential variables. `HOME` remains the user's home so Kimi can use its own configuration and OAuth. `KIMI_CODE_NO_AUTO_UPDATE=1` applies only to the supervised invocation; standalone `kimi` behavior is unchanged.

Runtime metadata and sanitized results live in `~/.local/state/ai-workers`, separate from Git. SQLite stores operational job fields and short redacted task summaries, not provider credentials.

## Explicit Kimi isolated editing

Editing is opt-in with `--mode isolated-edit`; read-only remains the default. The supervisor requires a clean Git checkout, creates a detached worktree at `~/.local/state/ai-workers/jobs/<job-id>/worktree`, and starts a per-job Kimi data home. The user's Kimi configuration and OAuth directory are symlinked into that private data home so Kimi retains ownership of authentication; credentials are neither copied nor parsed. Kimi session history for edit jobs stays under the job directory until explicitly discarded.

The editing profile exposes only six local MCP tools: `get_task`, `list_files`, `read_file`, `search_text`, `write_file`, and `replace_in_file`. The MCP file broker validates every relative path, rejects symlinks and sensitive/control paths, and scopes operations to the worktree. Kimi has no shell, native filesystem tools, or subagents. Its fixed prompt obtains task text through MCP; the task is not placed in process arguments.

The Kimi process is additionally launched under Linux Landlock (tested locally at ABI 8). Writes are allowed only inside that job's private runtime directory and Kimi's own OAuth directory, which the CLI may need to refresh. The model has no file tool targeting the OAuth directory. Reads remain unrestricted at the OS level: this is write confinement, not a complete filesystem sandbox. If Landlock is unavailable, edit mode fails closed. The worktree protects the primary checkout from edits, but is not itself a security boundary.

On completion, ai-worker returns the worktree location and a bounded sanitized diff. Nothing is copied into the primary checkout. Claude reviews the diff; the user controls any later import. `ai-worker diff JOB_UUID` re-reads it, and `ai-worker discard JOB_UUID --confirm` explicitly removes only that job's worktree and per-job Kimi history. There is no automatic commit, merge, push, test execution, or deployment.

## Future work

The dashboard and Qwen adapter are deferred. ACP and a daemon are not required. OS-level read confinement, persistent ACP sessions, and automated patch import remain future work.
