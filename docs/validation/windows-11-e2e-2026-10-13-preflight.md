# W11-011 本機測試環境核准與唯讀 preflight

## 使用者核准

2026-10-13 使用者對「是否使用目前這台電腦，並核准設定專用 runner 與 OBS 測試環境？」回覆「好的」。這解除本機專用 lab setup 的人工核准 gate，不等於所有 readiness 已通過，也不豁免 public-repository runner 隔離、protected SHA、CI 或 W11-011 全部驗收條件。

不包含重新登入或擴張 GitHub 憑證 scope、建立 OS 帳號/VM、修改全域 PATH/Python、日常 OBS profiles、分支保護、麥克風長時間錄音或 release publication。

## 已確認（唯讀）

- Canonical repo 仍 public、default develop；既有 gh 顯示 repository admin/maintain/push 權限。
- protected develop：`b4ae89923f2a55c9e132f584d0ce4cb2e7b50580`，W11-010 三階段完成紀錄見 `w11-010-production-delivery.md`。
- OS：Microsoft Windows 11 Pro，10.0.26200，64-bit；目前 process interactive=true、session11。
- 已安裝 OBS executable：`C:\Program Files\obs-studio\bin\64bit\obs64.exe`，version32.0.4。未啟動它。
- 未見 obs64、Runner.Listener、Runner.Worker process，未見4455 TCP listener。
- Win32 backend 唯讀列出一個2560×1440 display，origin=(0,0)。未確認 mixed-DPI fixture；不記錄完整 device identifier。
- repository runners API：total_count0。Actions enabled、allowed_actions=all；只讀、不改設定。
- org runner-groups API：403，診斷為既有 gh 缺 `admin:org` scope（或相應 organization runner/group 權限）。Repository admin 不等於 organization runner-group capability。
- `tools/validate_model_routing.ps1` PASS：windows_worker=gpt-5.6-luna/max，windows_arbitrator=gpt-6-astra/medium。具名仲裁 Epicurus `01a132da-4844-7120-9200-1ffc3a38ba7c` 已派發。

## 規格／安全缺口

現有 inactive `windows11-integration.yml` 的 shared `python -m pip install uv`、未 provision 的 config.toml、OBS profile 隔離，以及 public runner 的工作排程安全邊界須先確認。Labels 與 workflow 中的 if 不能替代 runner access control；不能在未驗證隔離前將日常使用者帳號 runner 長期掛在 public repo。

GitHub 官方 [runner-group 管理文件](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/manage-access) 提醒 public repo 的 fork workflow 可能執行危險程式碼。該文件方案描述存在 Free/Team 差異；不得據此猜測 selected-workflows 功能可用。實際 organization API 能力仍未取得。

W11-011 AC4 需要至少雙螢幕 mixed-DPI 與負座標 fixture，目前只有單螢幕。這不撤回 setup 核准，但完整 certification 仍不可能宣稱 Done。

## 目前處置

具名 Astra 已回覆 needs_human（high confidence），詳見 `windows-11-e2e-2026-10-13-arbitration.md`。W11-011 保持 Blocked，approval=approved、readiness=blocked 分開記錄，不消耗 code retry。尚未註冊、安裝或啟動 runner，未建立/更改 GitHub secret、OBS profile、OS account、VM 或 workflow。先前完成的 W11-010 不回退。此檔為 controller 本機交接資料，尚未推送。
