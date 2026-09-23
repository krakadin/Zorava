# ai-router

Local, read-only delegation from Claude Code to provider-owned worker CLIs.

> Do not put provider credentials in ai-router. Claude, Qwen, and Kimi retain ownership of their own authentication.

## Current implementation

Claude Code remains the primary agent and connects directly to Anthropic using its existing Max OAuth session. For Kimi delegation, Claude invokes `ai-worker`, which validates the request and launches the pinned Kimi Code CLI with its own Kimi Code OAuth. Claude's endpoint and model routing are not changed. No LiteLLM, proxy, MCP server, daemon, or dashboard is used in this checkpoint.

Kimi is configured as the advisory coding-analysis worker (`kimi-code/k3`). Its CLI was tested at version 2.0.2. The worker profile allows only Kimi's documented `Read`, `Grep`, and `Glob` tools and disables nested subagents. This is application-level read-only control, not an OS filesystem sandbox.

Qwen is intentionally not enabled yet: the replacement credential is for Alibaba Token Plan, whose current usage restrictions need resolution before automated non-interactive delegation. See [SECURITY_CHECKPOINT.md](SECURITY_CHECKPOINT.md).

```text
User -> Claude Code -> Anthropic (direct, existing Max OAuth)
                    -> ai-worker -> Kimi CLI -> Kimi Code / K3
```

Claude's own Anthropic traffic never passes through ai-worker. Worker task text is passed to ai-worker over stdin, staged briefly in an owner-only per-job file for Kimi, and removed after the job. Kimi Code itself retains session history in its own `~/.kimi-code` data directory; ai-router does not clean provider-owned history.

## Quick start

```bash
ai-worker preflight
ai-worker test kimi
ai-worker status
```

Delegate a task by sending task text on stdin:

```bash
printf '%s' 'Find where configuration is loaded and cite the relevant files.' |
  ai-worker delegate kimi --cwd "$PWD" --json
```

The `--json` response is the stable machine-readable interface for Claude. Only `read-only` mode is currently accepted. Allowed working directories resolve beneath `/home/krakadin/myDev`.

Claude integration instructions are installed at `~/.claude/skills/delegate-workers/SKILL.md`. Start or restart Claude Code, then ask it to have Kimi investigate a harmless task. Claude may ask you to approve the narrowly scoped `ai-worker` Bash invocation. Claude remains responsible for reviewing the result and responding.

## Commands available now

```text
ai-worker --help
ai-worker preflight [--json]
ai-worker test kimi [--json]
ai-worker delegate kimi --cwd PATH [--timeout SECONDS] [--json]  # task from stdin
ai-worker run [--json]                                          # JSON request from stdin
ai-worker jobs [--json]
ai-worker show JOB_UUID [--json]
ai-worker cancel JOB_UUID [--json]
```

The dashboard, provider cards, cleanup command, and Qwen adapter are not part of this Kimi-first checkpoint.

## Tests

Run offline tests without provider calls:

```bash
cd /home/krakadin/myDev/ai-router
/usr/bin/python3 -B -m unittest discover -s tests
```

Live provider calls are separate and explicit: `ai-worker test kimi` makes a small request. Normal tests never contact a provider.

See [architecture](docs/architecture.md), [security](docs/security.md), and [operations](docs/operations.md).
