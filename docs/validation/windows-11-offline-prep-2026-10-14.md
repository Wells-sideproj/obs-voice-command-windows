# W11-011-PREP 離線準備與 Organization 證據

## 目前狀態（2026-10-16）

以下早期區段與仲裁 JSON 是保留的歷史快照；驗收範圍以後述「2026-10-15 單螢幕 MVP 範圍變更」及最新 manifest 為準。W11-011 負責單螢幕 activation／認證，W11-012 負責單螢幕交付，多螢幕實機證據延後至 W11-013；不是刪除多螢幕功能或 mock regression。

2026-10-15 具名 Luna worker Aristotle（`01a14029-f948-7692-8837-cc0b95d12207`）已交回七檔，回報 integration 295 passed、完整 suite 436 passed/3 skipped。此為該次 worker 回報，尚非最終樹 controller 複驗或 GitHub CI 證據。Controller 審查發現 mutex release 與 primary 同時失敗的紀錄可能遺失，lifecycle 時鐘亦未明確注入；白名單診斷需補負向驗證。

2026-10-16 原 agent 的 `send_input` 回傳 not found，未 resume 或改用父模型。routing validator 再次 PASS 後，重新派送具名 `windows_worker` Socrates（`01a142a2-6247-73e3-bf57-7ac1669d56d8`），工具角色固定 `gpt-5.6-luna/max`，只修原七檔與既有 AC；controller 管理 Ready／Doing claim。這不是 code CI failure，不增加 retry。

目前全為本機未提交的 PREP 變更，尚無本票 PR 或 merged-SHA 證據；self-hosted workflow 與 live CLI／adapter 仍硬停用。既有 gh 唯讀重新確認 develop=`b4ae89923f2a55c9e132f584d0ce4cb2e7b50580`、ruleset20990581 active/bypass=[]/required gate app15368/strict/SQUASH ALLGREEN queue，並確認本分支無 PR；未使用 GCM 或 token 匯出。

## 範圍與來源

### 2026-10-16 最終本機複驗

具名 Socrates 完成同一七檔範圍內四檔追加修補：harness、lab helper 及兩個對應測試。Mutex release failure 與 primary error 同時出現時均保留；只有 release 失敗則為 cleanup-only，未取得 mutex 不清理非 owned process。Lifecycle clock/sleep 明確注入；診斷固定白名單與有限 transform，實際 argparse unknown/missing-value 路徑不回顯內容，help 保留。

Worker 回報完整 `447 passed, 3 skipped`；controller 在同一最終樹獨立執行 `python -m pytest tests -q --tb=short` 得 `447 passed, 3 skipped in 5.51s`，routing validator 與 `git --no-optional-locks diff --check` 均 PASS。另已通過14票 DAG 無循環／W11-012 不依賴 W11-013 的檢查及 production CLI help smoke。測試使用既有 production worktree venv、process-local PYTHONPATH/UTF-8，沒有安裝或變更共享環境。

本機測試不提供合併權，也不代表 Layer B 認證。交付仍須本票 PR head／merge-group 的 required gate 與 exact merged SHA 的 trusted `ci.yml` push、`post-merge.yml` producer；尚未產生的 PR／run 不在此預填。真實 runner、OBS、microphone 與 live runtime 均未啟用。

Controller 另重跑本票三個相關測試檔，`41 passed in 0.68s`。全套的3個 skip 來自既有 `tests/test_integration.py` 的 ASR model cache／macOS `say` 環境 guard；不是 Layer B PASS 或豁免。此次沒有取得語音或實機證據。

Controller 依既有 `windows-11-e2e-2026-10-13-arbitration.md` 的允許準備路徑實體化子票 W11-011-PREP，並依使用者 2026-10-14「繼續」開始。此票不代表 W11-011 certification，不安裝或啟用 runner／OBS／runtime，不開麥克風或讀取憑證。

基底是已即時唯讀核對的 protected develop `b4ae89923f2a55c9e132f584d0ce4cb2e7b50580`。獨立工作樹 `C:\Users\Wells\.codex\worktrees\w11-011-offline-prep\obs_voice_command`，分支 `codex/w11-011-offline-prep`。原 root 的26項與 production 工作樹20項修改均保留。

## 使用者提供的 API read-back

以下是本對話人類貼回的 gh GET 結果，不是 controller 在本輪持有 PAT 後查詢。沒有保存任何 token；採最小 Organization Self-hosted runners 細粒度權限，不擴張既有 gh OAuth。

```json
{
  "id": 3,
  "name": "w11-obs-integration",
  "allows_public_repositories": true,
  "restricted_to_workflows": true,
  "selected_workflows": ["Wells-sideproj/obs-voice-command-windows/.github/workflows/windows11-integration.yml@refs/heads/develop"],
  "visibility": "selected",
  "repositories": [{"full_name": "Wells-sideproj/obs-voice-command-windows", "id": 1338358349}],
  "total_count": 1
}
```

證據對應 `GET /orgs/Wells-sideproj/actions/runner-groups/3` 與 `/3/repositories`。最初 empty selected_workflows 已由使用者修正並重新讀回；Default ID1 保留禁止public的既有設定。這解除 org policy 讀取與設定白名單 blocker，但不證明 PAT 寫入權、runner membership、目前沒有漂移或 Layer B 通過。註冊／啟用前須再即時查核 group、repo、workflow 及實際 runner membership。

