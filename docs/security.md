# Security model

## Credential boundaries

- Claude owns Claude Max OAuth; ai-router does not read `~/.claude/.credentials.json` and does not change Claude routing.
- Kimi owns Kimi Code OAuth under its own configuration. ai-router does not copy or parse OAuth credentials.
- Kimi subprocesses receive only an explicit environment built by the adapter. Anthropic, Qwen, and generic provider credential variables are not inherited. `HOME` is retained so Kimi can read its own configuration.
- ai-router has no provider credentials, credential form, vault, telemetry, or direct provider HTTP client.
- Qwen is not currently enabled. The Alibaba Token Plan usage restrictions are documented in `SECURITY_CHECKPOINT.md`.

## Read-only behavior and limits

The Kimi profile allowlists the installed CLI's exact read tools: `Read`, `Grep`, and `Glob`; `subagents: []` disables nested agents. There is no shell tool in the profile. The parent instruction treats repository content as untrusted and forbids credential discovery, writes, installation, commits, pushes, deployment, and spawning other AI tools.

These are application-level tool restrictions and prompt policy, not a kernel sandbox. The subprocess runs as the same Unix user, and Kimi CLI can access the user's Kimi-owned configuration because it needs its own authentication. Do not delegate a repository or context that must not be sent to Kimi. Provider-side retention is controlled by Kimi's service/account terms, not ai-router.

The requested working directory is resolved and must be beneath `/home/krakadin/myDev`. This prevents simple traversal and symlink escapes for `cwd`; it does not prevent all symlinked file access within a repository. The profile does not expose shell/network tools, but same-user OS isolation is not claimed.

## Local data and redaction

Runtime root and subdirectories are owner-only (0700); SQLite and task files are owner-only (0600). Result, diagnostic, and event text is redacted before persistence. Raw provider output is not stored. Redaction covers common bearer/API-key/token/cookie/private-key forms as defense in depth; it cannot make arbitrary secret-bearing data safe. Do not intentionally put secrets in a task.

Job summaries, normalized results, and errors may contain proprietary source context. They are stored locally under `~/.local/state/ai-workers` with a 30-day cleanup policy planned but not yet implemented. Kimi separately retains native session history; ai-router does not delete it.

## Threat model and limitations

The design helps reduce accidental credential leakage, cross-provider credential inheritance, command injection, path traversal, malformed/oversized output, runaway processes, and worker write actions through the configured tool allowlist. It does not defend against malicious code already running as the same Unix user, root/kernel compromise, a malicious CLI binary/provider, every same-user filesystem attack, or provider-side data retention.

Claude's own session history may contain the tool invocation and returned worker result as part of normal Claude Code behavior. Do not use delegation for confidential material unless sending it to Kimi is acceptable.
