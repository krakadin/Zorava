# Multi-model Claude Code Environment Audit

Audit date: 2026-09-23.

This report records the completed read-only discovery pass. This Markdown file was created afterward at the user's request. Statements about making no changes or the directory being empty describe the discovery pass, before this report was saved.

## 1. Executive Summary

**[INFERENCE] Recommendation: use existing CLI delegation. LiteLLM is unnecessary for the requested architecture.** Claude can remain the orchestrator, while local wrapper tools invoke Qwen and Kimi using their separate existing configurations.

**[VERIFIED LOCALLY] Current configuration:**

| Role | Configured model | Backend | Authentication |
|---|---|---|---|
| Claude main | `claude-fable-5-1[1m]` | Normal Anthropic configuration; no endpoint override found | Claude Max OAuth |
| Qwen worker | `qwen3.8-max` | Alibaba Model Studio / international DashScope | `DASHSCOPE_API_KEY`, stored in Qwen settings |
| Kimi worker | CLI alias `kimi-code/k3`; API model `k3` | Kimi Code managed service | File-based OAuth |

**[VERIFIED LOCALLY] Two findings need attention before implementation:**

- The current DashScope key appears in **two Qwen chat transcripts**. Those transcripts and Qwen's settings file have `0664` permissions. **Rotate that key and secure the affected files.**
- Kimi's current executable embeds **2.0.0**, while **2.0.2 is staged for installation**. Its startup code can apply staged updates before processing ordinary options.

**[VERIFIED LOCALLY] This audit made no implementation changes.** No packages were installed, configurations edited, credentials rotated, or existing services restarted. No authenticated provider calls or worker sessions were launched. The `ai-router` directory remains empty.

**[UNKNOWN / NEEDS TESTING]** Current account entitlement, token validity, successful model requests, and complete delegation workflows remain untested.

## 2. Installed Components

**[VERIFIED LOCALLY] System inventory:**

| Component | Finding |
|---|---|
| OS | Ubuntu 24.04.5 LTS |
| Kernel | `7.0.0-31-generic` |
| Architecture | `x86_64` |
| User | `krakadin` |
| Shell | `/bin/bash` |
| Node.js on PATH | `v22.23.1` |
| npm | `10.9.8` |
| System Python | `3.12.3` |
| pip | `24.0` |
| pipx | Not found |
| uv | `0.12.5` |
| Additional Python | uv-managed Python `3.13.14` found |
| Docker | `28.3.3`; existing containers running |
| Podman | `4.9.3` |
| systemd user manager | Running |
| Bubblewrap | Installed; namespace probes failed in this execution environment |

**[VERIFIED LOCALLY] Observed audit-shell PATH:**

```text
/home/krakadin/.nvm/versions/node/v22.23.1/lib/node_modules/@openai/codex/node_modules/@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl/codex-path
/home/krakadin/.local/bin
/home/krakadin/.codex/tmp/arg0/codex-arg0GXK38l
/home/krakadin/.nvm/versions/node/v22.23.1/lib/node_modules/@openai/codex/node_modules/@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl/codex-path
/home/krakadin/.kimi-code/bin
/home/krakadin/.local/bin
/home/krakadin/.grok/bin
/home/krakadin/.nvm/versions/node/v22.23.1/bin
/home/krakadin/.local/bin
/usr/local/sbin
/usr/local/bin
/usr/sbin
/usr/bin
/sbin
/bin
/usr/games
/usr/local/games
/snap/bin
```

**[VERIFIED LOCALLY] CLI installations, determined from executable resolution, metadata, and embedded code:**

| CLI | Resolved executable | Version and installation |
|---|---|---|
| Claude | `~/.local/bin/claude` | Symlink to `~/.local/share/claude/versions/2.1.281`; native installation |
| Qwen | `~/.local/bin/qwen` | Shell wrapper → `~/.local/lib/qwen-code/bin/qwen`; standalone distribution of `@qwen-code/qwen-code` **0.24.4**, with bundled Node |
| Kimi | `~/.kimi-code/bin/kimi` | Standalone native executable; embedded build **2.0.0** |
| Codex | `~/.nvm/versions/node/v22.23.1/bin/codex` | Symlink into npm package `@openai/codex` **0.156.1** |
| LiteLLM | Not found | No matching installation found in inspected PATH, package locations, or containers |

