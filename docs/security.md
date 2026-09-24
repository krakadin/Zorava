# Security model

## Credential boundaries

- Claude owns Claude Max OAuth; ai-router does not read `~/.claude/.credentials.json` and does not change Claude routing.
- Kimi owns Kimi Code OAuth under its own configuration. ai-router does not copy or parse OAuth credentials.
- Qwen owns its Token Plan key in its existing Qwen Code settings. ai-router parses only safe model/endpoint/credential-presence metadata, does not copy the key, and does not pass `DASHSCOPE_API_KEY` to the child environment.
- Worker subprocesses receive only an explicit environment built by their adapters. Anthropic, Qwen, Kimi, and generic provider credential variables are not inherited across provider boundaries. `HOME` is retained so each CLI can read its own configuration.
- ai-router has no provider credentials, credential form, vault, or telemetry. Its only outbound HTTP client is the dashboard's read-only CLI version check described below; it cannot reach a provider inference endpoint.
- Qwen calls are synchronous, bounded, and initiated only for a user-directed task in an active Claude session. There is no scheduled, unattended, bulk, daemon, or standalone service path. The interpretation of Alibaba's usage scope and its uncertainty are documented in `SECURITY_CHECKPOINT.md`.

## Explicit read-only behavior and limits

The Kimi profile allowlists the installed CLI's exact read tools: `Read`, `Grep`, and `Glob`; `subagents: []` disables nested agents. There is no shell tool in the profile. Qwen runs in its installed CLI's `plan` approval mode, with a core allowlist of `read_file`, `list_directory`, `glob`, and `grep_search`, explicit exclusions for mutation/shell/agent/network and related tools, bounded tool calls/time, and chat recording disabled. Both read-only profiles treat repository content as untrusted and forbid credential discovery, writes, installation, commits, pushes, deployment, and recursive AI workers.

These are application-level tool restrictions and prompt policy, not a kernel read sandbox. The subprocess runs as the same Unix user, and provider CLIs can access their own user-owned configuration because they need their authentication. Do not delegate a repository or context that must not be sent to the selected external provider. Provider-side retention is controlled by the selected provider's service/account terms, not ai-router.

## Default isolated coding mode

The default `isolated-edit` mode is available for both Qwen and Kimi. The source checkout must be clean. Each coder works in a detached per-job Git worktree under the private runtime directory; it cannot modify the primary checkout through the configured tools. Both coding profiles expose only the scoped aiworker file tools; shell, native filesystem tools, and nested agents are disabled. It receives only a local MCP broker whose read/write operations validate worktree-relative paths, deny symlinks, and exclude credential-like/control paths. Task text is fetched through that broker, not command-line arguments.

The worker process is additionally confined with Linux Landlock. On this machine the kernel reports Landlock ABI 8, and automated probes confirmed writes inside an allowed root succeed while writes outside it and through an escaping symlink fail. Edit mode fails closed if Landlock cannot be activated. Writes are permitted inside the job runtime directory. Kimi additionally receives write access to its own credential directory so it can refresh its own OAuth; the model-facing MCP broker does not expose that credential directory. The worker can still read arbitrary paths at the OS level, because read access is not restricted by this Landlock policy. This is not a complete filesystem sandbox.

The Git worktree is not treated as security isolation. It prevents accidental changes to the primary checkout; Landlock write confinement and the MCP file broker provide the additional controls. No tests, shell commands, package installs, commits, pushes, merges, or deployment are offered in edit mode. Each coder returns a bounded sanitized diff and Claude reviews it; no patch is automatically applied. The per-job worktree and worker runtime data persist until the operator explicitly reviews and runs `ai-worker discard JOB_UUID --confirm`.

The requested working directory is resolved and must be beneath `/home/krakadin/myDev`. This prevents simple traversal and symlink escapes for `cwd`; it does not prevent all symlinked file access within a repository. The profile does not expose shell/network tools, but same-user OS isolation is not claimed.

## Local data and redaction

Runtime root and subdirectories are owner-only (0700); SQLite and task files are owner-only (0600). Result, diagnostic, and event text is redacted before persistence. Raw provider output is not stored. Redaction covers common bearer/API-key/token/cookie/private-key forms as defense in depth; it cannot make arbitrary secret-bearing data safe. Do not intentionally put secrets in a task.

Job summaries, normalized results, and errors may contain proprietary source context. They are stored locally under `~/.local/state/ai-workers`. A 30-day cleanup is implemented: `ai-worker cleanup --dry-run` previews, and `ai-worker cleanup --confirm` purges eligible terminal jobs/events. Active jobs and per-job directories containing retained isolated-edit worktrees/runtime data are protected. Cleanup does not delete provider-owned histories. Deletion is not guaranteed secure erasure, particularly on SSDs or snapshot-backed filesystems. Kimi separately retains native session history; ai-router does not delete it.

The optional operations dashboard binds only to `127.0.0.1`, validates Host/Origin/CSRF for state-changing requests, emits restrictive browser security headers, and has no arbitrary task or shell endpoint. Provider smoke-test buttons make small live calls; refreshes do not call providers. Dashboard tests are tracked as normal jobs and are canceled on graceful dashboard shutdown; ordinary CLI-launched jobs do not depend on the dashboard.

Provider cards also show the installed CLI version and cached update metadata. `GET /api/v1/updates`, `GET /api/v1/providers`, and every other GET are cache-only and make no outbound request; polling and page refreshes therefore never reach the network. Only the CSRF/Origin-protected `POST /api/v1/updates/check` triggers a check, and only for `qwen`/`kimi`. A check is a single bounded read-only HTTPS GET of published version metadata from two fixed official sources: `https://registry.npmjs.org/@qwen-code/qwen-code/latest` (JSON `version`) for Qwen and `https://code.kimi.com/kimi-code/latest` (a plain `x.y.z` line) for Kimi. The URL always comes from that pinned table, so no user-supplied or repository-supplied URL can be requested; the scheme/host allowlist is re-verified, environment proxies are disabled, redirects are refused rather than followed, no `Authorization`, cookie, or provider credential is sent, the timeout is 6 seconds, and the response is capped at 8192 bytes. Only stable `x.y.z` versions are accepted; prerelease, malformed, or oversized metadata becomes `unknown` with a fixed error code. At most one background check per worker runs at a time and extra requests are reported as deferred. Nothing in this path installs, upgrades, patches, or restarts a CLI; ai-router only reports whether an upgrade is pending.

## Threat model and limitations

The design helps reduce accidental credential leakage, cross-provider credential inheritance, command injection, path traversal, malformed/oversized output, runaway processes, and worker write actions through the configured tool allowlist. It does not defend against malicious code already running as the same Unix user, root/kernel compromise, a malicious CLI binary/provider, every same-user filesystem attack, or provider-side data retention.

Claude's own session history may contain the tool invocation and returned worker result as part of normal Claude Code behavior. Do not use delegation for confidential material unless sending it to the selected worker's provider is acceptable.
