# Windows 11 Delivery Workflow

This document defines the delivery gates for the Windows 11 port. It is a workflow specification; the GitHub Actions files are bootstrapped by W11-001, fully activated by W11-002, hardened by W11-005, administered by W11-009, automated post-merge by W11-010, and activated for real OBS certification by W11-011.

## 0. Runtime model router

The executable router combines the Codex project-agent configuration under `.codex/` with explicit model overrides on every spawn. The parent task is a coordination controller; implementation and arbitration always run in routed child agents.

```mermaid
flowchart TD
    Controller["Parent coordination controller"] --> Preflight["Validate manifest and .codex routing"]
    Preflight -->|PASS + Ready ticket| Worker["windows_worker: gpt-5.6-luna / max"]
    Worker -->|success| CI["PR and required checks"]
    Worker -->|escalation or third failure| Arb["windows_arbitrator: gpt-6-astra / medium"]
    Arb --> Decision["Structured arbitration decision"]
    Decision -->|targeted repair| Worker
    Decision -->|needs human| Stop["Blocked + human action"]
    Preflight -->|route unavailable| Stop
```

Routing rules:

- Static preflight: `powershell -NoProfile -ExecutionPolicy Bypass -File tools/validate_model_routing.ps1` must pass before dispatch.
- The controller resolves global defaults first, then applies a ticket's `execution` overrides.
- `windows_worker` is the only default implementation/repair route.
- `windows_arbitrator` is the only planning/arbitration/root-cause route.
- Custom agent files pin both the actual model ID and `model_reasoning_effort`; the parent task's selected model is not inherited for these two routes.
- A client with named custom-agent support dispatches `windows_worker` or `windows_arbitrator`. A spawn interface with only model controls must pass the exact route model and effort explicitly and include the corresponding agent instructions in its prompt.
- Routing is fail-closed. Missing agents, unavailable models, malformed configuration, omitted overrides, or unverifiable spawn metadata block the ticket and do not count as a code failure.
- A CLI-based runner may additionally append `-CheckCli`; this checks authentication, the multi-agent flag, and the model catalog. Desktop execution still requires evidence of either the named custom agent or the exact explicit model and effort overrides.

## 1. Empty-origin initialization gate

The Windows delivery repository is `origin=https://github.com/Wells-sideproj/obs-voice-command-windows.git`; the original project is retained as `upstream=https://github.com/htlin222/obs-voice-command.git`. The approved empty-origin seed has created the sole baseline ref `refs/heads/develop` at `de6c588f981596ed13bc9cd0254ad4989a2686b3`, and `develop` is the default branch. The repository is public and owned by the GitHub Free Organization `Wells-sideproj`.

`empty_origin_baseline_seed` was the external non-code predecessor of W11-001 and is now `policy_verified`:

1. For a fresh or repaired repository, keep W11-001 `Blocked` until explicit seed-exception approval and authenticated non-interactive GitHub write/admin authority exist. Set `GIT_TERMINAL_PROMPT=0` for every unattended Git command; never launch browser/device auth or expose credentials.
2. Before any future initialization mutation, re-verify that upstream `develop` is exactly `de6c588f981596ed13bc9cd0254ad4989a2686b3`, the local object is that commit, and the expected remote state matches the manifest. Any drift or unexpected ref returns to arbitration.
3. The only permitted direct-push exception is exactly:

   ```text
   git push --porcelain origin de6c588f981596ed13bc9cd0254ad4989a2686b3:refs/heads/develop
   ```

   `--force`, `--mirror`, `--all`, tags, a local branch as source, any other ref/SHA, and every second direct push are forbidden. The full-OID source guarantees that current uncommitted planning/probe files are excluded.