## 交付界線

- 只派具名 windows_worker（gpt-5.6-luna/max）；仲裁 windows_arbitrator（gpt-6-astra/medium）。
- 準備票完成須 protected PR/merge-group gate 與同 merged SHA trusted ci.yml push、post-merge.yml Layer A producer 全成功；不借用 W11-010 的舊 SHA 證據。
- 不新增 post_merge_registration，不擴張既有 W11-010/root13 publisher 授權。Controller 管理本票 completion/retry。
- W11-011 保持所有原 AC、Layer B completion profile 與雙螢幕實機要求。此票無 push 啟用；真正 certification SHA 待 W11-011 合併後才產生。
- 既有 W11-010 Done ledger 同步進本獨立 PR，原歷史檔內「尚未提交」是當時紀錄；不是重推或修改已合併 PR21。

## 驗證與待辦

初始路由驗證 PASS；新工作樹基底乾淨。準備實作與本票測試尚待 worker 完成；尚無本票 PR/CI/merged-SHA 證據，禁止標 Done。使用者於2026-10-14明確確認沒有雙螢幕；這是人類提供的硬體限制，不是本輪重新列舉顯示器的結果。離線準備不因此受阻，也不要求購買或接上第二台螢幕。多螢幕負座標／mixed-DPI 真機認證仍標「未驗證」；單螢幕 DPI 或 fake tests 不能冒充同時多螢幕實測，原 W11-011/012 AC 尚未豁免。

Controller 在 worker 產品修改前，以既有 production `.venv/Scripts/python.exe -m pytest tests -q --tb=short` 配合單次程序 `PYTHONPATH=<本準備工作樹>/src` 跑新工作樹 baseline：416 passed、3 skipped（13.34s）。未安裝套件或修改共享環境；此結果僅是新增 controller ledger 後的基準，worker 最終修改仍須另測。

2026-10-14 派工：具名 windows_worker `Maxwell`，agent `01a13614-1dd9-79f0-835f-38dda9c1f987`；工具角色契約固定 `gpt-5.6-luna`／`max`，不是繼承 parent model。派工前新工作樹 routing validator 與 diff check PASS。

同期使用既有 gh 唯讀確認 W11-010 producer38148005163（push）與 simulation rerun38331190028（workflow_dispatch）均 completed/success、headSHA=b4ae89923f2a55c9e132f584d0ce4cb2e7b50580。Ruleset20990581 active、bypass=[]、required / gate integration15368 strict、SQUASH/ALLGREEN merge queue 仍有效。這些是前置票／保護規則的複查，不是新準備票的 completion evidence。

## 中斷與仲裁恢復

Controller 在提交 PR 前發現草稿的 caller-supplied `authorized_sha` 不能當作獨立批准；排除舊 baseline SHA 亦無法保證新 PREP merge 仍不能啟動 live runner。runtime path/version proof、可選 lab 的繞路與 cleanup error redaction 同屬待仲裁契約。已 interrupt worker，PREP 設 Blocked、仲裁 requested；這是非 CI 契約升級，不消耗 code retry。

原仲裁 `windows_arbitrator`（agent `01a132da-4844-7120-9200-1ffc3a38ba7c`，`gpt-6-astra/medium`）於額度中斷後恢復，追加單螢幕限制與 hard-disabled PREP workflow/live CLI 決策問題，submission `01a137d0-dfca-7632-88b6-b84d5f1fad76`。尚未取得此 follow-up 決策前，不恢復 worker 改檔、不建立 PR、不啟動 runner/OBS。

## 2026-10-15 單螢幕 MVP 範圍變更

同一 Bernoulli 具名仲裁後續以 `targeted_repair`、`human_input_required=false`、`confidence=high` 明確批准 controller_touch_set 追加 `AGENTS.md`，僅同步單螢幕/多螢幕證據歸屬及當期 release mandatory set；路由、安全、合併、權限、exact-SHA 門檻不變。此例外不授予 worker 修改 controller 文件。

使用者明確批准「先完成單螢幕版本，多螢幕認證另案延後」。此新決策取代本檔較早關於 W11-011 必須雙螢幕實測才可 Done 的現行效力，舊 JSON 保留為歷史。僅記錄現有 DPI／解析度、不自動改顯示設定，是 controller 依環境安全邊界提出並由仲裁保留的限制，不額外虛構使用者的測試結果。

新具名 windows_arbitrator Bernoulli（`01a1402c-70df-7862-8598-6ae30c3691ea`），spawn 工具固定 `gpt-6-astra/medium`，裁定 W11-011 單螢幕 activation/certification、W11-012 單螢幕 release、W11-013 延後多螢幕；同 SHA Layer A／B／cleanup、安全與保護合併規則不降低。PREP worker 不受影響。

