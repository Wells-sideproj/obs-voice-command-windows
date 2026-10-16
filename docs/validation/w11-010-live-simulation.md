# W11-010 live simulation 驗收

## 原始失敗與仲裁

- [首次 PASS run38148400151](https://github.com/Wells-sideproj/obs-voice-command-windows/actions/runs/38148400151)，workflow #8/attempt1，精確 merged SHA `b4ae89923f2a55c9e132f584d0ce4cb2e7b50580`，production step skipped。
- `2026-10-11T14:44:34Z`：`python tools/post_merge_publisher.py --simulation-case "$POST_MERGE_SIMULATION_CASE"` → `blocked: simulation publication read-back missing or inconsistent`，exit1。
- 事後 [Issue22](https://github.com/Wells-sideproj/obs-voice-command-windows/issues/22) 為可信 bot、closed、case=pass、count0、observations=[pass]，body 完全相符；這不追認原 run 成功。
- 具名 Astra Medium 仲裁：`w11-010-simulation-readback-arbitration.json`。分類 unresolved_simulation_readback，非 confirmed infrastructure，code retry 不變。

## 一次性 revalidation 額度

完整 finalizer workflow 歷史共8次，全為 attempt1；#1–7 是不同受保護 commit 的自動 workflow_run（包含獨立診斷/cleanup 交付），沒有 re-run；#8 是第一個 manual simulation。production finalizer #7 已完整通過最新 relevant commit；本機 W11-010 retry state none、consecutive/total code failures 皆0，沒有 infrastructure retry dispatch 記錄。

本次 infrastructure allowance limit2、已用0、可用2；保留其中1次供仲裁限定的 same-case revalidation。尚未 dispatch 前不計已使用。不得自動使用第二次。核對來源：完整分頁 `actions/workflows/373741433/runs?per_page=100`、manifest、production run/result 與本機交付紀錄。

## 前置核對與結果

雙次唯讀 preflight 於 `2026-10-11T14:54:28Z`、`14:54:36Z` 通過：唯一 Issue22 direct GET/list 一致、closed、body SHA256 `f936bbcf67fb937050f2e899e901696b5ced6985e8c76dd452eebd4ce7db0c54`、updated_at `14:44:33Z`。code/workflow blob 與 protected SHA 相同、無 active finalizer、production baseline 三則留言及 Issue14 皆未變、無 production repair issue。

一次 [PASS revalidation run38149173213](https://github.com/Wells-sideproj/obs-voice-command-windows/actions/runs/38149173213) 已 success：`created_issue=false`、issue22、casepass/count0/observations[pass]；production step skipped。完成後 direct GET/list 一致，Issue22 timestamp/body/state 未變，production 三則留言與 Issue14 未變，沒有新增 simulation 或 production repair issue。

infrastructure revalidation allowance 已用1、剩1，但仲裁不授權自動使用剩餘次數。原 run38148400151 保持 failure、原始根因仍未確認，不能宣稱已證實 eventual consistency。

[獨立 FAIL run38149259346](https://github.com/Wells-sideproj/obs-voice-command-windows/actions/runs/38149259346) success：同 issue22、created_issue=false、casefail/count1/observations[pass,fail]，production step skipped。direct GET/list 一致、唯一 simulation issue；state=open、updated_at=2026-10-11T14:57:13Z、body SHA256 `87f47a80fefd304ec9bf3625fef00079fbbfa5cf691562f8bb59fb55af5b5798`。production baseline 未變。

2026-10-11 最後 rerun dispatch 在程序啟動前被 approval reviewer 額度限制擋下，未執行、沒有產生 GitHub run，也未消耗 ticket code retry。2026-10-13 恢復後即時驗證 develop/code/workflow、完整 finalizer 歷史、Issue22 direct/list 及 production baseline 均無漂移，再送出 [rerun case38331190028](https://github.com/Wells-sideproj/obs-voice-command-windows/actions/runs/38331190028)。

該 rerun 於 `2026-10-13T01:14:53Z` 完成 success，exact head 仍為 `b4ae89923f2a55c9e132f584d0ce4cb2e7b50580`。Production step skipped、created_issue=false、issue22、case=rerun、count=1、observations=[pass,fail,rerun]、production_ticket_mutated=false。

完成後 direct GET/list 一致且唯一，Issue22 state=open、updated_at=`2026-10-13T01:14:50Z`、body SHA256 `f3388615c531d012cf40dcfc3c466edfc127931a62cdd52a990e2f2bd5c2ffa1`。PR13/15/21 留言 body digest 與 updated_at、Issue14 body/state/labels/comments/updated_at 均等於 `w11-010-production-before-simulation.json`；production repair issue 仍無。Finalizer 無並行執行。

PASS/FAIL/rerun acceptance 已全部通過，重跑不增加模擬 retry count。保留 open 的隔離 Issue22 作驗收投影，不手動關閉／刪除、不視為 production repair。W11-010 三階段證據全部具備，controller 本機 manifest 更新為 Done；原始 read-back failure 的根因仍未確認，依既有仲裁不阻擋本次成功驗收。

此檔是 controller 本機驗收 ledger，不是已推送的 production manifest，也不是真 OBS Layer B 證據。