4. Verify origin now has exactly `refs/heads/develop` at the pinned SHA; set/confirm `develop` as default; enable squash merge and repository auto-merge, disable merge commits/rebase merges, and enable head-branch deletion.
5. Before any W11 branch is pushed, install and read back an active ruleset targeting exactly `refs/heads/develop`: empty bypass list, pull requests required, zero mandatory reviews, linear history, deletion blocked, and non-fast-forward updates blocked.
6. The initial rule cannot require `required / gate` before that check context exists. W11-001 introduces the check through its PR; after the authentic GitHub Actions context appears, add it to the active rule with strict updates and verify provider identity before the pre-queue exact-head squash auto-merge fallback. That legacy enrollment is superseded once the merge queue is active.

Gate states are `seed_not_written`, `baseline_seeded_policy_incomplete`, `policy_verified`, and `unexpected_remote_state_needs_human`. Failure after a correct seed freezes the public upstream baseline and all further pushes until policy is repaired. Unexpected refs or SHA never trigger automatic force-rewrite, deletion, or repository recreation.

## 2. Merge and completion model

```mermaid
flowchart TD
    PR["Worker PR"] --> A["Layer A: GitHub-hosted required CI"]
    A -->|FAIL| RepairA["Repair budget"]
    RepairA --> PR
    A -->|green / eligible| Enqueue["Controller enqueues exact PR head"]
    Enqueue --> Group["Protected merge_group"]
    Group -->|required / gate PASS| Develop["GitHub protected squash -> develop"]
    Develop --> PushA["Trusted post-merge Layer A on exact SHA"]
    PushA -->|FAIL| RepairA
    PushA --> Profile{"Ticket completion profile"}
    Profile -->|layer_a_post_merge| DoneA["Ticket Done"]
    Profile -->|Layer B required| B["Self-hosted Windows 11 + real OBS"]
    B -->|PASS + cleanup PASS| DoneB["Certification Done / release eligible"]
    B -->|FAIL| RepairB["Repair budget"]
    RepairB --> PR
```

The manifest assigns one completion profile per ticket:

- `layer_a_post_merge`: W11-001 through W11-009 require pre-merge `required / gate`, GitHub's protected merge-queue squash of the exact head SHA, and a distinct trusted `ci.yml` push run that succeeds on the exact merged `develop` SHA.
- `layer_a_post_merge_automated`: W11-010 requires the same evidence, but its authoritative post-merge provider is the newly merged `post-merge.yml` automation.
- `layer_a_plus_layer_b_exact_sha`: W11-011 and W11-012 require authoritative post-merge Layer A plus `windows11-integration / obs-e2e` success and cleanup success on the same exact merged SHA. W11-012 also requires release-readiness evidence.

Layer A always gates protected merge-queue execution. The pre-queue exact-head
GitHub auto-merge path is superseded after queue activation and is valid only as
the documented legacy fallback before activation. Layer B is never required for
a ticket that builds or bootstraps Layer B. A failure affects only the ticket
whose completion profile selected that layer and its repair lineage.

The active queue cutover does not retroactively invalidate W11-001 or W11-002:
their recorded pre-activation GitHub squash auto-merge evidence remains
grandfathered historical completion evidence. New tickets must satisfy the
active merge-queue contract.

## 3. Layer A — ordinary Windows CI

### Trigger and runner

- Events: `pull_request`, `push` to `develop`; add `merge_group` when merge queue is enabled.
- Primary Windows runner: `windows-latest`, Python 3.12.
- macOS regression and package jobs remain required inputs to the same aggregate gate.
- No self-hosted runner is used for pull-request code.

### Required jobs

1. `layer-a / windows-unit`
   - `uv sync --frozen`
   - package import and CLI `--help`
   - config, matcher, zoom, platform selection, Windows ctypes-mock tests
   - hardware-free controller and application component tests
2. `layer-a / macos-regression`
   - locked install and existing macOS behavior/tests
3. `layer-a / package`
   - build wheel/sdist and import the built artifact
4. `required / gate`
   - depends on every required Layer A job
   - runs even when a dependency job fails
   - succeeds only if every required result is successful

