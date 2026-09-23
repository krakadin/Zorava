We completed the read-only audit in `AUDIT.md`. Read that file completely before doing anything.

I now want you to IMPLEMENT the multi-model worker system described below.

This is no longer a discovery-only task, but implementation must be incremental, reversible, security-conscious, and verified at every stage.

# 1. Objective

Build a local multi-model development system where:

- Claude Code remains my normal primary CLI.
- Claude remains the MAIN AGENT / ORCHESTRATOR.
- Claude continues using my existing Anthropic/Claude Max OAuth directly.
- DO NOT proxy normal Claude traffic through LiteLLM or another model gateway.
- Qwen is a delegated worker using the already-installed Qwen CLI and its existing provider configuration.
- Kimi K3 is a delegated worker using the already-installed Kimi CLI and its existing Kimi OAuth/configuration.
- Claude can invoke Qwen and Kimi, provide bounded tasks/context, receive structured results, and use those results in its own reasoning.
- The existing standalone `claude`, `qwen`, and `kimi` commands must continue working normally.
- Add a small localhost-only dashboard for worker health, authentication status, jobs, logs, tests, and cancellation.
- The worker system must function without the dashboard running.

The desired architecture is:

```text
User
 |
 v
Claude Code
 |
 |-- MAIN ----------------------> Anthropic
 |                                Claude
 |                                existing Max OAuth
 |
 |-- Qwen worker ---------------> local qwen CLI
 |                                qwen3.8-max
 |                                existing DashScope configuration
 |
 `-- Kimi worker ---------------> local kimi CLI
                                  kimi-code/k3
                                  existing Kimi OAuth

```

The dashboard is a CONTROL AND OBSERVABILITY layer:

```text
                 Local Dashboard
                 127.0.0.1 only
                       |
                       v
Claude --------> Worker Supervisor
                    |       |
                    v       v
                  Qwen     Kimi

```

The dashboard must NOT become required for Claude delegation.

# 2. Important facts from the completed audit

Verify these against `AUDIT.md` before implementation.

Current Claude:

- executable: `~/.local/bin/claude`
- current native version discovered: 2.1.281
- main configured model: `claude-fable-5-1[1m]`
- authentication: Claude Max OAuth
- no `ANTHROPIC_BASE_URL` override
- no LiteLLM
- no alternate provider override
- Claude must remain on this normal direct path

Current Qwen:

- executable to use explicitly: `/home/krakadin/.local/bin/qwen`
- active model: `qwen3.8-max`
- current backend: Alibaba Model Studio / international DashScope
- OpenAI-compatible endpoint
- credential reference: `DASHSCOPE_API_KEY`
- supports noninteractive prompts
- supports JSON and stream-JSON output
- supports wall-time/tool-call limits
- supports ACP
- existing older Qwen installations exist, so USE THE ABSOLUTE PATH above

Current Kimi:

- executable: `/home/krakadin/.kimi-code/bin/kimi`
- configured model alias: `kimi-code/k3`
- API model: `k3`
- Kimi Code managed service
- existing file-based OAuth
- supports noninteractive prompts
- supports stream-JSON
- supports explicit agent profiles/tool allowlists
- supports ACP
- executable was 2.0.0 during the audit
- 2.0.2 was staged and may activate when Kimi next starts

Important:

Before invoking Kimi for the first time, determine what version will actually run and record it. Do not silently assume the audit version remains active.

# 3. Security issue that MUST be addressed first

The audit found the current DashScope API key duplicated into two Qwen transcript files and stored in Qwen settings.

DO NOT print the key.

DO NOT include it in logs.

DO NOT include it in command lines.

DO NOT copy it into this project's configuration.

Before normal implementation proceeds:

1. Identify the affected files from `AUDIT.md`.
2. Confirm permissions and exposure without printing the secret.
3. Explain to me what must be rotated.
4. If key rotation requires me to perform an external web/account action, STOP at that point and give me exact instructions.
5. Resume implementation only after I confirm rotation is complete.
6. Then verify the replacement Qwen credential works.
7. Restrict secret-bearing Qwen files appropriately.
8. Ensure our new system never records provider credentials.

Do NOT delete historical files containing the old credential until I approve the cleanup plan.

The system should redact common credential/token patterns before anything reaches persistent logs.

# 4. Do NOT install LiteLLM

The audit concluded LiteLLM is unnecessary for this architecture.

Do not install:

- LiteLLM
- OpenRouter infrastructure
- another model gateway
- Redis
- PostgreSQL
- Docker containers
- a separate database server

unless a verified technical blocker makes one necessary.

If you encounter such a blocker, STOP and explain it rather than changing architecture automatically.

# 5. Project location

Use:

```text
/home/krakadin/myDev/ai-router

