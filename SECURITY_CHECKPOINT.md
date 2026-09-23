# Checkpoints A and B — security remediation complete; core verified

Updated: 2026-09-23. This is an implementation checkpoint record, not a report that the full worker system is complete.

## Completed

- Read `AUDIT.md` completely before taking any other action.
- Saved the supplied implementation request verbatim in `IMPLEMENTATION_SPEC.md`, preserving duplicate entries for later reconciliation and further requirements.
- Rechecked the three audited Qwen files locally. Parsed only the credential field needed for an in-memory exact-match check; emitted presence/match booleans and filesystem metadata only.
- Checked current official Alibaba Cloud documentation for the required account action.

## Verified locally before the approved permission changes

At the initial check, the credential configured at `env.DASHSCOPE_API_KEY` in `/home/krakadin/.qwen/settings.json` was stored in plaintext and appeared in both of these historical transcripts:

1. `/home/krakadin/.qwen/projects/-home-krakadin-myDev-placeorbit-starter/chats/36af78eb-9bb1-41fe-99cd-8df0545a7b24.jsonl`
2. `/home/krakadin/.qwen/projects/-home-krakadin-myDev-placeorbit-starter/chats/32b706e0-884d-4374-904b-1b95da90608d.jsonl`

All three files were ordinary files, owned by `krakadin:krakadin`, with mode `0664`. Those mode bits permitted group writes and group/other reads. Effective access also depends on ancestor permissions and ACLs; this check is not a complete ACL assessment. The home directory was `0755`; `.qwen` was `0775`. The three file modes have since been changed to `0600` with approval, as recorded below; directory modes were not changed.

Affected transcript count: **2 confirmed from the audit's known files**. This was not an exhaustive search of backups, snapshots, or other histories. No credential value, fragment, fingerprint, or credential-file contents were printed or copied into this project.

The user has now explicitly confirmed that the exposed key was reset or revoked. This is **user-confirmed provider-side remediation**, not an independent provider test. No request was made with the old credential, and it must not be restored.

## Account-action instructions provided earlier — user reports completion