### Non-circular bootstrap

- W11-001 introduces `.github/workflows/ci.yml` with the final stable job names. Its explicit `bootstrap` mode tests only the dependency-lazy OBS probe, model-routing validator, manifest consistency, and planning artifacts. It uses pinned actions, `contents: read`, no secrets, no microphone, no model download, and no real OBS.
- Every bootstrap job and artifact must identify `ci_stage=bootstrap`; no later ticket may silently reuse that reduced suite.
- W11-002 adds the Darwin-only Quartz marker, platform seam, `uv.lock`, and `ci_stage=full_activation`. Its PR must remove the bootstrap exemption and make `required / gate` fail unless full mode is active.
- W11-005 hardens the already-active full Layer A suite, validates `merge_group`, and proves that an intentionally failing temporary change makes the aggregate gate fail.
- Through W11-009, the trusted `push` run of `ci.yml` is the temporary post-merge provider. The controller records workflow ID, run URL, check-suite app, merged SHA, and conclusion; only `success` on the exact merged SHA is acceptable.
- W11-010 makes `post-merge.yml` the authoritative post-merge Layer A provider. This avoids requiring a downstream workflow before the ticket that creates it is complete.

### W11-010 post-merge producer/finalizer 契約

- `post-merge.yml` 是可信任的 `push`-to-`develop` producer。它的 job graph 與相關 environment 必須從 `ci.yml` 複製，只有 `contents: read`，不得寫入 issues、checks、Actions state 或 pull requests。
- `post-merge-finalize.yml` 是獨立 workflow，只接收指定 producer 在 `develop` 完成的 run，並提供只有 `github.ref == refs/heads/develop` 才能執行的 trusted manual reconciliation；它不是 pull-request 或 merge-group publisher。它的 top-level permissions 為空，唯一 job 保留 `contents: read`、`actions: read`、`checks: read`、`pull-requests: read` 與供隔離 simulation 使用的 `issues: write`，並以 `cancel-in-progress: false` 序列化；不再存在任何 `pull-requests: write` continuation 例外。
- Finalizer 重新抓取並驗證 repository identity、producer workflow ID/path、`push` event、`develop` branch、completed status、source SHA、current run attempt、該 SHA 對應的 GitHub Actions check-suite identity、分頁取得的全部 required jobs，以及唯一 base 為 `develop` 且 `merge_commit_sha` 等於 source SHA 的 merged PR。Producer event 的 source SHA 是權威值，不採用 finalizer 自己的 SHA。
- Controller-owned registration 必須在 queue enrollment 前，經 protected `develop` 提交。每張 ticket 可使用以下窄幅 schema：

  ```yaml
  post_merge_registration:
    version: 1
    attempts:
      - pr_number: <real originating PR number>
        predecessor_pr_number: null
        repair_issue_number: null
  ```

  Repair attempt 使用 immediate predecessor PR 與已存在的 repair issue number。Worker 不得自行編造 PR number、commit SHA 或 future issue ID；merged SHA、run ID、comment 與新建 issue ID 都是 runtime evidence 與 controller bookkeeping。