```

for source code.

Runtime state must NOT live inside Git.

Use:

```text
/home/krakadin/.local/state/ai-workers/

```

for runtime state.

Suggested structure:

```text
ai-router/
  README.md
  .gitignore

  bin/
    ai-worker
    worker.py

  workers/
    __init__.py
    base.py
    qwen.py
    kimi.py

  profiles/
    qwen-researcher.md
    kimi-coder.md

  dashboard/
    server.py
    templates/
    static/

  tests/
    ...

  docs/
    architecture.md
    security.md
    operations.md

```

Runtime:

```text
~/.local/state/ai-workers/
  jobs/
  logs/
  tmp/
  workers.db

```

Use SQLite if persistent structured job metadata is useful.

Do not store credentials in SQLite.

# 6. Keep dependencies minimal

Prefer Python standard library.

A very small, well-maintained dependency is acceptable if it substantially improves correctness, but explain why before adding it.

Do not introduce a large web framework unless justified.

For the first dashboard, a small Python HTTP server is sufficient if implemented safely.

The entire system should remain understandable and maintainable.

# 7. Worker supervisor

Implement ONE supervisor boundary that controls both Qwen and Kimi.

Conceptually:

```text
Claude
   |
   v
worker.py
   |
   +--> Qwen adapter
   |
   `--> Kimi adapter

```

Claude should not construct arbitrary Qwen/Kimi shell commands.

The supervisor owns:

- executable paths
- model selection
- working directories
- timeouts
- environment filtering
- process groups
- cancellation
- tool restrictions
- output limits
- JSON parsing
- logging
- redaction
- error classification

Never invoke workers through:

```text
shell=True

```

Use argument arrays.

# 8. Supervisor request protocol

Accept JSON on stdin.

Example:

```json
{
  "worker": "qwen",
  "task": "Investigate how authentication works in this repository.",
  "cwd": "/home/krakadin/myDev/example",
  "mode": "read-only",
  "timeout_seconds": 300
}

```

Support initially:

```text
worker:
  qwen
  kimi

mode:
  read-only

```

DO NOT enable project editing in phase one.

Validate:

- worker name
- cwd
- cwd exists
- cwd is a permitted project location
- timeout range
- task length
- input size

Reject malformed requests.

# 9. Supervisor result protocol

Return structured JSON.

Example:

```json
{
  "job_id": "...",
  "worker": "qwen",
  "requested_model": "qwen3.8-max",
  "status": "completed",
  "started_at": "...",
  "completed_at": "...",
  "duration_ms": 12345,
  "exit_code": 0,
  "result": "...",
  "error": null
}

```

Statuses should distinguish at least:

```text
queued
running
completed
failed
timed_out
cancelled
auth_error
rate_limited
invalid_output

```

Do not report a model as actually used unless the worker output/API exposes enough information to establish that.

Keep requested model and reported model separate.

# 10. Qwen worker

Use:

```text
/home/krakadin/.local/bin/qwen

```

Initial role:

```text
RESEARCH / INVESTIGATION / SECOND OPINION

```

Requested model:

```text
qwen3.8-max

```

Initial permissions:

```text
READ ONLY

```

Use Qwen's safest available plan/read-only mode.

Use:

- structured output
- wall-clock limit
- tool-call limit
- explicit working directory
- explicit model

Pass substantial task content through stdin, NOT the process command line.

