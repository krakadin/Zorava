# ai-router

Local, user-triggered Qwen and Kimi delegation from Claude Code. Qwen is a read-only research worker; Kimi provides read-only coding analysis and explicitly requested isolated-worktree edits.

> Do not put provider credentials in ai-router. Claude, Qwen, and Kimi retain ownership of their own authentication.

## Architecture

Claude Code remains the primary agent and connects directly to Anthropic using its existing Max OAuth session. For a user-directed task in the active Claude session, Claude invokes `ai-worker` synchronously. The supervisor launches the pinned Qwen Code or Kimi Code CLI, each using its existing provider-owned configuration. Claude's endpoint and model routing are not changed. No LiteLLM, model proxy, scheduler, background service, or daemon is used.

```text
User -> Claude Code -> Anthropic (direct, existing Max OAuth)
                    -> ai-worker -> Qwen CLI -> Alibaba Model Studio Token Plan
                    -> ai-worker -> Kimi CLI -> Kimi Code / K3
```

Qwen uses `/home/krakadin/.local/bin/qwen`, `qwen3.8-max`, and the existing Alibaba Model Studio Token Plan URL/key stored in Qwen's own configuration. The adapter verifies the reviewed model/endpoint pair but never receives or stores the key. Qwen runs plan mode, with read tools allowlisted, mutation/shell/agent/network tools excluded, bounded tool calls/time, JSON output, and chat recording disabled.

Kimi uses `/home/krakadin/.kimi-code/bin/kimi` and `kimi-code/k3`. Read-only analysis allows only Kimi's documented `Read`, `Grep`, and `Glob` tools. Explicit edit jobs use a clean detached Git worktree, a path-scoped MCP file broker, and tested Linux Landlock write confinement. Kimi receives no shell or native filesystem tools. The primary checkout is never automatically changed; Claude reviews the returned diff.

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
  ai-worker delegate qwen --cwd "$PWD" --json
```

Use Kimi for coding analysis:

```bash
printf '%s' 'Trace this bug and propose the smallest patch.' |
  ai-worker delegate kimi --cwd "$PWD" --json
```

The task goes through stdin rather than the process command line. Read-only is the default. Use Kimi `--mode isolated-edit` only when the user explicitly requests implementation; that requires a clean Git checkout and Landlock availability. Allowed working directories must resolve beneath `/home/krakadin/myDev`.

Claude integration instructions are installed at `~/.claude/skills/delegate-workers/SKILL.md`. Start or restart Claude Code after updating the skill. Claude remains responsible for checking worker findings and responding. A Claude Bash/tool timeout must exceed the worker timeout.

## Commands

```text
ai-worker --help
ai-worker preflight [qwen|kimi] [--json]
ai-worker test qwen [--json]
ai-worker test kimi [--json]
ai-worker delegate qwen --cwd PATH [--timeout SECONDS] [--json]  # read-only; task from stdin
ai-worker delegate kimi --cwd PATH [--timeout SECONDS] [--json]  # read-only; task from stdin
ai-worker delegate kimi --cwd PATH --mode isolated-edit --json # explicit isolated edits
ai-worker diff JOB_UUID [--json]
ai-worker discard JOB_UUID --confirm
ai-worker run [--json]  # structured request from stdin
ai-worker jobs [--json]
ai-worker show JOB_UUID [--json]
ai-worker cancel JOB_UUID [--json]
ai-worker status [--json]
```

## Security and limits

Workers run as the same Unix user. Qwen plan mode and CLI tool restrictions are application-level protections, not an OS read sandbox; do not delegate material that must remain private from the selected external provider. Repository instructions are untrusted data and cannot expand permissions. ai-router stores sanitized operational metadata/results under `~/.local/state/ai-workers`; task summaries and results may contain proprietary information. Qwen and Kimi may have provider-side retention governed by their own services and account terms.

Claude's Anthropic traffic never passes through ai-worker. ai-router does not read Claude credentials, copy provider credentials, make direct provider HTTP calls, or provide a general-purpose shell endpoint. The worker supervisor works without any dashboard; a dashboard is not implemented yet.

## Tests

Run offline tests without provider calls:

```bash
cd /home/krakadin/myDev/ai-router
/usr/bin/python3 -B -m unittest discover -s tests
```

Live provider calls are explicit: `ai-worker test qwen` and `ai-worker test kimi` make small requests. Normal tests never contact a provider.

See [architecture](docs/architecture.md), [security](docs/security.md), [operations](docs/operations.md), and [the security checkpoint](SECURITY_CHECKPOINT.md).
