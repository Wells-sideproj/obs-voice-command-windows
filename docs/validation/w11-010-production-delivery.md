# W11-010 production publication 交付驗證

## 已完成

- [PR #21](https://github.com/Wells-sideproj/obs-voice-command-windows/pull/21) 已由 GitHub zero-bypass merge queue 於 `2026-10-11T14:38:27Z` 合併。
- PR 最終 head：`cb15c1a67a42e5678e93618003bc7b71e710bf83`。
- [合併 commit](https://github.com/Wells-sideproj/obs-voice-command-windows/commit/b4ae89923f2a55c9e132f584d0ce4cb2e7b50580)：`b4ae89923f2a55c9e132f584d0ce4cb2e7b50580`。
- 遠端 tree `86704758e080fcf41345afd3f7e0ff6172c15a35` 等於逐 blob 校驗的本機候選；只交付已批准範圍內 15 個檔案，主要 checkout 的使用者改動未包含。
- Worker 與 controller 分別實跑完整 pytest：**416 passed、3 skipped**。填入真實 PR21 registration 後再跑仍相同；routing 與 diff check PASS。
- [PR exact-head CI](https://github.com/Wells-sideproj/obs-voice-command-windows/actions/runs/38147758797)：四個 required jobs 全 success，GitHub Actions app `15368`，suite `103354980067`。
- 入列前讀回 ruleset `20990581` active、無 bypass actors、strict `required / gate`、SQUASH／ALLGREEN queue；GraphQL schema 支援 `expectedHeadOid`。Queue entry：`MQE_lQDOT8W6Tc8AAAABH0hm7M4ABAZczgM03Hs`。
- [merge-group CI](https://github.com/Wells-sideproj/obs-voice-command-windows/actions/runs/38147934656)：四個 required jobs 全 success，queue tree 等於候選 tree。
- 同一 merged SHA 的 [trusted producer](https://github.com/Wells-sideproj/obs-voice-command-windows/actions/runs/38148005163) 與 [trusted push CI](https://github.com/Wells-sideproj/obs-voice-command-windows/actions/runs/38148005235) 均四項 success。

## 正式發布與隔離驗收完成

[Production finalizer #7](https://github.com/Wells-sideproj/obs-voice-command-windows/actions/runs/38148049383) success。PR13/15 留下 historical PASS；[PR21 正式 PASS 留言](https://github.com/Wells-sideproj/obs-voice-command-windows/pull/21#issuecomment-6110174213) 精確對應 merged SHA、producer38148005163、suite103355631070，completion_eligible=true。

獨立 live simulation 三階段皆成功：

| Case | GitHub run | Issue | 模擬失敗次數 | Observations |
| --- | --- | --- | --- | --- |
| PASS | [38149173213](https://github.com/Wells-sideproj/obs-voice-command-windows/actions/runs/38149173213) | 22 | 0 | pass |
| FAIL | [38149259346](https://github.com/Wells-sideproj/obs-voice-command-windows/actions/runs/38149259346) | 22 | 1 | pass, fail |
| rerun | [38331190028](https://github.com/Wells-sideproj/obs-voice-command-windows/actions/runs/38331190028) | 22 | 1 | pass, fail, rerun |

三個 run 皆 workflow_dispatch、exact head `b4ae89923f2a55c9e132f584d0ce4cb2e7b50580`、production step skipped、created_issue=false。每次 direct GET 與完整分頁 list read-back 同一唯一 issue，正式 PR 留言與歷史 Issue14 皆維持 baseline。完整證據與最初 read-back failure 的仲裁／一次限定 revalidation 記錄見 `w11-010-live-simulation.md`；原 failure 仍保留且根因未確認，並未覆寫為成功。

W11-010 的 pre-merge、exact merged-SHA production、live simulation acceptance 三階段已齊，controller 本機 manifest 更新為 Done。此狀態更新尚未另行提交至 protected develop；遠端產品程式與原 PR21 合併內容不變。

## 下一個獨立 gate

W11-011 已無 W11-010 dependency blocker，但 `windows11_real_hardware_lab` 核准與 readiness 尚未驗證，仍 Blocked；W11-012 仍等待 W11-011。2026-10-13 唯讀 `gh api repos/Wells-sideproj/obs-voice-command-windows/actions/runners` 回傳 `total_count=0`、`runners=[]`；repository 目前沒有可用 self-hosted runner。沒有啟動專用 OBS、自架 runner、麥克風或 Layer B certification。

2026-10-13 controller 更新狀態後重新執行 `.venv/Scripts/python.exe -m pytest tests -q --tb=short`：416 passed、3 skipped（9.04s）；`tools/validate_model_routing.ps1` 與 `git --no-optional-locks diff --check` 均 PASS。Luna Max／Astra Medium 路由不變。本次沒有新增產品程式碼、PR、commit 或 push。

## 不變的安全邊界

Issue #14 是歷史診斷 anchor，不因 production PASS 自動關閉。OBS Layer B、GCM token、CI bypass、repository settings 與共享 runtime 全未動用。

本檔及合併後 manifest 狀態是 controller 本機交接資料，未再推送已入列／已合併的 PR head；遠端交付內容以合併 commit 為準。