The Qwen worker should be useful for:

- repository investigation
- architecture investigation
- code tracing
- logs
- requirements
- second opinions
- reviewing a proposed implementation
- identifying affected files
- producing recommendations for Claude

It must NOT initially:

- edit project files
- commit
- push
- deploy
- change configuration
- rotate credentials
- install packages

# 11. Kimi worker

Use:

```text
/home/krakadin/.kimi-code/bin/kimi

```

Requested model:

```text
kimi-code/k3

```

Initial role:

```text
CODING ANALYSIS / DEBUGGING / PATCH DESIGN

```

Initial permissions:

```text
READ ONLY

```

Create a dedicated Kimi agent profile.

The profile should permit only tools necessary for reading and investigating code.

Conceptually:

```text
Read
Grep
Glob

```

Use the exact supported tool names discovered in the installed Kimi version.

Do NOT guess tool identifiers.

Kimi should initially:

- inspect code
- diagnose defects
- design implementations
- propose patches/diffs in its RESPONSE
- identify tests to run
- review code

Kimi should NOT initially:

- modify source files
- commit
- push
- deploy
- install dependencies
- modify credentials

Use stream-JSON or another machine-readable output mode.

Avoid putting large prompts/context directly in process arguments.

# 12. Process management

Each worker job must run in its own process group.

Implement:

- timeout
- cancellation
- SIGTERM
- grace period
- SIGKILL fallback
- child cleanup
- stdout/stderr capture
- bounded output

If the supervisor exits unexpectedly, avoid leaving uncontrolled worker processes behind where practical.

Do not kill unrelated existing `qwen`, `kimi`, or `claude` sessions.

Track only processes created by this supervisor.

# 13. Environment isolation

Do not blindly pass the entire Claude environment to workers.

Construct an intentional child environment.

Qwen should receive only what it needs plus safe normal process variables.

Kimi should receive only what it needs plus safe normal process variables.

Never send:

- Anthropic OAuth/token material to Qwen
- Anthropic OAuth/token material to Kimi
- Qwen API credentials to Kimi
- Kimi OAuth/token material to Qwen

Be especially careful with inherited environment variables.

Document the final allowlist strategy.

# 14. Logging

Implement structured sanitized logs.

For each job record:

- job ID
- worker
- requested model
- project/cwd
- mode
- start
- finish
- duration
- exit status
- high-level error
- sanitized worker result/output

NEVER log:

- API keys
- OAuth access tokens
- OAuth refresh tokens
- authorization headers
- cookies
- private keys
- complete credential files

Redaction must happen BEFORE persistent storage.

Do not rely on the dashboard hiding secrets.

# 15. Local dashboard

Build a small local dashboard.

Bind ONLY to:

```text
127.0.0.1

```

Never:

```text
0.0.0.0

```

Suggested port:

```text
8787

```

Dashboard URL:

```text
http://127.0.0.1:8787

```

The worker system must continue functioning if the dashboard is stopped.

# 16. Dashboard home page

Show three provider cards:

```text
Claude
Qwen
Kimi

```

For each show:

- status
- executable
- configured/requested model
- provider/backend
- authentication TYPE
- credential presence/status
- version
- last successful test
- last failure

Never show actual credentials.

Example:

```text
Claude
READY
Model: claude-fable-5-1[1m]
Auth: Max OAuth
Provider: Anthropic

Qwen
READY
Model: qwen3.8-max
Auth: API key present
Provider: DashScope

Kimi
READY
Model: kimi-code/k3
Auth: OAuth
Provider: Kimi Code

```

# 17. Authentication handling

The dashboard must NOT become an OAuth implementation or credential vault.

For authentication failures:

Kimi:

```text
Authentication expired
[ Re-authenticate ]

```

The action should invoke or instruct the OFFICIAL Kimi authentication flow.

Claude:

```text
Authentication required
[ Open Claude login instructions ]

```

Use the official Claude CLI flow.

Qwen:

```text
Credential missing/invalid
[ Show configuration instructions ]

```

Do not build custom secret storage unless necessary.