1. Open the [official Singapore API Key page](https://modelstudio.console.alibabacloud.com/ap-southeast-1/model/settings/api-key) with the account that owns the existing key. Identify its existing workspace. The audited `dashscope-intl.aliyuncs.com` endpoint maps to Singapore; this does not establish the owning account. [Official region reference](https://www.alibabacloud.com/help/en/model-studio/regions).
2. Create a replacement key in that same region/workspace, retaining the permissions needed for `qwen3.8-max`, then delete the exposed key. Alternatively, the console's **Reset** operation generates a replacement and immediately invalidates the previous value. Preserve the replacement privately when displayed; do not paste it into this conversation. [Official key-management instructions](https://www.alibabacloud.com/help/en/model-studio/get-api-key).
3. Confirm that the exposed key has been invalidated. If it is shared with another application, that application will also need its replacement; no automatic account or application changes will be made here.

Only the API-key management procedure above is relevant from the linked documentation. Do not follow examples that print credentials, place them in shell command history, or append them to shell startup files.

## Approved local permissions remediation — applied and verified

The user explicitly approved replacing Qwen's existing credential field and restricting the three affected files. Qwen retains credential ownership and its supported settings mechanism. The user entered the replacement through hidden terminal input; ai-router does not own provider credentials.

Applied exactly this approved permission scope, after checking resolved paths, ownership, regular-file type, and absence of hard links:

| File | Previous mode | Verified new mode | Reason |
| --- | --- | --- | --- |
| `/home/krakadin/.qwen/settings.json` | `0664` | `0600` | Contains the active provider credential |
| First transcript listed above | `0664` | `0600` | Contains the exposed historical credential |
| Second transcript listed above | `0664` | `0600` | Contains the exposed historical credential |

No recursive chmod, directory changes, ownership changes, transcript deletion, or historical content rewrite occurred. Open file descriptors were used for the permission changes; resulting modes and unchanged UID/GID were checked. File contents were not read or rewritten during chmod.

Transcript cleanup options after rotation, requiring a separate decision: retain both in place with owner-only permissions; redact only the revoked value; or delete the two files. Retention with restricted permissions is the least disruptive initial choice. Deletion or overwrite cannot guarantee eradication from SSDs, snapshots, backups, or copied histories. Revocation is the primary security control.

## Verification required before closing Checkpoint A

- User confirmation of provider-side invalidation: **received**. Do not test the old key.
- Replacement configured through Qwen's own existing mechanism, without copying it into ai-router.
- Approved permission changes: **applied and verified**, with previous/new modes above.
- One minimal harmless Qwen request through `/home/krakadin/.local/bin/qwen`: **PASS**. Version `0.24.4`, requested/reported model `qwen3.8-max`, JSON output, `--approval-mode plan`, `--max-tool-calls 0`, `--max-wall-time 300s`; exit `0`, expected `QWEN_WORKER_OK`, duration `18,162 ms`. This verifies one response for the tested key/endpoint/model; ongoing quota/entitlement remains unknown.
- In-memory check after live use: active key absent from the six then-existing project files and all 44 Qwen chat transcripts. No credential value was emitted. The two known old transcripts remain by user instruction and still contain the revoked credential. Backups and snapshots were not searched.

## Files changed and tests

- Created `IMPLEMENTATION_SPEC.md`; verified its saved request body equals the supplied message exactly.
- Created this checkpoint record.
- Updated this checkpoint record as remediation progressed.
- Created `tools/update_qwen_credential.py`, a one-time user-operated administrative helper. This is separate from the future worker supervisor; it is never a worker credential store or provider client.
- Created `tests/test_qwen_credential_update.py`: **21 offline tests pass** with synthetic keys and temporary fixtures only. Tests cover exact field/byte preservation, endpoint/key-class pairing, permission checks, symlink/hard-link rejection, malformed/duplicate JSON, exposed-key reuse rejection, hidden-input failure, declined confirmation, atomic-write failure cleanup, and credential-free messages.
- Created `ai_router/` core and `workers/base.py`, plus offline fake-worker tests. The full command `/usr/bin/python3 -B -m unittest discover -s tests -v` passes **38 tests** in 14.4 seconds. It covers request/path validation, symlink escape, child environment filtering, redaction, database safety, state transitions, process group timeout/cancellation including a child process, output bounds, errors, and fake provider modes. Tests make no provider calls.
- Outside the project: only the permissions of the three listed Qwen files changed, `0664` to `0600`; Qwen settings were then updated with the replacement key and two active-model endpoint fields. Transcript contents and ownership were retained.
- No Kimi CLI was launched, so its staged update was not activated. Claude configuration was not changed.
- Git was initialized locally on branch `master`; no remote exists. Checkpoint A/B files are being reviewed for the first local commit. No package installation or runtime-state directory was created by the supervisor tests.
- Tests/checks: exact-match scans, mode/ownership checks, spec preservation, one live Qwen test, and offline helper tests. The active key was absent from all scanned ai-router files afterward.

## Checkpoint C paused pending Qwen usage-scope clarification

**Checkpoints A and B are complete.** Checkpoint C is paused before any further Qwen calls because the Token Plan terms may not allow the proposed noninteractive worker invocation. No Kimi live test or Claude delegation has occurred.

Checkpoint B covers the supervisor protocol, SQLite job/event state, request/path validation, adapter contract, child environment filtering, process-group timeout/cancellation, bounded output, redaction, and offline fake-worker tests. Further supplied requirements will be reconciled with the preserved baseline before dependent implementation.

## Additional master prompt reconciled

Read `Claude_Qwen_Kimi_Worker_System_Master_Prompt.md` completely after the user added it. Its sections 1–27 match the corresponding original request after normalizing bullet markers and blank lines. The file currently ends at a bare `# 28` heading, with no section 28 body or subsequent sections.

`IMPLEMENTATION_SPEC.md` retains the full supplied baseline through section 615, including the original duplicate entries. Neither source was rewritten or shortened. Adding the file did not withdraw the remaining requirements or itself confirm credential rotation; the later explicit user reply provided that confirmation. Further additions can be compared against both preserved sources.

## Endpoint information supplied during remediation

The user supplied two possible routes:

- Token Plan OpenAI-compatible base URL: `https://token-plan.maas.qwencloudapi.com/compatible-mode/v1`.
- Singapore workspace API host: `ws-6ngvqa7xxgx1usvk.ap-southeast-1.maas.aliyuncs.com`. Its OpenAI-compatible base URL is the HTTPS host followed by `/compatible-mode/v1`.

**Verified from official documentation:** Token Plan dedicated keys and regular API keys use different endpoints and must not be mixed. See [QwenCloud Token Plan setup](https://docs.qwencloud.com/token-plan/team/token-plan-team-quickstart). The supplied workspace hostname follows Alibaba's documented Singapore workspace endpoint format; see [Alibaba Model Studio API examples](https://help.aliyun.com/en/model-studio/what-is-model-studio).

**Verified live:** the replacement credential returned `QWEN_WORKER_OK` through the configured Token Plan endpoint and Qwen CLI. The supplied workspace endpoint was not contacted. The subscription edition associated with the Token Plan credential has not been independently established.

The helper recognizes the documented key class locally without printing any characters from the key. This establishes only a candidate route, not credential validity, workspace ownership, subscription edition, quota, or model entitlement. Unknown key classes fail without writing. No automatic fallback or probing of multiple endpoints is used.

The user-approved change originally covered only the credential and file permissions. In `--configure-endpoint` mode, the helper displayed the exact proposed endpoint and required the user to type `UPDATE` in their own terminal before applying these three settings changes together:

1. `env.DASHSCOPE_API_KEY`: replacement entered through hidden input.
2. `model.baseUrl`: matching supplied OpenAI-compatible endpoint.
3. `modelProviders.openai` entry whose `id` is `qwen3.8-max`: the same `baseUrl`. This is index 6 in the inspected current configuration; the helper locates it by model ID and requires a unique match.

All other configuration bytes are preserved, including model selection, authentication type, other model definitions, and provider metadata. `DASHSCOPE_API_KEY` remains the existing local credential-field name; the name alone does not establish which backend is in use. Claude and Kimi configuration are never opened or modified by the helper.

If the replacement is a Token Plan key, the other 13 saved models keep their existing pay-as-you-go endpoints; that key will not work with those endpoints. The helper explicitly reports this before confirmation. Migrating other saved models is outside the approved change and requires separate review of model availability. Provider identity in future ai-router status must follow the actual endpoint, not the retained historical display label.

**Usage-scope checkpoint:** current official QwenCloud terms say Personal Edition is limited to interactive use in programming/agent tools and prohibits automation scripts, custom application backends, and non-interactive batch calls; the Team Edition FAQ also prohibits automated scripts and application backends. See [Personal Edition terms](https://docs.qwencloud.com/token-plan/personal/token-plan-personal-overview) and [Team Edition FAQ](https://docs.qwencloud.com/token-plan/team/token-plan-team-faq). The proposed Claude-to-`ai-worker`-to-noninteractive-Qwen-CLI call path may be outside these terms. No further Qwen worker call will be made until this use is confirmed as allowed or a suitable pay-as-you-go credential is configured. No subscription changes or account scraping are authorized.

## Private replacement entry — completed

The user ran this directly in their own terminal:

```bash
/usr/bin/python3 -B /home/krakadin/myDev/ai-router/tools/update_qwen_credential.py --configure-endpoint
```

Enter the replacement twice at the hidden prompts. Verify the displayed key class and endpoint match what your account issued, then type `UPDATE` to approve the displayed three-field change. Any other answer leaves settings unchanged. Do not supply the key as an argument, environment override, pipe, chat message, or screenshot.

The helper refuses non-terminal input or any failure to disable input echo. Its only credential-bearing temporary file is an unpredictable `0600` atomic replacement beside Qwen's existing settings, removed on normal completion or handled failure. It created no backup or persistent credential copy in this project and did not itself call Qwen or a provider.
