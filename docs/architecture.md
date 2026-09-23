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

## Invocation and boundaries

`ai-worker delegate kimi --cwd PATH --json` reads the task from stdin. The supervisor validates UTF-8, payload size, timeout, worker, mode, and canonical working-directory containment beneath `/home/krakadin/myDev`. It launches `/home/krakadin/.kimi-code/bin/kimi` using an argument array, its explicit `kimi-code/k3` model, `profiles/kimi-coder.md`, and stream-JSON output.

Kimi CLI prompt arguments cannot accept stdin as the task channel, so ai-router writes a short-lived request file in `~/.local/state/ai-workers/tmp/<random-job-dir>/request.json`, mode 0600 inside a 0700 directory. Kimi gets access to that one temporary directory and is asked to read the request file. The task itself is not put in process arguments. The file is removed after completion. Kimi CLI may separately retain the conversation in its own session history under `~/.kimi-code/sessions`.

Each job runs in its own process group, with a bounded timeout and output, captured stdout/stderr, normalized JSONL parsing, sanitized result persistence, and process-group cleanup. Per-worker file locks limit Kimi to one active job. Another simultaneous Kimi request returns `WORKER_BUSY`. The CLI is synchronous; no daemon is required.

The supervisor gives Kimi a small environment with ordinary runtime variables and a deliberate PATH. It does not pass Anthropic or Qwen credential variables. `HOME` remains the user's home so Kimi can use its own configuration and OAuth. `KIMI_CODE_NO_AUTO_UPDATE=1` applies only to the supervised invocation; standalone `kimi` behavior is unchanged.

Runtime metadata and sanitized results live in `~/.local/state/ai-workers`, separate from Git. SQLite stores operational job fields and short redacted task summaries, not provider credentials.

## Future work

The dashboard and Qwen adapter are deferred. ACP/MCP, a daemon, persistent agent sessions, and editing mode are not required for current CLI delegation. Any future editing mode should use a separate Git worktree and receive a new security review; a worktree alone is not a security sandbox.