**[VERIFIED LOCALLY] Additional versions:**

- Claude binaries: `2.1.276`, `2.1.278`, `2.1.280`, `2.1.281`. An existing Claude process is running **2.1.280**.
- Older Qwen npm installation: **0.19.9**, under Node `v20.19.5`.
- Older Codex npm installation: **0.147.0**, under Node `v20.19.5`.
- Kimi **2.0.2** executable and staging manifest exist under `~/.kimi-code/bin/.staging/`.

**[INFERENCE]** Absolute executable paths should be used in future wrappers to avoid accidentally selecting the older Qwen installation.

## 3. Claude Configuration

**[VERIFIED LOCALLY]** [Claude settings](/home/krakadin/.claude/settings.json) specify:

```text
model: claude-fable-5-1[1m]
effortLevel: high
enabled plugin: security-guidance@claude-plugins-official
skipDangerousModePermissionPrompt: true
```

The enabled plugin's installed version is **2.0.8**.

**[VERIFIED LOCALLY] Authentication evidence:**

- `~/.claude/.credentials.json` contains Claude OAuth access/refresh credentials.
- Subscription metadata identifies **Max**, with the `default_claude_max_20x` tier.
- No Anthropic API key or alternative provider override was found in the inspected applicable settings.
- The existing Claude process has these variables **absent**:

```text
ANTHROPIC_BASE_URL
ANTHROPIC_MODEL
ANTHROPIC_AUTH_TOKEN
ANTHROPIC_API_KEY
CLAUDE_CODE_SUBAGENT_MODEL
CLAUDE_CODE_USE_BEDROCK
CLAUDE_CODE_USE_VERTEX
CLAUDE_CODE_USE_FOUNDRY
```

HTTP proxy variables were also absent from that process.

**[INFERENCE]** The inspected setup uses normal Anthropic subscription authentication and the standard Anthropic API destination, `https://api.anthropic.com`. No gateway configuration was found. Actual request traffic was not captured.

**[VERIFIED LOCALLY] Scope and agents:**

- `ai-router` has no project configuration or instruction files.
- `~/myDev/.claude/settings.local.json` exists and contains three permission allowances.
- No `~/.claude/agents/` directory was found.
- The running Claude process belongs to `~/myDev/placeorbit_starter`.
- That project has **23 custom agents**. `contract-guardian` and `marika` specify `sonnet`; the others omit a model and inherit.
- That project also has a Bash `PreToolUse` hook referencing `.claude/hooks/pre-commit-claude.sh`.

These project agents do not automatically become `ai-router` agents.