```json
{
  "ticket_id": "W11-011",
  "decision": "dependency_dag_change",
  "root_cause": "使用者於 2026-10-15 正式將當期 MVP 改為單螢幕認證，但現行 plan、WORKFLOW 與 manifest 仍要求雙螢幕 mixed-DPI、負座標及跨螢幕 freeze 實機證據，W11-012 又要求所有 W11 tickets Done。此外，PREP 已要求 workflow、live CLI 與實際 adapter 硬停用，因此 W11-011 現有 workflow＋文件 touch_set 無法完成安全 activation。最小變更是保留 W11-011 activation 與單螢幕認證責任，新增一張 W11-013 延後多螢幕認證，並明定當期 mandatory set；不另建 activation 前置票。",
  "evidence": [
    "已唯讀核對指定 worktree 的 AGENTS.md、WORKFLOW.md、完整 port plan／manifest、.codex/config.toml、兩個具名 agent 設定，以及 windows-11-offline-prep-2026-10-14.md 的完整仲裁記錄與其引用的 2026-10-13 仲裁。",
    "git --no-optional-locks rev-parse HEAD 回傳 b4ae89923f2a55c9e132f584d0ce4cb2e7b50580。status／顯式 work-tree 檢查遭環境權限錯誤，故未宣稱取得完整 diff 或工作樹乾淨；未修改檔案、執行測試或操作外部服務。",
    "manifest 記錄 W11-001～010 Done、PREP Doing，worker 為 01a14029-f948-7692-8837-cc0b95d12207、windows_worker/gpt-5.6-luna/max；W11-011／012 為 Blocked。完整 manifest 及相關 plan／tools／tests 搜尋未見 W11-013 已佔用。",
    "plan 的 W11-011 AC4 要求單螢幕及雙螢幕 mixed-DPI／負座標，AC7 同時要求 capture monitor tracking 與跨螢幕 freeze；W11-012 AC4 要求所有 W11 tickets Done。這些是本次正式範圍授權需要替換的現行條文。",
    "manifest 的 layer_a_plus_layer_b_exact_sha 要求 trusted post-merge Layer A、真實 obs-e2e、cleanup 及同一 exact merged SHA；其本身未規定顯示器數量，可保留 profile 名稱與全部證據強度。",
    "PREP touch_set 恰為 workflow、harness、lab helper、三個測試檔及非秘密 fixture。已檢視相關程式與測試落點；讀取期間 worker 正在修改，觀察到部分 lab helper 已加入 structural_valid/runtime_verified=false，其餘草稿不代表最終 PREP 交付。本次不重新裁定 PREP 修復。",
    "tests/test_windows_display.py 保有負座標與 mixed-DPI physical-pixel mock tests；tests/test_controller.py 保有非擷取螢幕 freeze 及負座標案例。這些既有實作與測試應保留，不能轉稱實機認證。",
    "group 3 證據明確標為 user-supplied read-back，不能證明目前 policy、runner membership 或某個 certification SHA 已獲批准。既有 current-PC setup approval 與啟用前 fresh validation 契約均仍有效。",
    "使用者本輪明確批准先完成單螢幕、多螢幕另案延後，並限定只記錄現有 DPI／解析度，不自行改 Windows display settings。本次為 planning scope arbitration，沒有 failing check、失敗 command 或 run URL，不是 code CI failure。",
    "設定檔確認 windows_arbitrator/gpt-6-astra/medium 與 windows_worker/gpt-5.6-luna/max；controller 記錄本裁決時仍須附本次實際具名派送身分或 explicit model/effort metadata，不得以設定檔代替執行身分證據。"
  ],
  "directed_repair": [
    "核可當期 Windows 11 MVP 為單螢幕、實際現有解析度與 DPI 的認證範圍。多螢幕、跨多 DPI 真機矩陣、負座標排列及跨螢幕 freeze 實機認證移交 W11-013；功能、Win32 backend、monitor mapping、既有 mock 幾何矩陣及 regression tests 全部保留。",
    "W11-011 AC4 直接替換為：在真實 Windows 11 單螢幕環境記錄實際 display count、解析度、physical-pixel rectangle、現有 DPI／縮放、monitor identity 與 OBS capture mapping，並在該配置完成認證；不得宣稱覆蓋未測 DPI。不得為此自動修改縮放、解析度、display layout、driver 或重新啟動系統。",
    "W11-011 AC7 直接替換為：在實際單一 capture monitor 上驗證游標追蹤、zoom center 與 production command-to-transform 路徑；跨螢幕 freeze 的實機證據由 W11-013 負責，目前標記 unverified/deferred。單螢幕 certification 不再等待第二台顯示器。",
    "W11-011 AC1～3、5～6、8～10 保留：同一 exact merged SHA 的 trusted Layer A、真實 deterministic Layer B 與 cleanup；遮蔽後 OBS API／baseline 證據；每個預設正向 phrase 各10次皆觸發、至少10個負向 utterances 零誤觸、至少9/10次於 phrase 結束後1.5秒內開始 transform；zoom out／Ctrl-C／reconnect 後 position誤差≤0.5px、scale誤差≤1e-4；devices、named device、dry-run、首次模型下載及第二次 cache hit。語音與 latency 實測另附同 SHA 證據，不改 deterministic Layer B 為依賴語音輸入。",
    "批准 W11-011 在 PREP Done 後接手原七檔的 activation 範圍：workflow 建立可信 preflight 與受控 live job；harness 恢復受可信授權約束的 CLI／public live wrapper／實際 spawn adapter；lab helper 補實際 readiness 驗證與純結構驗證的明確區別；三個測試檔驗證合法 activation 及拒絕路徑。fixture 只允許必要的非秘密契約更新，若 PREP 最終版本已足用則不必修改。另保留 W11-011 evidence docs，不擴張產品 backend、publisher 或全域 runtime。",
    "activation 必須以 PREP 最終受保護合併版本為基底。將 PREP 的『永遠停用』測試有依據地演進為『缺少有效可信授權必拒絕，授權完整才可進入 live』；保留 fake lifecycle、portable argv/cwd、listener ownership、互斥、cleanup、秘密 sentinel 與既有 regression 覆蓋。不可單純刪除測試、加入 allow-live flag 或用 approved 環境字串自證授權。",
    "保留既有啟用前契約：fresh group/repository/workflow/membership read-back、隔離 portable OBS、可信 bootstrap canonical paths／完整性／ACL、Python3.12／uv0.11.8／venv／cache／針對 certification checkout 的 frozen install。OBS 使用專用 profile／collection、--portable、cwd=executable.parent、127.0.0.1:4455；只停止本次 owned process，完整 finally 與 baseline read-back，primary／cleanup failure 分別阻擋。",
    "將 code readiness 與 live readiness 分階段記錄以避免循環：PREP Done 且 W11-011 派工所需路由／既有批准及前置 readiness 成立後，worker 可交付 activation PR；未知的最終 SHA 不作 PR 前必須已存在的條件。合併產生 S 後，controller 才以既有批准及 lab readiness 建立 caller 不可改寫、可撤銷的 repo/ticket/workflow revision/S 授權紀錄。未取得紀錄時 hosted preflight 拒絕排程 live；取得後走既有 authorized exact-commit dispatch，無須另造 bookkeeping 前置票。",
    "W11-012 保留單獨的 release candidate exact SHA 驗證；不能借 W11-011 舊 SHA 的 Layer B PASS。README／release notes 在證據齊備後明示『single-monitor certified，限所列實測 DPI／解析度；multi-monitor unverified，後續追蹤 W11-013』。證據未齊前寫 pending certification，不先宣稱 certified；不移除多螢幕功能。",
    "W11-013 保留原多螢幕實機範圍：至少一組真實雙螢幕 mixed-DPI，包含左側或上方負座標；100%／125%／150%／200% 的跨 DPI 真機矩陣以明確案例及逐項結果記錄；驗證 OBS monitor_id 對應、擷取螢幕 tracking、移至非擷取螢幕 freeze、返回後追蹤與 zoom out／Ctrl-C／reconnect restore。任何尚未執行項目不得 PASS；mock、單螢幕縮放或虛擬顯示不得冒充雙螢幕。實際設定變更另依批准邊界安排，本裁決不授權自動更動顯示設定。",
    "Controller 同步 plan 的 MVP、DoD、DAG、W11-011／012／013 AC 及 WORKFLOW 的 completion 說明；offline-prep 文件追加帶日期的範圍變更，保留舊仲裁為歷史，不覆寫其當時結論。不得中斷、重派或修改目前 PREP worker 的七檔；後續 activation 與 PREP 序列化。"
  ],
  "manifest_changes": [
    "新增 plan_arbitrations 項目 PLAN-W11-SINGLE-MONITOR-MVP-2026-10-15：state=resolved、decision=dependency_dag_change、human_input_required=false、consume_code_retry_budget=false；approval_basis 記錄使用者『先完成單螢幕版本，多螢幕認證另案延後』及本輪現有 DPI／解析度限制。實際 agent ID／route metadata 由 controller 依派送證據填入，不編造。",
    "W11-011-PREP 完全保留 status=Doing、worker=01a14029-f948-7692-8837-cc0b95d12207、depends_on=[W11-010]、completion_profile=layer_a_post_merge、additional_done_requires、七檔 touch_set、hard-disabled AC、retry 與既有路由。不得新增 W11-013 依賴。",
    "W11-011：title='Windows 11 single-monitor activation and real-machine certification'；depends_on=[W11-010,W11-011-PREP]；completion_profile=layer_a_plus_layer_b_exact_sha；ci_stage=layer_b_activation；當下 status=Blocked；blocked_reason=offline_preparation_and_single_monitor_activation_readiness_pending。blocked_on 移除 mixed_dpi_negative_coordinate_fixture_missing，保留 W11-011-PREP、runner_membership_and_pre_activation_policy_revalidation，並明列 isolated_lab_runtime_acl_readiness；合併後尚缺的 exact-SHA authorization 記於 live readiness，不設成 code PR 的前置依賴。",
    "W11-011 acceptance_criteria 採上述單螢幕 AC4／AC7 替代及其餘保留 AC；readiness 保留實際 mixed_dpi_negative_coordinate_fixture_available=false、multi_monitor_hardware_certification=unverified，新增 certification_scope=single_monitor_existing_configuration、deferred_certification_ticket=W11-013。上述多螢幕欄位改為資訊性證據，不參與本票 Ready／Done blocker 判斷。",
    "W11-011 touch_set 明列 [.github/workflows/windows11-integration.yml, tools/windows11_integration_harness.py, tools/windows11_lab.py, tests/integration/windows11/test_layer_b_harness.py, tests/integration/windows11/test_workflow_contracts.py, tests/integration/windows11/test_windows11_lab.py, tests/integration/windows11/fixtures/windows11-lab.example.toml, docs/validation/windows-11-e2e-*.md]。不新增 activation ticket；controller 文件變更維持 controller ownership。",
    "W11-012：title='Single-monitor release candidate and final handoff'；status=Blocked；blocked_reason=dependency_blocked_by_W11-011；depends_on=[W11-011]；completion_profile=layer_a_plus_layer_b_exact_sha；ci_stage=release_candidate；release_readiness_required=true。不依賴 W11-013，維持既有 release touch_set 及 tag／GitHub Release 明確批准 gate。",
    "新增 W11-013：epic=E4；title='Deferred multi-monitor, mixed-DPI and negative-coordinate real-machine certification'；status=Blocked；blocked_reason=deferred_by_user_2026_10_15_multimonitor_fixture_and_live_readiness_pending；depends_on=[W11-011]；completion_profile=layer_a_plus_layer_b_exact_sha；ci_stage=layer_b_certification；execution={worker_profile:implementer,arbitration_profile:arbitrator,escalation_threshold:low}；approval_gates=[windows11_real_hardware_lab]。Deferred 僅作標題／範圍描述，不新增 status enum，不繼承尚未證實涵蓋新硬體的批准。",
    "W11-013 touch_set 初始限 [docs/validation/windows-11-multimonitor-*.md, docs/windows/**]，AC 採上述完整多螢幕實機契約及同 SHA Layer A／Layer B／cleanup；沿用 W11-011 已啟用 harness。若未來需產品或 harness 修復，先提出具體失敗及所需 touch_set，不預先授權任意改碼。未取得硬體與 live readiness 前不得派工或標 Done。",
    "completion_profiles.layer_a_plus_layer_b_exact_sha.applies_to 改為 [W11-011,W11-012,W11-013]；done_requires 全部保留。release_candidate_additions 明定只套用 release_readiness_required=true 的 W11-012；W11-013 的多螢幕範圍由其 AC 約束，不新增或降低 completion profile。",
    "新增 release_scope：id=windows11_single_monitor_mvp；mandatory_tickets=[W11-001,W11-002,W11-003,W11-004,W11-005,W11-006,W11-007,W11-008,W11-009,W11-010,W11-011-PREP,W11-011,W11-012]；deferred_tickets=[W11-013]。W11-012 AC4 改為：本 mandatory set 中除 W11-012 本身外全部 Done，且本票其餘 AC／exact-SHA profile 齊備後才可 Done；MVP 整體完成才要求 mandatory set 全部 Done，避免自我前置循環。",
    "release blocker 僅排除明確延期的多螢幕認證缺證；所有影響當期單螢幕功能、安全、cleanup 或共用 regression 的未解 Repair Ticket 仍阻擋 release，即使發現於 W11-013。不得以延期票名義隱藏當期產品缺陷。",
    "保留所有既有 approval、zero-bypass protected queue、same-SHA、publisher 授權邊界與 Luna/Astra executable routing。W11-011／012 既有 retry 值不變；W11-013 初始化 retry.state=none、consecutive_code_failures=0、total_code_failures=0、repair_ticket=null、last_failed_layer=null、last_failed_run=null。本裁決不觸發 code retry，也不創造 CI run URL。"
  ],
  "human_input_required": false,
  "confidence": "high"
}
```