- Issue body、label 與 PR comment 都是可編輯 projection。Reducer 從 verified run 與 protected registration 重建 retry state，分頁搜尋 open/closed issues，驗證 bot author 與 machine payload，去重相同 run/attempt 的 rerun，並讓較新的 failure 對 stale PASS 保持權威。每次 invocation 都 reconcile protected `develop` 可見的所有 registered lineage run，不只處理觸發它的 delivery。
- Cleanup 後，`paired_comment_diagnostic_registration` 與 `pr_comment_authorization_continuation_registration` 僅保留為 historical evidence，任何重新出現或 malformed block 都 fail closed，絕不重新武裝 diagnostic。Protected manifest 的 production publication pause 固定為 `state=blocked`、`reason=pending-authorization`、`durable_permission=awaiting_explicit_authorization`、`diagnostic_state=consumed`，並明確停用 production `POST`、`PATCH`、`DELETE`。
- `workflow_run` 與 `workflow_dispatch` 的正常 reconciliation 仍先以 GET-only 驗證 protected tip、producer run、check suite 與 merged PR（若 delivery 提供 producer run），然後以 `pending-authorization` 非零失敗退出。這個 failure 是授權阻塞，不是 code failure：不建立／更新 Issue #14、不改 retry state、不執行任何 production POST/PATCH/DELETE，也不把跳過 publication 偽裝成 success。Cleanup merge 觸發的可信 producer 與 finalizer run #6 因此應清楚失敗並等待明確授權。
- Finalizer 的 `workflow_dispatch.simulation_case` 只允許 `none`、`pass`、`fail`、`rerun`；非 `none` 時固定使用 `w11-010-simulation` namespace，透過 real Issues API 建立／更新隔離 projection，不讀寫 manifest、PR comment、production ticket 或 production retry state。`rerun` 必須找到既有 simulation failure，且不得增加 retry count。
- W11-010 acceptance 分三階段：(1) merge 前 fake-transport publisher tests；(2) protected merge 後 exact merged-SHA producer/finalizer evidence；(3) 隔離且明確標示 simulated 的 live PASS/FAIL/rerun publication 與 issue read-back。Local tests 不得取代第 (2) 或第 (3) 階段；本次 zero-mutation cleanup 與 pending-authorization failure 也不等於 W11-010 完成。

### Merge execution contract

- Protect `develop` with pull requests and required check `required / gate`.
- Require the latest pull-request commit to have current checks.
- Do not permit admin bypass, force push, or direct worker merge.
- With the active merge queue, enrollment and merge execution are distinct. The orchestrator may enqueue the verified exact latest head only after the input schema is checked; GitHub may execute the protected squash only after the queue creates a `merge_group` whose `required / gate` and every configured branch rule succeed on that same head. Enrollment is neither merge authority nor completion evidence.
- If queue enrollment is rejected, do not directly merge, weaken protection, add an artificial review requirement, manufacture a failing check, or create an empty/no-op commit. If the queue is not yet active, the pre-queue exact-head squash auto-merge is a documented legacy fallback only; once the queue is active, a queue failure is fail-closed and the legacy path is not a substitute.
- W11-001 performed the one-time no-browser bootstrap using an out-of-band, non-interactive repository-administration credential. Its exact-head squash auto-merge was the pre-queue legacy enrollment path and is superseded by the active merge queue.
- The rule activation timestamp must precede `mergedAt`. After the separately approved and consumed empty-origin baseline seed, direct push, REST merge, manual merge, `--admin`, and every check bypass are forbidden.

### Merge queue contract

- The active `develop` queue is the preferred and, once activated for W11-009, authoritative merge path. A green, open PR is eligible; enrollment is controller-owned and is not a merge or completion action.
- Before enrollment, the controller must read the GraphQL `EnqueuePullRequestInput` schema and confirm `pullRequestId: ID!`, `expectedHeadOid: GitObjectID`, `jump: Boolean`, and `clientMutationId`. It then calls `enqueuePullRequest` with `expectedHeadOid` equal to the distinct latest PR head SHA. A worker never enqueues its own PR.
- GitHub must build a protected `merge_group`, run the authentic GitHub Actions `required / gate` for that group, and execute the protected `SQUASH` only after every branch rule succeeds on the same head. The configured queue is `ALLGREEN`, one entry to build/merge, one-minute minimum wait, and a 60-minute check-response timeout.
- Required evidence is distinct: PR head SHA, queue entry, merge-group SHA plus workflow/check-suite provider, GitHub-created final merged `develop` SHA, and a trusted `push` `ci.yml` success on that exact merged SHA. Settings read-back, enrollment, or a pending queue entry alone never makes a ticket Done.

## 4. Layer B — real Windows 11 OBS integration

