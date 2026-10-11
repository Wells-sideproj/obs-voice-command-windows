# W11-010 production 啟用前主控證據

## 授權與邊界

使用者於 cleanup PR #20 合併後明確回覆「批准」，允許後續獨立 PR 將可信任 finalizer 的 job-level `pull-requests: write` 改為長期權限，恢復 production PR 留言與 Repair Ticket 發布。此批准不包含 OBS Layer B、GCM token 擷取、GitHub settings、CI bypass 或共享 runtime 變更。

完整仲裁見 `w11-010-production-authorization-arbitration.json`。Issue #14 僅為歷史診斷 anchor，不得被 publisher 改寫或關閉。新 PR 編號及執行結果只會在實際取得後記錄，不能預填。

## 本機基線（2026-10-07）

- canonical develop：`0715c59b454dd7ae07919c4fe4c3b1a8381cecde`，由 `gh api` 讀回。
- 隔離分支：`codex/w11-010-production-publication`；開始修改前工作樹乾淨，主要 checkout 原有修改未動。
- 具名仲裁：`windows_arbitrator` / `gpt-6-astra` / `medium`，agent `01a116df-deb7-7262-9de8-67b9d41a97f4`。
- 具名實作：`windows_worker` / `gpt-5.6-luna` / `max`，agent `01a116eb-d458-7f61-8232-b5b561621e8a`。
- `tools/validate_model_routing.ps1`：PASS。
- 使用既有 CPython 3.12.14 執行 `uv sync --frozen --no-python-downloads`，依 `uv.lock` 建立工作樹內 `.venv`；沒有改全域 Python 或 PATH。
- 修改前 `.venv/Scripts/python.exe -m pytest tests -q`：**293 passed, 3 skipped**，10.15 秒。此數字只是基線，非新實作的驗收。

## 遠端安全基線（2026-10-07T15:12Z 讀回）

- ruleset `20990581`：active，`bypass_actors=[]`，required `required / gate` provider `15368`，strict；merge queue SQUASH / ALLGREEN。
- GraphQL `EnqueuePullRequestInput` 支援 `expectedHeadOid`；目前沒有 open develop PR。本紀錄不代表已 enqueue。
- producer workflow：ID `373741435`、path `.github/workflows/post-merge.yml`、active。
- finalizer workflow：ID `373741433`、path `.github/workflows/post-merge-finalize.yml`、active。

### Issue #14 不變性基線

[Issue #14](https://github.com/Wells-sideproj/obs-voice-command-windows/issues/14) 必須在 production PASS、FAIL 與 rerun 後保持原樣。

- id：`5689928254`；node_id：`I_kwDOT8W6Tc8AAAABUyVqPg`。
- author：`Wells-Huang`，user id `34418952`。
- state：`open`；comments：`0`；updated_at：`2026-10-03T13:11:28Z`。
- labels：`type:repair`、`status:ready`。
- API body UTF-8 SHA-256：`69b15487d4118557507ebb8076e7e503806275f6f58d38124697064a8f70597b`。

## 尚待驗證

新實作測試、exact-head GitHub required checks、merge-group、protected merge、新 merged-SHA producer/finalizer production publication read-back、獨立 live PASS/FAIL/rerun simulation 皆尚未完成。W11-010 不得據本 preflight 標為 Done；W11-011/012 尚未解鎖。