**[VERIFIED FROM OFFICIAL DOCUMENTATION]** Subagent frontmatter supports `model:` with aliases, full model IDs, and `inherit`. Current documented aliases include `sonnet`, `opus`, `haiku`, and `fable`. The field is not limited to the historical three-alias enum. [Claude subagent documentation](https://code.claude.com/docs/en/sub-agents)

**[VERIFIED LOCALLY]** Installed code contains model-frontmatter parsing that preserves nonempty strings, plus alias resolution and model-availability checks.

**[UNKNOWN / NEEDS TESTING]** Whether the literal names `qwen-worker` or `kimi-worker` survive every validation step and reach a gateway has not been exercised.

**[INFERENCE]** A model string alone cannot select Qwen's or Kimi's credentials and endpoint. No supported per-subagent provider/base-URL/authentication selector was found. Native subagent requests should be treated as using the parent's provider configuration.

## 4. Qwen Configuration

**[VERIFIED LOCALLY]** [Qwen settings](/home/krakadin/.qwen/settings.json) contain:

| Setting | Value |
|---|---|
| Active authentication type | `openai` |
| Provider metadata | `alibabaStandard` |
| Active model | `qwen3.8-max` |
| Base URL | `https://dashscope-intl.aliyuncs.com/compatible-mode/v1` |
| Credential reference | `DASHSCOPE_API_KEY` |
| Credential storage | Actual key stored in `settings.json` under `env` |
| Configured context window | `1,000,000` tokens |
| Reasoning capability | Enabled/supported |
| Advertised reasoning efforts | `low`, `medium`, `xhigh` |
| Configured default effort | `xhigh` |
| Image/video capability metadata | Enabled |

**[VERIFIED FROM OFFICIAL DOCUMENTATION]** This hostname is Alibaba Model Studio's international/Singapore DashScope endpoint, using the OpenAI-compatible API. It is distinct from a finding that the CLI currently uses your QwenCloud account. [Alibaba endpoint reference](https://help.aliyun.com/en/model-studio/base-url)

**[UNKNOWN / NEEDS TESTING]** Whether the configured key and your `home.qwencloud.com` account share billing or account ownership cannot be determined from these local settings.

**[VERIFIED LOCALLY] Configured alternative Qwen models:**

```text
qwen3.6-plus
qwen3.7-plus
qwen3.7-max
qwen3.8-max-0902
qwen3.8-flash
```

The same provider list also includes GLM, DeepSeek, and Kimi models, including `kimi-k3`. That is a separate possible DashScope route, not the existing Kimi Code OAuth route.

**[VERIFIED LOCALLY] Installed tooling supports:**

- Non-interactive prompts through `-p` or stdin.
- `text`, `json`, and `stream-json` output.
- Tool/function-calling infrastructure.
- MCP client configuration.
- ACP through `qwen --acp`.
- Wall-clock and tool-call budgets.
- Permission modes and tool restrictions.
- A bundled `@qwen-code/acp-bridge` component.

No enabled user extensions or existing Claude-to-Qwen worker bridge was found.

**[VERIFIED FROM OFFICIAL DOCUMENTATION]** Headless JSON output is a message array ending in a result record; streaming JSON supports incremental consumption. [Qwen headless documentation](https://qwenlm.github.io/qwen-code-docs/en/users/features/headless/)

**[UNKNOWN / NEEDS TESTING]** The configured model's successful tool calls, actual context allowance, and effective reasoning behavior require a live request.

## 5. Kimi/K3 Configuration

**[VERIFIED LOCALLY]** [Kimi configuration](/home/krakadin/.kimi-code/config.toml) contains:

| Setting | Value |
|---|---|
| Default CLI model alias | `kimi-code/k3` |
| API model identifier | `k3` |
| Provider | `managed:kimi-code` |
| Provider implementation | `kimi` |
| Base URL | `https://api.kimi.com/coding/v1` |
| Authentication | Managed OAuth, file storage |
| Static API key | Empty |
| Configured context window | `1,048,576` tokens |
| Thinking | Enabled |
| Effort | `high` |
| Supported efforts | `low`, `high`, `max` |
| Capabilities | Tool use, image/video input, dynamically loaded tools |

Other configured aliases are `kimi-code/k3-256k`, `kimi-code/kimi-for-coding`, and `kimi-code/kimi-for-coding-highspeed`.

**[VERIFIED LOCALLY]** K3 is already represented and selected in your configuration. No model migration is needed to select it.

**[UNKNOWN / NEEDS TESTING]** Current account entitlement and a successful K3 request remain unverified.

**[VERIFIED FROM OFFICIAL DOCUMENTATION]** Kimi Code exposes both:

- OpenAI-compatible API: `https://api.kimi.com/coding/v1`
- Anthropic-compatible API: `https://api.kimi.com/coding/`

Its documented K3 API IDs include `k3` and `k3-256k`. [Kimi model documentation](https://www.kimi.com/code/docs/en/kimi-code/models.html)

**[VERIFIED FROM OFFICIAL DOCUMENTATION]** The configured `kimi` provider uses an OpenAI-compatible interface. The CLI's separate support for an `openai_responses` provider does **not** establish that this Kimi endpoint supports Responses. [Kimi provider documentation](https://www.kimi.com/code/docs/en/kimi-code-cli/configuration/providers.html)

**[UNKNOWN / NEEDS TESTING]** Responses API support at the configured endpoint was not established.

**[VERIFIED LOCALLY]** The installed executable supports:

```text
-m, --model
-p, --prompt
--output-format text|stream-json
--agent-file
kimi acp
```

**[VERIFIED FROM OFFICIAL DOCUMENTATION]** Kimi supports MCP as a **client**. That does not make `kimi` an MCP server Claude can connect to directly. [Kimi MCP documentation](https://www.kimi.com/code/docs/en/kimi-code-cli/customization/mcp.html)

**[VERIFIED LOCALLY] Version caveat:** the executable is **2.0.0**; **2.0.2 is staged**. Startup includes update activation and cache maintenance before ordinary command handling. I therefore did not launch it for version/help inspection.

## 6. Existing ACP/MCP/Agent Infrastructure

**[VERIFIED LOCALLY] Found:**

- Qwen's ACP server and bundled ACP bridge.
- Kimi's ACP server implementation.
- Claude's normal tool/MCP infrastructure.
- Claude's enabled security-guidance plugin and lifecycle hooks.
- Synced finance-plugin MCP definitions, including BigQuery and Slack, and corresponding saved OAuth metadata.
- Project-specific Claude agents in `placeorbit_starter` and related worktrees.

Synced or cached plugin files alone do not prove that every listed connector is active.

**[VERIFIED LOCALLY] Not found in inspected locations:**

- A standalone `claude-agent-acp` installation.
- A Claude-to-Qwen or Claude-to-Kimi worker wrapper.
- An ACP-to-MCP delegation bridge.
- A LiteLLM installation or running AI routing gateway.
- Relevant routing aliases or user services.
- OpenRouter or custom localhost model endpoints in the inspected active AI configurations.

**[VERIFIED LOCALLY]** Listening ports were accounted for by existing development infrastructure: MinIO, Redis, ClamAV, Loki, Mailpit, Meilisearch, printing, and DNS. No AI gateway was identified.

**[VERIFIED FROM OFFICIAL DOCUMENTATION]** `claude-agent-acp` exposes Claude to an ACP client. Installing it would not by itself make Claude an ACP client that dispatches Qwen/Kimi. [Official adapter repository](https://github.com/agentclientprotocol/claude-agent-acp)

**[INFERENCE]** The worker executables already provide the necessary capabilities. A small invocation/result-handling layer is missing; a model router is not.

## 7. Authentication and Security Findings

**[VERIFIED LOCALLY] Credential files:**

| File | Owner/group | Mode | Finding |
|---|---|---:|---|
| `~/.claude/.credentials.json` | `krakadin:krakadin` | `0600` | Plaintext OAuth credentials, including connector credentials |
| `~/.qwen/settings.json` | `krakadin:krakadin` | `0664` | Plaintext DashScope API key |
| `~/.kimi-code/credentials/kimi-code.json` | `krakadin:krakadin` | `0600` | Plaintext OAuth access/refresh credentials |
| `~/.kimi-code/server.token` | `krakadin:krakadin` | `0600` | Local server token |
| `~/.kimi-code/oauth/kimi-code` | `krakadin:krakadin` | `0664` | Empty legacy file; no credential found |

`~/.claude.json` is `0600` and contains sensitive account/configuration metadata. `~/.kimi-code` is `0700`.

**[VERIFIED LOCALLY] Confirmed exposure:** the exact current DashScope key occurs in these two files:

```text
~/.qwen/projects/-home-krakadin-myDev-placeorbit-starter/chats/
  36af78eb-9bb1-41fe-99cd-8df0545a7b24.jsonl
  32b706e0-884d-4374-904b-1b95da90608d.jsonl
```

Both files are `0664`, owned by `krakadin`. Your home directory is `0755`, and `~/.qwen` is `0775`.

**[INFERENCE] Recommended remediation before implementation:**

1. Rotate the DashScope key.
2. Restrict Qwen's secret-bearing settings and transcripts to owner access.
3. Review and securely remove or redact exposed transcript copies and backups.
4. Keep the replacement credential out of prompts, command arguments, Git, and transcript-producing config dumps.
5. Prefer a protected credential source loaded only for Qwen processes.

Do not remove the existing key from Qwen settings until its replacement mechanism has been verified.

**[VERIFIED LOCALLY] History checks:**

- No occurrence of the current DashScope key in `.bash_history`.
- One secret-assignment pattern was found, but its assigned value was empty.
- No token-literal patterns were found in that shell history.

**[VERIFIED LOCALLY] Scan limits:** exact-key searches covered selected Claude, Qwen, Kimi, and Codex history/log locations. Large Claude and Codex session trees were capped at approximately 600 MB each; some files were skipped.

**[UNKNOWN / NEEDS TESTING]** This is not an exhaustive secret scan of all historical keys, Git history, backups, or remote copies.

**[VERIFIED LOCALLY] Git findings:**

- `ai-router` is empty, is not a Git repository, and has no `.gitignore`.
- Across 17 nearby repositories/worktrees, the selected credential/config filename checks found no tracked candidates.
- `placeorbit_starter` ignores `.env`, `.env.*`, and private Claude configuration, while intentionally tracking agent definitions and selected hooks/settings.
- Its ignore rules do not cover the tested `.qwen/settings.json`, Kimi credential paths, or generic `.credentials.json`.

**[VERIFIED LOCALLY]** The existing Claude process was launched with `--dangerously-skip-permissions`.

**[INFERENCE]** Future worker safeguards must be enforced by the wrappers and worker profiles; they should not depend on that Claude session prompting before every invocation.

## 8. Architecture A: Native Claude Subagents

**[INFERENCE] Verdict: unsuitable for direct independent provider routing.**

Native Claude subagents can select models and tools, but no supported subagent-specific endpoint/credential configuration was found.

**[VERIFIED LOCALLY]** The installed model handling is broader than a fixed Claude-alias enum.

**[UNKNOWN / NEEDS TESTING]** Arbitrary gateway labels may still encounter recognition, availability, or provider checks. Merely writing:

```yaml
model: qwen-worker
```

does not establish a Qwen worker.

**[VERIFIED FROM OFFICIAL DOCUMENTATION]** `ANTHROPIC_BASE_URL` changes the request destination for Claude Code. It is a session/provider setting, not a documented per-subagent provider selector. [Claude model configuration](https://code.claude.com/docs/en/model-config)

**[INFERENCE]** Setting it globally would put the main Claude path through that endpoint as well, contrary to your preference.

## 9. Architecture B: Existing CLI/ACP/MCP Delegation

**[INFERENCE] Verdict: best fit.**

Claude invokes a local worker process, passes a bounded task, and receives its structured result. Each worker retains its own authentication and model configuration.

| Concern | Proposed behavior |
|---|---|
| Invocation | Claude Bash tool → local Python supervisor → absolute CLI path |
| Qwen input | Prompt/context through stdin |
| Kimi input | Short `-p` instruction; larger context in a private task file |
| Output | Parse JSON/JSONL; return a normalized result |
| Permissions | Qwen plan/restricted-tool mode; explicit Kimi agent tool allowlist |
| Initial file handling | Read-only analysis and proposed patches |
| Later editing | Separate worktree per editing worker, after permission tests |
| Concurrency | Initially one job per backend; separate task state |
| Timeout | Supervisor deadline; terminate and reap its process group |
| Failures | Explicit timeout/auth/rate-limit/invalid-output/partial-result status |
| Retry policy | Bounded retries for safe requests; no blind retry after edits |

**[VERIFIED LOCALLY]** Qwen has `--max-wall-time` and `--max-tool-calls`; its installed documentation warns that some budgets do not apply identically to ACP daemon sessions.

**[VERIFIED FROM OFFICIAL DOCUMENTATION]** Kimi prompt mode does not request ordinary interactive approvals; static denials remain relevant. Explicit tool restrictions are therefore necessary for unattended workers. [Kimi command reference](https://www.kimi.com/code/docs/en/kimi-code-cli/reference/kimi-command)

**[INFERENCE]** ACP is technically capable of driving both workers, but it requires a client/bridge. A short-lived CLI process is simpler for the initial workflow.

**[INFERENCE]** MCP is appropriate if you want named tools such as `qwen_researcher` and `kimi_coder`. A small stdio MCP server can wrap the same supervisor later. Claude officially supports local stdio MCP servers. [Claude MCP documentation](https://code.claude.com/docs/en/mcp)

**[UNKNOWN / NEEDS TESTING]** Tool restrictions are not proven filesystem isolation. A worktree also does not prevent access to the rest of your home directory. Bubblewrap isolation could not be validated here.

## 10. Architecture C: LiteLLM Router

**[INFERENCE] Verdict: technically plausible, operationally unnecessary here.** This is a fallback assessment, not a recommendation to install it.

| Question | Finding |
|---|---|
| Installed? | **[VERIFIED LOCALLY]** No installation found |
| Current stable release? | **[VERIFIED FROM OFFICIAL DOCUMENTATION]** GitHub's latest release resolved to **v1.102.1** |
| Installation method? | **[VERIFIED FROM OFFICIAL DOCUMENTATION]** Official CLI instructions use `uv tool install 'litellm[proxy]'` |
| Anthropic `/v1/messages`? | **[VERIFIED FROM OFFICIAL DOCUMENTATION]** Supported |
| Exact DashScope base URL? | **[VERIFIED FROM OFFICIAL DOCUMENTATION]** Listed by LiteLLM's DashScope provider |
| `qwen3.8-max` behavior? | **[UNKNOWN / NEEDS TESTING]** Exact reasoning/tool/streaming behavior through LiteLLM |
| Kimi Code `k3` endpoint? | **[INFERENCE]** Candidate through OpenAI-compatible or Anthropic-compatible routing |
| Existing Kimi OAuth reuse? | **[UNKNOWN / NEEDS TESTING]** No verified LiteLLM integration with this CLI's managed OAuth store |
| Arbitrary subagent routing labels? | **[UNKNOWN / NEEDS TESTING]** Must test installed Claude validation and outgoing model IDs |

Sources: [release](https://github.com/BerriAI/litellm/releases/tag/v1.102.1), [installation](https://docs.litellm.ai/docs/proxy/quick_start), [Claude Messages support](https://docs.litellm.ai/docs/proxy/client_setup/claude_code), [DashScope support](https://docs.litellm.ai/docs/providers/dashscope), [OpenAI-compatible routing](https://docs.litellm.ai/docs/providers/openai_compatible).

**[INFERENCE] Protocol translation:**

- Routing Anthropic Messages to Qwen's **currently configured** OpenAI-compatible endpoint would require translation.
- Kimi advertises an Anthropic-compatible endpoint, so translation is not inherently required if that route is selected.
- Exact feature compatibility still requires testing.

**[VERIFIED FROM OFFICIAL DOCUMENTATION]** LiteLLM now documents Claude Max OAuth passthrough, forwarding the client's OAuth authorization to Anthropic. Therefore, a separate Anthropic API key is **not universally required**. This is LiteLLM's documented capability, not a locally tested deployment. [LiteLLM Max subscription guide](https://docs.litellm.ai/docs/tutorials/claude_code_max_subscription)

**[INFERENCE]** Such a router would handle additional sensitive headers, traffic, logs, credentials, updates, and availability concerns. Anthropic credentials must never be forwarded to worker providers.

**[INFERENCE]** A future router could serve **only worker processes**, leaving the main Claude process direct. That would still require the external-worker boundary proposed in Architecture B.

## 11. Recommended Architecture for This Machine

**[INFERENCE] Technical ranking:**

1. Existing CLIs with small wrappers.
2. The same wrappers exposed through stdio MCP.
3. An ACP client bridge, if richer interactive sessions become necessary.
4. A worker-only router for a future requirement the CLIs cannot meet.

Native independent-provider Claude subagents are not established as an available option.

```text
User
 |
 v
Claude Code
 |
 +-- Main model: claude-fable-5-1[1m]
 |      |
 |      +--> Anthropic directly
 |             Existing Claude Max OAuth
 |
 +-- qwen-researcher wrapper
 |      |
 |      +--> ~/.local/bin/qwen
 |             qwen3.8-max
 |             DashScope international endpoint
 |             Existing Qwen credential mechanism
 |
 +-- kimi-coder wrapper
        |
        +--> ~/.kimi-code/bin/kimi
               CLI alias: kimi-code/k3
               API model: k3
               Kimi Code managed endpoint
               Existing Kimi OAuth
```

**[INFERENCE] Direct answers:**

- Claude can remain the normal main agent: **yes**.
- Claude can invoke the installed Qwen and Kimi workers: **supported in principle; live handoff untested**.
- ACP alone is already wired for this: **no**.
- MCP is appropriate: **yes, optional**.
- LiteLLM is needed: **no**.
- New provider credentials are needed for CLI delegation: **none expected**, apart from replacing the exposed DashScope key.

## 12. Exact Proposed Configuration

**[INFERENCE — PROPOSED, NOT CREATED]** Use `~/myDev/ai-router` for the integration code:

```text
ai-router/
  README.md
  .gitignore
  bin/
    worker.py
  profiles/
    kimi-coder.md
```

Create private runtime storage outside repositories:

```text
~/.local/state/ai-workers/
```

Add one Claude instruction entry:

```text
~/.claude/skills/delegate-workers/SKILL.md
```

No native Claude agent model override is needed. The skill would instruct Claude when to call each wrapper and how to interpret results.

**[INFERENCE — PROPOSED] Wrapper interface:**

```text
python3 /home/krakadin/myDev/ai-router/bin/worker.py qwen
python3 /home/krakadin/myDev/ai-router/bin/worker.py kimi
```

Both accept a JSON request on stdin containing:

```json
{
  "task": "Task description",
  "cwd": "/absolute/approved/project",
  "timeout_seconds": 300
}
```

The supervisor validates paths, bounds input/output, creates private task state, launches with argument arrays rather than shell interpolation, and returns:

```json
{
  "worker": "kimi",
  "requested_model": "kimi-code/k3",
  "status": "completed",
  "exit_code": 0,
  "result": "Worker answer"
}
```

A reported model should be included separately only when the worker actually exposes it.

**[INFERENCE — PROPOSED] Underlying Qwen invocation:**

```text
/home/krakadin/.local/bin/qwen
  --model qwen3.8-max
  --approval-mode plan
  --output-format json
  --max-wall-time 300s
  --max-tool-calls 40
```

Pass the task through stdin. Explicit tool exclusions would be finalized and tested against the installed registry before enabling delegation.

**[INFERENCE — PROPOSED] Initial Kimi profile:**

```markdown
---
name: kimi-coder
description: Analyze code, diagnose defects, and propose patches.
tools:
  - Read
  - Grep
  - Glob
subagents: []
---

Complete the delegated task.
Return findings, supporting file references, and any proposed patch.
Your final response must be self-contained.
```

Kimi documents explicit tool allowlists and launch-scoped `--agent-file` profiles. [Kimi agent reference](https://www.kimi.com/code/docs/en/kimi-code-cli/customization/agents)

**[INFERENCE — PROPOSED] Underlying Kimi invocation:**

```text
/home/krakadin/.kimi-code/bin/kimi
  --model kimi-code/k3
  --agent-file /home/krakadin/myDev/ai-router/profiles/kimi-coder.md
  --output-format stream-json
  -p "Read the designated private task file and complete its task."
```

The actual task-file path would be passed as an argument value. Full private context would not be placed on the process command line.

**[INFERENCE — PROPOSED] Environment and dependencies:**

- Keep normal Claude authentication and endpoint settings.
- Reuse Qwen's credential-loading mechanism after key replacement.
- Let Kimi manage its existing OAuth.
- Use a deliberate child-process environment; avoid propagating unrelated provider credentials.
- Initial implementation needs **Python's standard library only**.
- No LiteLLM, Docker, systemd service, ACP adapter, or MCP package is required.

**[INFERENCE — OPTIONAL LATER]** If named MCP tools are preferred, add a stdio server and project `.mcp.json`:

```json
{
  "mcpServers": {
    "ai-workers": {
      "type": "stdio",
      "command": "/usr/bin/python3",
      "args": [
        "/home/krakadin/myDev/ai-router/mcp_server.py"
      ]
    }
  }
}
```

That server would expose the two wrapper operations. It would require a separately reviewed implementation and dependency choice.

## 13. Implementation Steps

**[INFERENCE — PROPOSED, NOT EXECUTED]**

1. Rotate the exposed DashScope key and verify standalone Qwen with its replacement.
2. Secure the secret-bearing settings and transcript copies.
3. Decide whether the next Kimi launch may activate staged version `2.0.2`.
4. Create the supervisor, Kimi profile, private runtime directory, and ignore rules.
5. Implement deadlines, process cleanup, JSON parsing, output bounds, and explicit failure reporting.
6. Add the Claude delegation skill with Qwen research and Kimi coding/diagnosis instructions.
7. Test each worker separately with harmless tasks.
8. Test Claude invoking each wrapper and receiving its result.
9. Enable overlapping Qwen/Kimi jobs after concurrency and OAuth-refresh checks.
10. Add editing workers only after isolated-worktree and permission tests.
11. Consider MCP only if direct wrapper invocation proves awkward.

## 14. Verification/Test Plan

**[INFERENCE — PROPOSED, NOT RUN]**

| Test | Acceptance criterion |
|---|---|
| Version verification | Record the executable actually used; explicitly account for Kimi's staged update |
| Claude authentication | Claude still reports its normal subscription and Claude model |
| Qwen smoke test | Structured result from requested `qwen3.8-max` |
| Kimi smoke test | Structured result with requested `kimi-code/k3` |
| Claude handoff | Claude invokes each worker and incorporates its result |
| Permission test | Attempts to write or launch forbidden tools fail |
| Timeout test | Worker and owned child processes terminate; partial output is marked |
| Failure test | Authentication, rate-limit, and malformed-output failures are distinguishable |
| Concurrency test | Independent sessions and outputs; no worktree or credential-refresh collisions |
| Standalone regression | Normal `claude`, `qwen`, and `kimi` workflows continue working |

After implementation, harmless wrapper checks would look like:

```bash
printf '%s\n' \
  '{"task":"Reply with OK. Do not call tools.","cwd":"/home/krakadin/myDev/ai-router","timeout_seconds":60}' |
  python3 /home/krakadin/myDev/ai-router/bin/worker.py qwen
```

Repeat with `kimi`.

**[UNKNOWN / NEEDS TESTING]** These future commands will contact providers and may create normal CLI session state. They were deliberately excluded from this audit.

## 15. Rollback Plan

**[INFERENCE — PROPOSED]**

1. Remove or disable only the newly added Claude delegation skill.
2. Remove the optional `ai-workers` MCP entry if it was added.
3. Stop only worker processes started by the new supervisor.
4. Archive or remove the new integration files and private runtime state after reviewing outstanding patches.
5. Leave existing CLI installations and provider configurations intact.

**[INFERENCE]** The base design requires no global endpoint change, so rollback does not require repairing Claude's authentication route.

A rotated, exposed key must not be restored during rollback. Kimi's staged update should be treated as a separate version-management action.

## 16. Questions or Unknowns Requiring My Input

**[UNKNOWN / NEEDS TESTING] Decisions for implementation:**

1. **Qwen backend:** retain the currently configured DashScope service, or intentionally migrate to QwenCloud? Retaining DashScope is the smallest change.
2. **Worker editing:** begin with analysis/proposed patches, or allow Kimi to edit isolated worktrees? Analysis/proposed patches is the recommended first stage.
3. **Kimi update:** may the next launch activate staged **2.0.2**, or should implementation first preserve a fixed version for testing?
4. **Project scope:** should delegation be available everywhere through a user-level Claude skill, or initially in one project?

**[UNKNOWN / NEEDS TESTING] Technical unknowns:** live authentication and quota, account-specific K3 access, exact model behavior, enforced worker restrictions, concurrency, and whether the transcript exposure has remote or backup copies.
