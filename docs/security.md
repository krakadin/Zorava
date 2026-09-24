# Security model

## Credential boundaries

- Claude owns Claude Max OAuth; ai-router does not read `~/.claude/.credentials.json` and does not change Claude routing.
- Kimi owns Kimi Code OAuth under its own configuration. ai-router does not copy or parse OAuth credentials.
- Qwen owns its Token Plan key in its existing Qwen Code settings. ai-router parses only safe model/endpoint/credential-presence metadata, does not copy the key, and does not pass `DASHSCOPE_API_KEY` to the child environment.
- Worker subprocesses receive only an explicit environment built by their adapters. Anthropic, Qwen, Kimi, and generic provider credential variables are not inherited across provider boundaries. `HOME` is retained so each CLI can read its own configuration.
- ai-router has no provider credentials, credential form, vault, telemetry, or direct provider HTTP client.
- Qwen calls are synchronous, bounded, and initiated only for a user-directed task in an active Claude session. There is no scheduled, unattended, bulk, daemon, or standalone service path. The interpretation of Alibaba's usage scope and its uncertainty are documented in `SECURITY_CHECKPOINT.md`.

## Read-only behavior and limits

The Kimi profile allowlists the installed CLI's exact read tools: `Read`, `Grep`, and `Glob`; `subagents: []` disables nested agents. There is no shell tool in the profile. Qwen runs in its installed CLI's `plan` approval mode, with a core allowlist of `read_file`, `list_directory`, `glob`, and `grep_search`, explicit exclusions for mutation/shell/agent/network and related tools, bounded tool calls/time, and chat recording disabled. Both worker instructions treat repository content as untrusted and forbid credential discovery, writes, installation, commits, pushes, deployment, and recursive AI workers.

These are application-level tool restrictions and prompt policy, not a kernel read sandbox. The subprocess runs as the same Unix user, and provider CLIs can access their own user-owned configuration because they need their authentication. Do not delegate a repository or context that must not be sent to the selected external provider. Provider-side retention is controlled by the selected provider's service/account terms, not ai-router.

## Opt-in isolated-edit mode

The explicit `isolated-edit` mode is only available for Kimi. The source checkout must be clean. Kimi works in a detached per-job Git worktree under the private runtime directory; it cannot modify the primary checkout through the configured tools. The Kimi profile has no shell, built-in filesystem tools, or subagents. It receives only a local MCP broker whose read/write operations validate worktree-relative paths, deny symlinks, and exclude credential-like/control paths. Task text is fetched through that broker, not command-line arguments.

The worker process is additionally confined with Linux Landlock. On this machine the kernel reports Landlock ABI 8, and automated probes confirmed writes inside an allowed root succeed while writes outside it and through an escaping symlink fail. Edit mode fails closed if Landlock cannot be activated. Writes are permitted inside the job runtime directory and Kimi's own credential directory so the CLI can maintain its session and refresh its own OAuth; the model-facing MCP broker does not expose that credential directory. The worker can still read arbitrary paths at the OS level, because read access is not restricted by this Landlock policy. This is not a complete filesystem sandbox.

The Git worktree is not treated as security isolation. It prevents accidental changes to the primary checkout; Landlock write confinement and the MCP file broker provide the additional controls. No tests, shell commands, package installs, commits, pushes, merges, or deployment are offered in edit mode. Kimi returns a bounded sanitized diff and Claude reviews it; no patch is automatically applied. The per-job worktree and Kimi session history persist until the operator explicitly reviews and runs `ai-worker discard JOB_UUID --confirm`.

The requested working directory is resolved and must be beneath `/home/krakadin/myDev`. This prevents simple traversal and symlink escapes for `cwd`; it does not prevent all symlinked file access within a repository. The profile does not expose shell/network tools, but same-user OS isolation is not claimed.

## Local data and redaction

Runtime root and subdirectories are owner-only (0700); SQLite and task files are owner-only (0600). Result, diagnostic, and event text is redacted before persistence. Raw provider output is not stored. Redaction covers common bearer/API-key/token/cookie/private-key forms as defense in depth; it cannot make arbitrary secret-bearing data safe. Do not intentionally put secrets in a task.

Job summaries, normalized results, and errors may contain proprietary source context. They are stored locally under `~/.local/state/ai-workers` with a 30-day cleanup policy planned but not yet implemented. Kimi separately retains native session history; ai-router does not delete it.

## Threat model and limitations

The design helps reduce accidental credential leakage, cross-provider credential inheritance, command injection, path traversal, malformed/oversized output, runaway processes, and worker write actions through the configured tool allowlist. It does not defend against malicious code already running as the same Unix user, root/kernel compromise, a malicious CLI binary/provider, every same-user filesystem attack, or provider-side data retention.

Claude's own session history may contain the tool invocation and returned worker result as part of normal Claude Code behavior. Do not use delegation for confidential material unless sending it to the selected worker's provider is acceptable.