## 2026-10-15 恢復

上次新 worker Locke（`01a137d8-835d-74b2-945f-662fd0a4b480`）在讀取規格階段遇到 usage limit，未完成 claim／仲裁指定修正。此次保留全部草稿，以新具名 windows_worker Aristotle（`01a14029-f948-7692-8837-cc0b95d12207`）重新派送；工具角色固定 `gpt-5.6-luna/max`，派工前 validator PASS，等待 Ready/dependency claim。未重新仲裁已由新具名 Astra 核可的相同方案。額度中斷不消耗 code retry。

2026-10-15 controller 唯讀確認本工作樹仍為 `codex/w11-011-offline-prep`、HEAD `b4ae89923f2a55c9e132f584d0ce4cb2e7b50580`、16項既有 dirty/untracked。新修正仍待 worker 與完整測試；不把 routing PASS 或先前 baseline 記成產品測試通過。

## 新具名路由重新核可

舊 Maxwell worker resume 後回報 session model_switch 為 GPT-6，未 claim、未改檔。Controller 因此將路由 fail closed，改以新具名 windows_arbitrator 派送 Nietzsche（agent `01a137d5-8469-7990-92b3-6c532cebf656`）；spawn 工具固定角色 metadata 為 `gpt-6-astra/medium`，且派工前 validator PASS。以下裁決明確重新核可下一節完整 directed repair；不得再恢復身分漂移的舊 worker。

