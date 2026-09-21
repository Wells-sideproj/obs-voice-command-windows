[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-%3E%3D3.12-3776AB.svg?logo=python&logoColor=white)](https://www.python.org)
[![Upstream GitHub stars](https://img.shields.io/github/stars/htlin222/obs-voice-command)](https://github.com/htlin222/obs-voice-command/stargazers)

# obs-voice-command

用本機語音指令控制 OBS 畫面縮放：說「來個特寫」時，畫面平滑 zoom 到滑鼠位置並持續追蹤；說「退回全畫面」時緩動回全畫面。語音辨識在本機執行，不需訓練錄音樣本。

這個 Windows 版本保留原始 macOS 專案的可用流程，並加入 Windows 11 的 OBS capture 與顯示器對應。Windows 交付 repository 是 [`Wells-sideproj/obs-voice-command-windows`](https://github.com/Wells-sideproj/obs-voice-command-windows)；原始上游專案與歷史說明仍保留在 [`htlin222/obs-voice-command`](https://github.com/htlin222/obs-voice-command)。本文件描述安裝與操作，不是 W11-011 真實 Windows 11、OBS 或麥克風 certification evidence。

## 需求

- Windows 11 path：Windows 11、Git、已有 Python 3.12+ executable、已有 `uv`、OBS Studio 28+。
- macOS path：macOS 10.14+、Git、Python 3.12+、已有 `uv`、OBS Studio 28+。
- 兩個平台都使用 project-local `.venv`；不要以全域 Python、pip 或 PATH 取代它。

## 支援矩陣

| 執行方式 | 支援內容 | 指令 action |
| --- | --- | --- |
| Windows 11 + OBS | 透過 OBS WebSocket 修改選定 capture source 的 transform | `zoom_in`、`zoom_out` |
| macOS + OBS | 原有 OBS transform 流程 | `zoom_in`、`zoom_out` |
| macOS-only `--os` | 透過 macOS Accessibility Zoom 做整個螢幕縮放，不修改 OBS transform | 仍使用 `zoom_in`、`zoom_out` |
| Windows + `--os` | 不支援；會在模型、音訊、pointer 或 OBS side effect 前結束 | — |

`--os` 是輸出模式，不會新增 `os_zoom_in` 或 `os_zoom_out` 設定 action。`config.toml` 的 `[[commands]].action` 只接受 `zoom_in` 和 `zoom_out`；舊文件中的 `os_zoom_*` 寫法已失效。

## Windows 11 快速開始

前提是已有 Git、Python 3.12+ 與 `uv`。以下命令使用專案自己的 `.venv`，不修改全域 `PATH`、Python 或 pip 設定，也不讓 uv 自動下載 Python runtime：

```powershell
git clone --branch develop --single-branch https://github.com/Wells-sideproj/obs-voice-command-windows.git
Set-Location .\obs-voice-command-windows
uv sync --frozen --no-python-downloads

if (Test-Path -LiteralPath .\config.toml) {
    Write-Host 'config.toml already exists; leaving it unchanged.'
} else {
    Copy-Item -LiteralPath .\config.example.toml -Destination .\config.toml -ErrorAction Stop
}

& .\.venv\Scripts\python.exe -c "import tomllib; from pathlib import Path; tomllib.loads(Path('config.toml').read_text(encoding='utf-8')); print('config.toml: valid TOML')"
& .\.venv\Scripts\obs-voice-command.exe --help
```

完成 OBS 設定與 Windows 麥克風權限後，在同一個 PowerShell 目錄執行：

```powershell
# 列出可用麥克風；只查詢裝置，不載入 ASR、不連 OBS
& .\.venv\Scripts\obs-voice-command.exe --list-devices

# dry-run：不建立/連線 OBS，但仍會載入 ASR、讀取 pointer/display、開啟麥克風
& .\.venv\Scripts\obs-voice-command.exe --config .\config.toml --dry-run

# 正式啟動
& .\.venv\Scripts\obs-voice-command.exe --config .\config.toml
```

正式程序在前景執行；停止時回到同一個 PowerShell 視窗按 `Ctrl+C`，讓程式走正常 restore path。沒有獨立的 `stop` CLI；日常停止不應以強制終止程序取代 `Ctrl+C`。

完整的 Windows OBS、權限、cache、source 選擇與疑難排解請見 [`docs/windows/setup.md`](docs/windows/setup.md)。

## OBS 需求與設定摘要

- OBS Studio 28+ 已內建 WebSocket；5.x 預設 port 是 `4455`。在 OBS 開啟 **工具 → WebSocket 伺服器設定**（英文 UI 可能顯示 **WebSocket Server Settings** 或 **obs-websocket Settings**），啟用 server、確認 port，並把密碼放到本機 `config.toml` 的 `[obs].password`。不要把真實密碼放進 README、範例、log 或 commit。
- 在要控制的 scene 新增 **Display Capture／顯示器擷取** source。`[obs].source` 留空只會在恰好一個支援的 capture source 時自動選取；有多個 source 時，請填 Sources 面板中的完整、大小寫相符名稱。
- Windows source 必須能以穩定 monitor identity 對應到實際顯示器；程式不會以解析度猜測顯示器。選定 source 後若 mapping 失敗，依錯誤中的 source/kind/可用 display ID 修正 OBS source 與 `[obs].source`。
- 程式啟動時會拒絕不符合 zoom math 的 transform：左上對齊、position `(0, 0)`、`boundsType=none`、`boundsWidth=0`、`boundsHeight=0`、`boundsAlignment=0`、crop 四邊為 0、rotation 為 0、scale X/Y 為相同且正值，並且 source 的實際 geometry 必須精確填滿 OBS canvas。Fit-to-Screen 單獨使用不保證通過；比例不合、殘留 bounds 或 letterboxing 都必須先修正。

## macOS 使用方式

原有 macOS OBS 流程仍可使用：

```bash
uv sync --frozen
if [ -e config.toml ]; then echo "config.toml already exists; leaving it unchanged."; else cp config.example.toml config.toml; fi
uv run obs-voice-command
uv run obs-voice-command --dry-run
uv run obs-voice-command --list-devices
```

若要用真正的 macOS 螢幕縮放（不是 OBS transform），需先在系統輔助使用中允許終端機，並開啟「使用鍵盤快速鍵來縮放」，再執行：

```bash
uv run obs-voice-command --os
```

`--os` 仍使用設定中的 `zoom_in`/`zoom_out` phrases；`[zoom].os_level` 只影響此 macOS-only 模式。`config.example.toml` 內的 `os_level = 2.0` 是範例檔明確寫入的 override；若設定檔省略這個欄位，程式預設值是 `1.5`。

## 自訂指令

編輯 `config.toml` 的 `[[commands]]`：

```toml
[[commands]]
phrases = ["來個特寫", "放大一點"]
action = "zoom_in"

[[commands]]
phrases = ["退回全畫面", "拉遠"]
action = "zoom_out"
```

`phrases` 是關鍵詞列表；辨識時會忽略聲調，同音字也可能相符。`[obs].scene = ""` 使用目前 program scene；`[obs].source` 若非空，必須是 OBS scene 中支援 capture source 的 exact name。

## 開發

```bash
uv run pytest
```

模組一覽：

- **config.py** — 設定檔解析、預設值管理
- **matcher.py** — 語音辨識結果與指令 phrase 的比對
- **zoom.py** — 縮放狀態、倍率與 transform 計算
- **mouse.py** — 平台 pointer/display facade
- **asr.py** — sherpa-onnx ASR 驅動與模型 cache
- **obs_client.py** — OBS WebSocket 通訊、source/monitor mapping 與 transform contract
- **runtime.py** — 語音→指令→OBS lifecycle

## Citation 與上游致謝

本專案源自 [htlin222/obs-voice-command](https://github.com/htlin222/obs-voice-command)，若使用本專案請保留原作者致謝：

```bibtex
@software{lin2026obsvoicecommand,
  author = {Lin, Hsieh-Ting},
  title = {obs-voice-command: Voice-commanded zoom-to-mouse for OBS on macOS},
  year = {2026},
  url = {https://github.com/htlin222/obs-voice-command},
  version = {0.1.0}
}
```

Lin HT. *obs-voice-command: Voice-commanded zoom-to-mouse for OBS on macOS*. Published online 2026. https://github.com/htlin222/obs-voice-command

Lin, H.-T. (2026). *obs-voice-command: Voice-commanded zoom-to-mouse for OBS on macOS* (Version 0.1.0) [Computer software]. https://github.com/htlin222/obs-voice-command

## License

This project is licensed under the [MIT License](LICENSE).