If a provider requires terminal interaction, tell the user what command to run rather than trying to capture secrets in the browser.

# 18. Dashboard jobs view

Show:

```text
ACTIVE JOBS

Worker    Role        Project      Duration   Status
Kimi      coding      project-a    01:42      running
Qwen      research    project-b    00:31      running

```

Actions:

```text
View
Cancel

```

Recent jobs should show:

- worker
- project
- task summary
- status
- duration
- timestamp

# 19. Job detail page

Tabs or sections:

```text
Summary
Worker Output
Errors
Raw Sanitized Log

```

Show:

- parent/orchestrator: Claude
- worker
- requested model
- project
- task
- permissions/mode
- start/end
- duration
- exit
- result

Never expose credentials.

# 20. Dashboard test actions

Provide:

```text
Test Qwen
Test Kimi

```

Each sends a harmless task such as:

```text
Reply with exactly WORKER_OK. Do not call tools.

```

Report:

- success/failure
- duration
- requested model
- worker version
- sanitized error

A Claude test may simply verify Claude configuration/status rather than generating unnecessary provider traffic.

# 21. Permissions page

Show current worker permissions clearly.

Initial state:

```text
Qwen Researcher

Filesystem:
READ ONLY

Shell:
restricted/off where possible

Git writes:
OFF

Push:
OFF

Deploy:
OFF

```

and:

```text
Kimi Coder

Filesystem:
READ ONLY

Shell:
OFF or strictly restricted

Git writes:
OFF

Push:
OFF

Deploy:
OFF

```

Do not build interactive approvals yet.

Hard restrictions are preferred in phase one.

# 22. Claude integration

After standalone Qwen and Kimi worker tests succeed, integrate delegation with Claude.

Prefer a user-level Claude skill/instruction mechanism so Claude knows:

- when Qwen is appropriate
- when Kimi is appropriate
- how to invoke the supervisor
- how to consume results
- that Claude remains responsible for the final answer/decision
- that workers are advisory/delegated agents
- that worker failures should be reported rather than hidden

Possible conceptual tools:

```text
delegate-qwen
delegate-kimi

```

Claude should not need to know provider credentials.

Claude should not set:

```text
ANTHROPIC_BASE_URL

```

for this system.

Claude's normal Anthropic path must remain unchanged.

# 23. Delegation policy

Initial policy:

Use Qwen for:

- research
- codebase exploration
- tracing
- architecture
- log investigation
- requirements analysis
- independent review

Use Kimi K3 for:

- coding analysis
- debugging
- implementation design
- patch proposals
- test design
- code review

Claude:

- orchestrates
- decides which worker to call
- reviews worker output
- reconciles disagreements
- communicates with me
- remains the main agent

Do not automatically delegate every task.

# 24. Do NOT enable editing yet

Phase one workers are READ ONLY.

Once everything is stable, phase two may add:

```text
Kimi editing worker
    |
    `--> isolated Git worktree

```

But do not implement editing until the read-only system has passed verification.

Design the supervisor so a future:

```text
mode: isolated-edit

```

can be added cleanly.

# 25. Future isolated editing design

Document but DO NOT activate:

- one Git worktree per editing job
- worker cannot modify primary checkout
- worker cannot push
- worker cannot deploy
- worker may produce commits only if explicitly enabled later
- Claude reviews diff
- user controls final merge

Do not treat Git worktrees as security isolation by themselves.

# 26. Database/state

If using SQLite, store only metadata such as:

- jobs
- timestamps
- statuses
- worker
- model
- cwd
- sanitized task
- sanitized result
- exit information

Never store credentials.

Runtime database:

```text
~/.local/state/ai-workers/workers.db

```

# 27. CLI commands

Create a convenient local command such as:

```text
ai-worker

```

Examples:

```text
ai-worker status

ai-worker test qwen

ai-worker test kimi

ai-worker jobs

ai-worker show JOB_ID

ai-worker cancel JOB_ID

ai-worker dashboard

```

And delegation:

```text
printf '%s' '<json>' | ai-worker run

```

or an equally safe structured interface.

# 28