```json
{
  "ticket_id": "W11-011-PREP",
  "decision": "targeted_repair",
  "root_cause": "核可既有 follow-up directed_repair。實際草稿仍混合離線準備與 live activation：caller 自填 SHA 不構成獨立授權，排除舊基底 SHA 不能阻止未來 PREP commit 啟動。固定停用 workflow、CLI 與實際 adapter，並以純函式及注入 fake 驗證契約，足以在原七檔範圍修復。本裁決核可修復方向，不代表現有草稿已符合要求。",
  "evidence": [
    "已唯讀核對指定工作樹的 AGENTS.md、WORKFLOW.md、完整 port plan／manifest、.codex/config.toml、windows-arbitrator.toml、既有仲裁／preflight／offline-prep 完整決策及七個草稿檔。",
    "git --no-optional-locks rev-parse HEAD 與 branch --show-current 分別確認 b4ae89923f2a55c9e132f584d0ce4cb2e7b50580、codex/w11-011-offline-prep；status 回報 must be run in a work tree，不據此判定 repository 毀損或工作樹乾淨。",
    ".github/workflows/windows11-integration.yml:128 的 integration 尚無固定 false，仍有 checkout、任意 runtime 執行及秘密引用；authorized_sha 來自 inputs。",
    "tools/windows11_integration_harness.py:400、467、525、632 保留實際 spawn、lab=None fallback、真實 adapter 預設及 approved 環境字串入口；cwd 使用 lab_root。primary／nested cleanup 診斷仍可帶入原始例外。",
    "tools/windows11_lab.py:174 將結構檢查標為 valid；:374 將空白 listener 輸出當成空集合。fixture executable 仍為 obs/bin/obs64.exe，未採既有決策要求的 obs/bin/64bit/obs64.exe。",
    "test_workflow_contracts.py:65 仍要求舊 concurrency 與授權入口；test_layer_b_harness.py:159 的 fake process 缺 pid，會在進入預期 transform failure 前失敗。這些是靜態發現，本輪未執行測試。",
    "manifest 已記錄 PREP routing blocked、七檔 touch_set、零 code retries；416 passed／3 skipped 僅為既有 baseline。沒有本票 PR／CI／merged-SHA completion evidence。",
    "agent TOML 確認 gpt-6-astra/medium 設定；本次具名 spawn metadata 與 validator PASS 依 controller 本輪提供的證據，未以 TOML 或模型自述替代執行身分證明。"
  ],
  "directed_repair": [
    "完整採納 docs/validation/windows-11-offline-prep-2026-10-14.md「Follow-up 仲裁結果」（第52行起）JSON 的 directed_repair 第1至14項；不得省略固定 false、移除 live steps／秘密／caller 授權輸入、CLI／public live wrapper／spawn adapter 無條件拒絕、pure portable argv/cwd、structural_valid/runtime_verified=false、明確注入 fake、完整 cleanup 及白名單診斷。",
    "測試具體補正：遷移 test_layer_b_harness.py 的 nested-cleanup 案例至注入 seam 時，fake process 必須提供有效 owned PID，並斷言確實到達 transform failure 與各 cleanup 階段，避免以前置 AttributeError 冒充目標失敗覆蓋。",
    "白名單診斷要求亦涵蓋 windows11_lab.py 的 CLI 錯誤與序列化輸出；不得保留原始 str(exc)、任意 fixture 值或巢狀 cleanup 原文的旁路。這是既有秘密處理契約的落點澄清，不新增授權框架。",
    "由新具名 windows_worker（gpt-5.6-luna/max）完成原七檔修復及既定測試；不得 resume 身分漂移的舊 worker。本仲裁未實作、再派工、執行 live 路徑或外部寫入。",
    "實際 runtime、ACL、frozen 安裝、runner policy/membership 與可信 exact-SHA 授權留 W11-011。單螢幕不阻擋 PREP；W11-011 多螢幕 mixed-DPI、負座標、跨螢幕 freeze 及 W11-012 release 門檻完整保留。"
  ],
  "manifest_changes": [
    "由 controller 追加本次重新核可紀錄，保留舊仲裁與 worker model drift 歷史；以實際新具名派送證據更新 execution_identity_revalidation，不編造 agent ID 或 metadata。",
    "controller 核對本次仲裁路由並記錄裁決後，可將 PREP 轉 Ready；新 worker 派送身分驗證成功後才進 Doing。若路由驗證失敗，維持 Blocked，不以本裁決越過 routing gate。",
    "沿用上一完整 JSON 的 manifest_changes 第2至8項；維持七檔 worker touch_set、controller 文件範圍、原 DAG 與 completion profile。PREP Done 仍須本票 exact merged SHA 的 protected merge、trusted ci.yml push 及 post-merge.yml Layer A producer 成功。",
    "retry.state=none、consecutive_code_failures=0、total_code_failures=0 保持不變；不新增 publisher registration、硬體／秘密／GitHub 權限，不將 skipped Layer B、fake PASS 或本次核可記為實機認證。"
  ],
  "human_input_required": false,
  "confidence": "high"
}
```