### Trigger and runner

- W11-010 實作 inactive reusable/manual workflow、harness、cleanup 與 repair behavior，但不在每次 `develop` push 啟動 live OBS；該 workflow 沒有 `push`、pull-request 或 merge-group trigger。
- W11-011 才會為 certification commit 啟用 trusted `push` 到 protected `develop` trigger。
- Optional recovery/debug event 為針對 exact commit、經人工核准的 `workflow_dispatch`。
- 在配置 self-hosted runner 前，先執行 hosted preflight；只接受 canonical repository、`refs/heads/develop`、目前 protected `develop` 的 exact SHA 與 separately recorded hardware authorization。Live harness 強制鎖定 `127.0.0.1`，在啟動自有 OBS 前證明 port `4455` 未被占用，並分開回報 primary failure 與 cleanup failure。
- Runner labels: `[self-hosted, Windows, X64, obs-integration]`.
- Use an interactive logged-in Windows 11 desktop session. Do not run the OBS integration runner as a non-interactive service session.
- Serialize runs with one concurrency group for the OBS runner; do not cancel an active cleanup sequence.

### Dedicated test environment

- Dedicated OBS executable/version record, test profile, scene collection, and display-capture source.
- WebSocket endpoint `127.0.0.1:4455`; password supplied through a scoped GitHub secret.
- The test scene must not reuse a user's production streaming profile.
- The runner must record Windows build, OBS version, commit SHA, monitor layout, DPI, and source name without exposing secrets.

### Integration sequence

```mermaid
flowchart TD
    Merge["develop merge"] --> Action["GitHub Action"]
    Action --> Runner["self-hosted Windows 11"]
    Runner --> StartOBS["Start dedicated test OBS"]
    StartOBS --> Wait["Wait for localhost:4455"]
    Wait --> Snapshot["Read and save baseline transform"]
    Snapshot --> Harness["Run integration harness"]
    Harness --> ZoomIn["Send zoom command through production path"]
    ZoomIn --> ReadIn["Read OBS scene-item transform"]
    ReadIn --> AssertIn["Validate zoom-in state"]
    AssertIn --> ZoomOut["Send zoom-out command"]
    ZoomOut --> ReadOut["Read OBS transform"]
    ReadOut --> AssertOut["Validate baseline restore"]
    AssertOut --> Result["PASS / FAIL"]
    Result --> Cleanup["Always restore transform and stop test OBS"]
```

### Harness contract

- Do not use live speech recognition as the command source for the required Layer B gate; inject deterministic zoom commands through the production application/controller boundary.
- Connect to the same production `ObsClient` path used by the application.
- Snapshot the original transform before mutation.
- After zoom-in, poll with a bounded timeout and verify scale and position changed to the expected zoom state.
- After zoom-out, verify position error is at most 0.5 px and scale error is at most `1e-4` from baseline.
- Include a selected-monitor assertion when the self-hosted runner has the declared multi-monitor fixture.
- On any exception or timeout, run cleanup and report both the primary failure and any cleanup failure.
- The Layer B check name is `windows11-integration / obs-e2e`.
- W11-001 through W11-010 must not start dedicated OBS as a completion gate. Their real-OBS probe result may be absent, `MISSING_OBS`, or `UNREACHABLE_ENDPOINT` without consuming retry budget.

## 5. Retry budget and repair loop

The code retry budget is counted by repair lineage, not by individual failed jobs in the same workflow run.

| Consecutive code failure | Automated action | Ticket state | Model |
| --- | --- | --- | --- |
| 1 | Create/update Repair Ticket and apply first repair | Blocked -> Ready/Doing | implementer |
| 2 | Attach both failures and apply second repair | Blocked -> Ready/Doing | implementer |
| 3 | Stop worker dispatch and perform root-cause analysis | Blocked + arbitration requested | arbitrator |
| 4 | Stop all automatic repair and apply `needs-human` | Blocked + needs-human | human decision required |

