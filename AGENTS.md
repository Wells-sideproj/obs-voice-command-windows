# Repository Agent Policy

## Canonical execution sources

Agents working in this repository must read these files before claiming or implementing a Windows 11 ticket:

1. `docs/plans/2026-08-17-windows-11-port-plan.md`
2. `docs/plans/2026-08-17-windows-11-ticket-manifest.yml`
3. `WORKFLOW.md`
4. `.codex/config.toml` and the selected file under `.codex/agents/`

The ticket manifest is authoritative for dependencies, model profiles, retry state, and arbitration state. `WORKFLOW.md` is authoritative for CI, auto-merge, Windows 11 integration, and repair behavior.

## Repository topology and empty-origin gate

- The canonical Windows repository is `https://github.com/Wells-sideproj/obs-voice-command-windows.git`; local `origin` must point there after the transfer. Local `upstream` is the read-only source `https://github.com/htlin222/obs-voice-command.git`.
- The Windows repository was seeded exactly once at `de6c588f981596ed13bc9cd0254ad4989a2686b3`, remains public, and is now owned by the GitHub Free Organization `Wells-sideproj`; its external predecessor gate, `empty_origin_baseline_seed`, is `policy_verified`.
- The seed requires explicit human approval plus existing non-interactive GitHub write and administration authority. Public read access, Codex authentication, or repository ownership inferred from its name is not permission evidence.
- The only permitted direct-push exception is one creation of the absent `refs/heads/develop` at exactly `de6c588f981596ed13bc9cd0254ad4989a2686b3`, using the manifest's full-OID refspec after every precondition passes. It may not include any current working-tree file, tag, other ref, force option, or second push.
- After the seed, the sole remote ref and exact SHA, `develop` default branch, merge settings, and active zero-bypass `develop` rule were read back successfully before any W11 branch was pushed.
- If the seed or policy setup becomes incomplete, freeze remote writes and keep W11-001 and its descendants `Blocked`. Never automatically force-rewrite, delete a ref/repository, recreate the repository, or improvise another initialization method.

## Agent roles

- All implementation work uses the manifest's `implementer` profile by default.
- The `arbitrator` profile analyzes architecture conflicts and root causes. It does not silently take over implementation.
- Only the orchestrator may dispatch tickets, update cross-ticket dependencies, or request arbitration.
- A worker may only claim a ticket whose status is `Ready` and whose dependencies are `Done`.
- A ticket becomes `Done` only when the evidence required by its manifest `completion_profile` is present for the exact merged commit. Layer B is not a universal completion gate.
- Use one ticket, worktree, branch, and pull request per unit of work.

## Executable model routing

Model routing is enforced by project-scoped Codex custom agents or exact spawn-time model overrides, not by display labels or by the model selected for the parent task.

- Before the first dispatch in a task, run `powershell -NoProfile -ExecutionPolicy Bypass -File tools/validate_model_routing.ps1`. A failure blocks dispatch.
- Resolve a ticket's worker profile from `ticket.execution.worker_profile` when present, otherwise from `execution_defaults.worker_profile`.
- Resolve arbitration from `ticket.execution.arbitration_profile` when present, otherwise from `execution_defaults.arbitration_profile`.
- For implementation or repair, the controller must dispatch the `windows_worker` route, pinned in `.codex/agents/windows-worker.toml` to `gpt-5.6-luna` with `model_reasoning_effort = "max"`.
- For planning arbitration or third-failure root-cause analysis, the controller must dispatch `windows_arbitrator`, pinned in `.codex/agents/windows-arbitrator.toml` to `gpt-6-astra` with `model_reasoning_effort = "medium"`, and wait for its result.
- When the client supports named custom agents, dispatch the named agent. When the spawn interface exposes only model and effort overrides, pass the route's exact `model` and `model_reasoning_effort` explicitly and include the matching custom-agent instructions in the child prompt. Never omit either override.
- Never use a generic built-in worker, the parent agent, or another available model as a silent fallback for either route.
- The controller may perform deterministic scheduling and state bookkeeping. It must not implement a ticket itself or substitute its own judgment for required arbitration.
- After spawning, verify either the named custom-agent identity or the spawn call's explicit model and effort metadata. If the custom agent cannot be loaded, the configured model is unavailable, or the route metadata cannot be verified, fail closed: do not consume the code retry budget, set routing state to `blocked`, keep the ticket `Blocked`, and request human action.

The parent task may be set to Luna without weakening arbitration. The route's custom-agent settings or explicit spawn overrides are authoritative and must override the parent for the spawned session.

## Pull requests and merge authority

- A worker must never merge its own pull request, use an admin bypass, disable a check, or weaken a test to obtain a pass.
- Local tests are evidence, not merge authority.
- With the active `develop` merge queue, only the orchestrator may enroll a green, open pull request by schema-checked `enqueuePullRequest` with `expectedHeadOid` equal to the exact latest PR head SHA. GitHub must create the protected `merge_group` and execute the squash only after `required / gate` and every branch rule succeed on that same head. A worker never enqueues, merges, or bypasses protection.
- The pre-queue `enablePullRequestAutoMerge_SQUASH` contract is historical, superseded after `w11_009_merge_queue_activation`, and valid only before queue activation when no queue capability exists. It is not a current completion requirement; its attempt records remain audit history.
- Any head change invalidates prior queue or enrollment evidence. Revalidate the new exact head and its protected checks before enrollment, and do not push further commits after enrollment.
- The stable branch-protection check is `required / gate`. It must aggregate every Layer A required job and fail if any required job fails or is cancelled.
- `continue-on-error` is forbidden for required checks.
- A pull request with a failing, missing, stale, or cancelled required check remains in `PR` or returns to `Doing`.