## Follow-up 仲裁結果

具名路由身分沿用本 chat 原始 `windows_arbitrator` 派送證據（工具角色固定 gpt-6-astra/medium），本次 resume 同一 agent；不是僅從設定檔推測。以下保留本次完整決策；原 needs_human 決策是 live setup 的歷史邊界，不是新離線票的禁止令。

```json
{
  "ticket_id": "W11-011-PREP",
  "decision": "targeted_repair",
  "root_cause": "draft 混合了離線準備與可執行的 live activation：caller 自填 authorized_sha 並互相比對，只能證明輸入一致，不能證明授權；排除舊 b4 SHA 也不能禁止未來 PREP merged SHA 啟動。現有 workflow、live CLI 與 lab=None 路徑仍具實際執行能力。修復應收斂為合併後仍硬停用的 PREP，加上以 fake 驗證的最小契約；不需要新增批准、硬體或授權框架。",
  "evidence": [
    "已核對指定工作樹的 AGENTS.md、路由設定、manifest、先前仲裁及 offline-prep 文件，並比對 WORKFLOW／plan 與先前已讀版本的變更。windows-arbitrator.toml 固定 name=windows_arbitrator、model=gpt-6-astra、effort=medium；本回合未取得額外 session spawn metadata，設定檔不能單獨證明執行身分，controller 應沿用其具名派送證據核對。",
    "manifest 的 PREP 為 Blocked、follow_up_state=requested，worker touch_set 恰為七檔；completion 要求本票 exact merged SHA 的 protected merge 與 trusted ci.yml／post-merge.yml Layer A，不要求 Layer B。416 passed、3 skipped 是修改前 baseline，非 draft 驗證結果。",
    "讀取中的 workflow 沒有 integration job 級硬停用條件；authorized_sha、workflow_sha 均來自 inputs，preflight 不再核對獨立 hardware authorization，僅拒絕 b4ae89923f2a55c9e132f584d0ce4cb2e7b50580。",
    "workflow 先執行 inputs.python_path 指向的程式，再驗證 lab fixture；uv 版本以子字串判斷。lab helper 目前僅驗證宣告、路徑及存在性，不能證明 Python3.12、venv 身分或 frozen dependencies。",
    "harness 的 run_live_harness 允許 lab=None，因而可略過 portable 與 mutex；_start_obs 的 portable 預設 false、cwd 使用 lab_root。CLI 仍可藉環境字串 approved 與 GITHUB_ACTIONS=true 進入 live path。",
    "draft 已將 OBS spawn／ObsClient 建構置於 try/finally，這項修正可保留；但 run_transform_harness 仍拼接原始例外，外層 cleanup_error 原文直接輸出，僅清理 primary_error 不足以避免秘密外洩。",
    "現有 test_workflow_contracts.py 仍要求舊 concurrency group 與 W11_010_HARDWARE_AUTHORIZATION，尚未配合 draft 更新；lab tests 使用 placeholder executable，因此只能作結構測試。",
    "Git diff／status 在本仲裁環境仍回報 work-tree 錯誤；上述判斷依實際 UTF-8 檔案內容、文件差異與完整 escalation packet，未宣稱 Git diff 已成功或工作樹乾淨。本回合沒有修改、執行測試或外部 mutation。",
    "使用者提供的 group3 read-back 已證明當時 selected repository／workflow 政策，解除原先未知能力問題；但 @refs/heads/develop 是分支限制，不是某個 certification SHA 的批准，也不證明 runner membership 或目前政策無漂移。",
    "GitHub 支援 job 級 if 阻止 job 執行；skipped job 不代表實機驗證成功。[GitHub job conditions](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-jobs-with-conditions)",
    "OBS 官方要求自動啟動時 cwd 指向 obs64.exe 所在目錄。[OBS launch parameters](https://obsproject.com/kb/launch-parameters)"
  ],
  "directed_repair": [
    "workflow 的 integration job 必須加入固定 if: ${{ false }}，不得引用 input、vars、secret、event 或 needs output。保留專用 group／labels 與既有 check 名稱作未來啟用骨架，但將 steps 收斂為停用說明；移除可執行的 runtime／OBS steps、秘密引用、自填 authorized_sha／workflow_sha／python_path／uv_path 與 b4 黑名單。保留無秘密、無任意路徑輸入的 manual／reusable 宣告即可；禁止新增 push、PR、merge_group trigger。",
    "hosted job 只明確回報 PREP_DISABLED／not_certified，不發布 protected_sha 或 approved 類授權輸出。PREP workflow 成功或 integration skipped 一律不能形成 Layer B completion evidence。固定 false 是這份可信 PREP workflow 的停用保證，不宣稱它能隔離其他惡意 workflow。",
    "live CLI 無條件以固定 PREP_LIVE_DISABLED 與非零狀態結束，在讀取密碼、fixture、建立 mutex、探測 socket、執行 runtime 或 spawn 前拒絕。help 可正常顯示；任何環境變數、flag、SHA 或 approved 字串均不可解鎖。公開 run_live_harness 與實際 OBS spawn adapter 同樣預設不可執行，啟用必須經未來 W11-011 的受保護程式變更。",
    "保留 run_transform_harness 的 production controller 測試價值；將 acquisition／cleanup 編排抽成小型內部函式，要求明確注入 process、OBS client、pointer、listener、clock 等有副作用的介面，不提供真實 adapter 預設值。tests 注入 fakes；移除 lab=None 的 live fallback。測試 seam 不得成為 CLI 的 allow-live／test-mode 解鎖入口，不需建立通用執行框架。",
    "portable 契約要求完整 lab fixture 與解析後的專用路徑；純函式產生 --portable、profile、collection 參數及 cwd=executable.parent。範例改為 obs/bin/64bit/obs64.exe，lab_root 與 portable OBS 根目錄、bin 目錄分清。缺 scene／source／zoom、非4455、非127.0.0.1、路徑越界或 portable=false 必須拒絕；以 fake spawn 驗證參數，PREP 不實際啟動。",
    "runtime 本票只驗證 fixture 結構、路徑邊界與觀測結果的純判定，不執行 fixture 指定的程式。輸出應稱 structural_valid，並明列 runtime_verified=false；檔名 python312、宣告 uv_version 或存在 uv.lock 都不算驗證。若保留版本結果判定，須解析確切 Python major/minor=3.12 與 uv version=0.11.8，拒絕子字串近似值。",
    "將實際 Python／bootstrap venv／project venv 身分、完整性、ACL、cache 隔離、針對 certification checkout 的 uv sync --frozen 及安裝結果證據留給 W11-011 啟用前驗證。屆時由已信任的 bootstrap 工具先驗證 canonical paths，再執行核准的 executable；不得先執行未驗證路徑來自證安全。PREP 不實作 installer、ACL 管理或依賴證明系統。",
    "listener 檢查區分成功空集合與查詢失敗。避免指定不存在 LocalPort 導致正常無 listener 被誤判；可先成功列舉 listener 再過濾4455，輸出明確 JSON array。PowerShell error 必須成為非零結果；timeout、空白／malformed 輸出、缺欄位及未知結果均 fail closed。spawn 前要求無 listener，spawn 後要求實際 owned PID 與允許的 loopback 位址；PREP 全以注入資料測試，不改 firewall。",
    "秘密處理採最小白名單診斷：固定 stage／error code／受控 exception type，加上有限數值 transform 證據。移除 str(exc)、repr(exc)、原始 inner cleanup_error 及子程序 stdout/stderr 的串接；不要依賴 regex 掩碼兜底。primary 與 cleanup 分列，兩者同時失敗皆保留；CLI 不輸出 chained traceback。用含無標籤密碼、換行、token、巢狀 cleanup 的 sentinel exceptions 驗證所有輸出與序列化欄位均無原文。",
    "保留從資源取得開始的 finally、baseline restore read-back、disconnect、owned process stop 與互斥釋放；fake tests 覆蓋建構失敗、connect／wait timeout、restore mismatch、cleanup 同時失敗。不得清理非本次 owned process，未取得 baseline 不捏造復原成功。",
    "未來 W11-011 必須另以受信任來源取得 exact-SHA authorization。最小契約是 controller 在確認實際 protected merge S、既有使用者授權與 lab readiness 後，於受管理且 caller 無法改寫的啟用紀錄綁定 repository、ticket、workflow path/revision、S、授權來源及撤銷狀態；可在 activation 階段採經驗證權限的環境設定，PREP 不建立或寫入。dispatch input 僅是請求值；hosted preflight 與 side effect 前重新讀取並比較可信紀錄、event／workflow／checkout SHA 與 protected tip，缺漏或漂移即拒絕。",
    "不可將未來未知 S 寫入包含自身的同一 commit 形成循環；exact-SHA 綁定在 S 產生後由 controller 記錄，既有「好的」作授權來源，不重問 blanket setup approval。group3 的 develop 白名單與即時 membership／policy read-back 是另一道必要檢查，不能取代該紀錄。本票不新增秘密、PAT 使用、runner 或 publisher 授權。",
    "最小驗證為：七檔中的 workflow hard-disable 靜態契約；偽造全部 approved／SHA／環境輸入仍零 live side effect 的 CLI／wrapper 測試；fake lifecycle、portable argv/cwd、fixture、listener、runtime 純判定與秘密 sentinel 測試；再由 worker 跑完整既有 Layer A suite。不可把此仲裁、baseline 或 fake PASS 記為最終測試已通過。",
    "單螢幕不阻擋 PREP 的全部離線交付。未來取得 live activation gates 後，可先記錄單螢幕 deterministic transform、語音／延遲、device／cache 與 cleanup 的部分實機證據；雙螢幕 mixed-DPI、負座標與跨螢幕 freeze 仍標未驗證。保留 W11-011 全部 AC 與 Done 門檻，mock 不替代實機證據，也不要求為 PREP 增購螢幕。"
  ],
  "manifest_changes": [
    "PREP follow-up arbitration 改為 resolved／targeted_repair／human_input_required=false，保留先前 needs_human 為歷史。controller 核對既有具名派送 metadata，記錄本決策後可將 PREP Blocked→Ready，再依原路由交回 worker；本仲裁不自行派工。",
    "維持七檔 worker touch_set 不變；manifest／WORKFLOW／plan／evidence 僅由既有 controller_touch_set 同步本契約。不得藉修復擴張 publisher、runtime provisioning 或 GitHub settings 範圍。",
    "將含糊的 inactive manual/reusable AC 改為『合併後 self-hosted job 固定 false；live CLI／實際 adapter 無環境或參數解鎖入口；僅 pure/fake seams 可測』。明定未來 activation 需要 W11-011 受保護程式變更。",
    "將 PREP 的實際 runtime／授權／ACL readiness 要求拆為離線結構及 fake 行為契約；實際 provisioning、版本／venv／frozen 安裝、ACL 與可信 exact-SHA authorization 驗證列為 W11-011 activation prerequisites，不作 PREP completion blocker。",
    "PREP 仍採 layer_a_post_merge，加上同一新 merged SHA 的 trusted post-merge.yml producer success；controller 管理 completion，不加入 W11-010/root13 registration。Skipped Layer B、structural_valid、mock results 均不是 certification evidence。",
    "W11-011 保持原依賴、全部 AC 與 layer_a_plus_layer_b_exact_sha。readiness 分列 preparation、runner-policy/membership、single-monitor evidence、multi-monitor evidence；單螢幕事實不使 PREP Blocked，多螢幕缺失只阻止相關實機項目與完整認證 Done。",
    "保留 group3 policy 為 user-supplied read-back，維持啟用前 revalidation 與 membership 未驗證狀態；不得轉記成已批准某個 S 或已啟用 runner。",
    "此次為 pre-PR architecture review，沒有 CI failure；retry.state、consecutive_code_failures、total_code_failures 不增加。尚無 PREP PR／CI／merged evidence，不標 Done；既有 current-PC setup approval 持續有效。"
  ],
  "human_input_required": false,
  "confidence": "high"
}
```
