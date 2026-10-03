# W11-010 硬體免費驗證

本文件只記錄 worker 在 merge 前可重現的 fake-transport、workflow contract 與 production controller wiring 證據；不宣稱 GitHub post-merge delivery、issue write、exact merged SHA，亦不啟動實際 OBS、麥克風或 ASR model。

Controller 於 2026-10-03 在既有專案 `.venv` 中獨立重跑 `python -m pytest -p no:cacheprovider tests -q`，結果為 **188 passed、3 skipped**；模型路由驗證與 `git diff --check` 均通過。沙箱內既有 pytest 暫存目錄 ACL 拒絕存取，故完整測試在沙箱外用相同虛擬環境執行；未修改或刪除該暫存目錄。此結果僅表示 pre-PR 本地硬體免費驗證通過。

## 驗證範圍

- `post-merge.yml` 的完整 jobs/env parity 與 trusted `push`/`develop`/`contents: read` contract。
- 獨立 finalizer 的 `workflow_run` producer identity、develop-only manual reconciliation、最小權限與不可取消的 repository concurrency。
- Publisher 的 fake GitHub transport：PASS、FAIL、rerun dedup、stale PASS、concurrent dedup、pagination、edited marker、partial API failure、錯誤 workflow/repository/check-provider provenance。
- Finalizer 的 develop-only `simulation_case` entrypoint：固定 `w11-010-simulation` namespace 透過 Issues API projection PASS、FAIL、rerun；rerun read-back 同一 issue 且不增加 simulated failure count，也不碰 production manifest、PR comment、ticket 或 retry state。
- Layer B 的 inactive manual/reusable workflow、hosted exact-SHA preflight、self-hosted label、localhost/port-free safety，以及 production `ZoomController` 的 zoom/restore/finally cleanup。

## 非本階段證據

Exact merged-SHA producer/finalizer run、GitHub Actions check-suite read-back、隔離的 live PASS/FAIL/rerun issue projection，以及 W11-011 的實機 OBS certification，仍由 controller 在 protected `develop` 上依三階段 acceptance contract 執行。W11-010 worker 不執行 live OBS。