## Two-layer Windows verification

### Layer A: ordinary Windows CI

- Runs before merge on GitHub-hosted Windows.
- Covers locked dependency installation, build/import smoke, unit tests, adapter tests, hardware-free component tests, and CLI tests.
- Must not require a microphone, ASR model download, a real OBS process, or a persistent desktop session.
- Layer A is part of `required / gate` and therefore blocks protected merge-queue execution (and the superseded pre-queue auto-merge fallback).

### Layer B: real Windows 11 integration

- Runs after a commit reaches protected `develop` on a dedicated self-hosted Windows 11 runner.
- Runs only from trusted `push` to `develop` or an explicitly authorized `workflow_dispatch`; never execute untrusted pull-request code on this runner.
- The runner must have an interactive logged-in desktop, a dedicated OBS test profile/scene collection, and exclusive access to port 4455.
- The harness starts test OBS, waits for `127.0.0.1:4455`, exercises the production command-to-transform path, reads the OBS transform, verifies zoom-in and restore, and always restores the baseline transform during cleanup.
- W11-010 implements and hardware-free-tests the Layer B workflow and harness without activating real OBS on every `develop` push. W11-011 activates and executes the trusted Layer B certification path for the existing single-monitor DPI/resolution. W11-013 owns deferred multi-monitor, mixed-DPI, negative-coordinate, and cross-monitor freeze real-machine certification; existing code and mock regression remain intact.
- 依2026-10-15使用者批准，W11-012當期單螢幕 release 依 manifest 的 mandatory set 完成，不等待 W11-013 的延期實機證據；影響當期功能、安全、cleanup 或共用 regression 的缺陷仍阻擋 release。未測配置不得宣稱 certified。
- Layer B is post-merge by design. It does not gate W11-001 through W11-010; it gates only tickets whose `completion_profile` requires Layer B, plus release readiness.

## Bootstrap and unattended execution

- W11-001 owns the bootstrap form of `.github/workflows/ci.yml` and must emit the final stable Layer A job names, including `required / gate`. Its Windows bootstrap jobs cover only the dependency-lazy probe, routing validator, manifest, and planning artifacts; they do not install the still-Quartz-dependent project or start OBS.
- W11-002 removes the bootstrap exemption, adds platform markers and the lock, and activates full cross-platform Layer A. W11-005 hardens that already-active gate. W11-003 and W11-004 therefore depend on W11-005.
- Until W11-010 is merged, a distinct trusted `push` run of `ci.yml` on the exact merged `develop` SHA supplies temporary post-merge Layer A evidence. A pull-request run, stale SHA, skipped/neutral result, or matching check name without the trusted GitHub Actions source is insufficient.
- In unattended mode, agents must never launch a browser or device-auth flow. They may use only an already authenticated CLI session, existing token, or existing app credential.
- Missing interactive authentication, repository administration permission, shared-runtime approval, or real-hardware availability blocks only the owning ticket and its transitive descendants. It does not consume the code retry budget, and the orchestrator continues every independent Ready ticket whose own gates are satisfied.
- Unattended execution never authorizes a bypass. Apart from the separately ratified and one-time `empty_origin_baseline_seed`, there is no direct push to `develop`, REST/manual/admin merge, disabled check, weakened rule, or secret copied into repository files, logs, artifacts, or prompts.
- W11-001 does not require OBS to be running. `MISSING_OBS` or `UNREACHABLE_ENDPOINT` is acceptable sanitized preflight evidence. Live API, transform, microphone, and existing-configuration single-monitor evidence belongs to W11-011; deferred multi-monitor/mixed-DPI/negative-coordinate/cross-monitor evidence belongs to W11-013.

## Retry and arbitration policy

Retry accounting is per originating ticket and repair lineage. A full pass on the latest target commit resets consecutive failures. Runner outages or other confirmed infrastructure failures use the infrastructure retry allowance and do not consume the code retry budget.

1. First code-related CI failure: create or update the Repair Ticket and dispatch the implementer.
2. Second code-related CI failure: dispatch the implementer for one more repair cycle with both failure records attached.
3. Third code-related CI failure: stop worker dispatch, set the ticket to `Blocked`, set arbitration to `requested`, and ask the high-tier arbitrator for root-cause analysis. After the decision is recorded, the implementer may apply the directed repair.
4. Fourth code-related CI failure: stop all automatic repair, keep the ticket `Blocked`, set retry state to `needs_human`, apply the `needs-human` label, and request human direction.

The manifest's `escalation_threshold` still governs non-CI uncertainty such as architecture boundary changes, dependency DAG changes, permissions, or conflicting acceptance criteria. The four-stage CI retry budget overrides that threshold for ordinary failing checks.

Every escalation packet must contain the ticket ID, failing layer/check, commit and run URL, exact failing command, relevant log excerpt, attempted repairs, current diff summary, and one concrete decision question.

## Safety and cleanup

- Do not install or change shared Python, PATH, package-manager settings, OBS profiles, or GitHub repository settings without the approval gate recorded in the ticket.
- Keep dependencies project-local and locked.
- Never expose OBS WebSocket passwords or other secrets in logs, artifacts, issue bodies, or model prompts.
- Layer B must serialize access to the self-hosted OBS runner and execute cleanup under an always-run/finally path.
- A failed integration run must leave OBS source transforms at the captured baseline or report cleanup failure as a separate blocking failure.