After third-failure arbitration, the arbitrator records one of: targeted repair instructions, ticket split, dependency/DAG change, rollback, or request for human input. Implementation remains with the implementer unless the user explicitly changes the role policy.

### Retry accounting rules

- Count one code failure per distinct remediation attempt and resulting failing commit.
- Deduplicate workflow reruns for the same commit and failure signature.
- Reset `consecutive_code_failures` only after the complete required workflow for that layer passes on the latest relevant commit.
- Preserve `total_code_failures` for audit history.
- Confirmed runner offline, GitHub outage, unavailable secret, unavailable OBS lab, authentication/authorization failure, or unmet approval gate does not consume the code budget. Retry infrastructure at most twice, then block the owning ticket and its descendants if the external capability cannot be restored safely; independent Ready tickets continue.

## 6. State transitions

- Merge-queue enrollment: the orchestrator may enqueue the verified exact latest head only after the `EnqueuePullRequestInput` schema is checked and `expectedHeadOid` is bound to that SHA; enrollment does not merge the pull request and is not completion evidence.
- Layer A PASS: GitHub may execute the protected `SQUASH` only after it creates the `merge_group` and that group's `required / gate` plus every configured branch rule succeed on the same exact head.
- The historical `enablePullRequestAutoMerge_SQUASH` enrollment path is superseded after queue activation and is valid only before the queue external gate; preserved attempt records are audit history, not a current completion requirement.
- Layer A FAIL: PR cannot merge; use the retry budget.
- Merge completed: Ticket becomes `Merged`, never immediately `Done`.
- Trusted post-merge Layer A PASS on the exact merged SHA: a `layer_a_post_merge` or `layer_a_post_merge_automated` ticket becomes `Done`.
- Layer B PASS plus cleanup PASS on the same exact merged SHA: a `layer_a_plus_layer_b_exact_sha` ticket may become `Done` after any release-readiness conditions are also satisfied.
- Required completion-profile failure: Ticket becomes/stays `Blocked`, a Repair Ticket is Ready for code-attributable failure, and the appropriate retry state advances.
- Fourth code failure: no worker is dispatched until a human explicitly resumes or replaces the ticket.

## 7. Unattended, no-browser execution

- `browser_launch` is forbidden while unattended. Do not open device authorization, OAuth, repository settings, or any other interactive browser flow.
- Allowed authentication sources are an existing authenticated CLI session, an existing scoped token, or existing app credentials. OpenAI/Codex authentication is not evidence of GitHub authentication.
- If a non-interactive credential or permission is missing, mark only the owning external operation and its transitive descendants `Blocked`; do not consume code retries and do not weaken any gate.
- Continue dispatching independent Ready tickets whose dependencies, touch-set constraints, routing, and own approval gates are satisfied.
- Repository-administration credentials remain out of Actions, repository files, logs, artifacts, and model prompts. PR jobs retain `contents: read`; the trusted repair automation uses only the minimum separately documented permissions.
- Unattended mode is fail-closed, not a promise that absent GitHub credentials, admin permission, release approval, or a Windows 11 hardware lab can be manufactured automatically.

## 8. Ticket ownership

- W11-001 bootstraps `ci.yml`, the stable job names, and the initial zero-bypass repository rule/auto-merge evidence.
- W11-002 activates the full cross-platform CI mode and lock; W11-005 hardens Layer A and its stable aggregate required check.
- W11-009 audits and hardens branch protection, required checks, merge queue/auto-merge, and PR policy after the bootstrap.
- W11-010 implements authoritative post-merge Layer A, the inactive Layer B workflow/harness, Repair Ticket creation, deduplication, and retry enforcement.
- W11-011 activates Layer B on trusted certification pushes and records the real Windows 11 evidence.
- W11-012 validates both exact-SHA layers again for the release candidate; tag and GitHub Release publication remain separately approval-gated.
