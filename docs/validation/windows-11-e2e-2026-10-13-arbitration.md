# W11-011 啟用前安全仲裁

本檔為 controller 保存的具名仲裁摘要，不是 worker 實作或實機驗證結果。

- Route：windows_arbitrator，GPT-6 Astra Medium。
- Agent：Epicurus `01a132da-4844-7120-9200-1ffc3a38ba7c`。
- Decision：needs_human；confidence=high；code retry 未消耗。
- 使用者 dedicated runner／OBS setup 的「好的」已生效；readiness 與新組織權限不能由該核准推定。

## 阻礙

1. 現有 gh 無法查 organization runner-group 政策（403，缺 org runner/group permission 或 admin:org）；無法證明僅 canonical repository 的可信 workflow revision 可排程到此個人主機。
2. Persistent、JIT／ephemeral、隨機 label、workflow if、hosted preflight、environment approval 或空佇列都不能單獨替代平台端排程隔離。不得試探性註冊、猜 runner group ID 或自動擴張憑證。
3. 只有一個2560×1440 display；不能滿足真實雙螢幕 mixed-DPI／負座標 AC4，也不能用 mock／虛擬顯示冒充。
4. 既有 touch_set 只有 workflow 與 evidence，未涵蓋必要的 portable OBS、runtime isolation、lifecycle cleanup 與負向測試。

## 最小後續契約

- 人類需提供 organization runner-policy/entitlement 證據或明確授權最小必要組織權限流程。若平台沒有足夠限制，再另選隔離環境；不自行改 visibility、升級方案、建 VM／OS 帳號。
- Runner 啟用前需可讀回實際 policy：只允許 canonical repo 指定可信 workflow path@revision、排除 PR/fork/其他 workflow，並核對 membership。JIT 只能作生命週期補強。
- OBS 採官方版本／完整性驗證的 portable ZIP 與 repo 外獨立 lab root、明確 --portable/cwd/profile/collection/scene/monitor_capture；不複製日常 OBS profile 或秘密。
- Python3.12 明確路徑、lab-local bootstrap venv 的 uv0.11.8、project .venv/cache、frozen lock；不對共用 Python pip install，不修改持久 PATH。
- fixture 必須明定 scene/source/zoom；啟動前缺漏即失敗，hard-lock127.0.0.1:4455，驗證 listener ownership/exposure；不靜默改 firewall。
- 專用 OBS 密碼只進經驗證的 GitHub environment secret、live step env 與 repo 外限制 ACL 的必要 OBS 設定；管理憑證不得進 runner。Artifact 僅白名單數據，不含個人畫面或秘密。
- 從 OBS 啟動起完整 finally 保護，包括 ObsClient 建構失敗；復原 baseline read-back、disconnect、owned process exit、分列 primary/cleanup failure；本機互斥且 cancel-in-progress=false。
- Certification SHA S 必須是真正 W11-011 protected merge 結果；event/input/checkout/workflow revision、LayerA/LayerB/cleanup 都綁定S；side effect 前再驗證 tip/approval drift。
- deterministic CI 不開 ASR/mic；相同S的人工認證另涵蓋正向phrases各10次、負向≥10句零誤觸、9/10 latency≤1.5s、device/dry-run/model-cache、Ctrl-C/reconnect及真雙螢幕fixture。任何缺項不得Done。
- 不擴張 W11-010 publisher authorization/registration；W11-011 controller 按實際 run 管理狀態。若需自動RepairIssue發布，須另定授權契約並仲裁。

## 允許的離線準備路徑（尚未派工）

可由 controller 建立只依賴 Done W11-010 的準備子票，Layer A post-merge completion，不啟用 runner、下載/安裝 runtime、建 secrets 或啟動 OBS。W11-011 原 profile 與全部 AC 不變，等待準備票及外部 gate；不得 worker claim Blocked W11-011。

範圍限 windows11-integration.yml、windows11_integration_harness.py、test_layer_b_harness.py、test_workflow_contracts.py、一個具名 lab helper、對應 hardware-free tests 與非秘密fixture template；具體新檔路徑派工前由 controller 定名。測試涵蓋授權/SHA/event/drift、fixture/portable/port/runtime boundary、secret redaction、resource acquisition/timeout/cleanup failure。Controller 可同步既有 W11-010 completion ledger 到下一個合規準備 PR，不改PR21、不創造循環依賴。

本回合未建立準備子票／worktree／PR，未派發 worker；未註冊或啟動 runner。父 controller 的 elevated git status 已驗證既有 dirty 內容；仲裁子agent sandbox 的 git失敗並非repository毀損。
