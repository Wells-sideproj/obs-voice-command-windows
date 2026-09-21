# Windows 11 安裝、OBS 設定與操作

這份文件給已經有 Windows 11、Git、Python 3.12+ executable 與 `uv` 的使用者。所有 Python 套件都安裝到專案自己的 `.venv`；命令不修改全域 `PATH`、全域 pip、共用 Python 或其他專案的 runtime。本文是使用與 troubleshooting 指南，不宣稱 W11-011 的真實 Windows 11 + OBS + 麥克風 certification 已完成。

## 1. 取得程式與建立 project-local environment

請在全新的 **PowerShell** 視窗執行。需要 Git 才能執行 clone；`develop` 是 Windows delivery repository 的 canonical branch；不要改用上游 macOS repository 來做 Windows 安裝。

~~~powershell
git clone --branch develop --single-branch https://github.com/Wells-sideproj/obs-voice-command-windows.git
Set-Location .\obs-voice-command-windows

# 使用現有 Python 3.12+；鎖檔不更新，也不自動下載 uv-managed Python
uv sync --frozen --no-python-downloads
~~~

`--frozen` 讓 `uv.lock` 保持不變；`--no-python-downloads` 只禁止 uv 自動下載 Python runtime。這不是 ASR 模型離線開關，也不會替你準備 OBS 或麥克風。

若 `uv` 找不到已存在的 Python 3.12+，可以用已存在的絕對路徑明確指定 interpreter。以下語法只把路徑傳給這次的 `uv sync`，不修改 PATH 或其他共用設定：

~~~powershell
$pythonCommand = Get-Command python.exe -CommandType Application -ErrorAction SilentlyContinue
if ($null -eq $pythonCommand) {
    throw 'No existing Python executable found. Provide an approved Python 3.12+ runtime, then rerun this command.'
}
$existingPython = [System.IO.Path]::GetFullPath($pythonCommand.Source)
& $existingPython --version
& uv sync --frozen --no-python-downloads --python $existingPython
~~~

如果沒有現成的 Python 3.12+ executable，請由使用者或組織核准的流程提供/安裝 runtime，再回到這一步；本專案不會自動下載、替你安裝 global Python、修改 global PATH 或改動共用 runtime。

確認使用的是本專案 interpreter：

~~~powershell
& .\.venv\Scripts\python.exe --version
& .\.venv\Scripts\python.exe -c "import sys; print(sys.executable); print(sys.platform)"
~~~

Windows 應顯示 Python 3.12 以上與 `win32`。若 `.venv` 不存在，先確認 `uv sync` 的錯誤；不要改用全域 `pip install` 或修改 `PATH` 來繞過它。

## 2. 安全建立與驗證設定檔

不要直接覆蓋既有的 `config.toml`，因為其中可能有自己的 OBS source、device 或 password。只在檔案不存在時複製範例：

~~~powershell
if (Test-Path -LiteralPath .\config.toml) {
    Write-Host 'config.toml already exists; leaving it unchanged.'
} else {
    Copy-Item -LiteralPath .\config.example.toml -Destination .\config.toml -ErrorAction Stop
}

& .\.venv\Scripts\python.exe -c "import tomllib; from pathlib import Path; tomllib.loads(Path('config.toml').read_text(encoding='utf-8')); print('config.toml: valid TOML')"
~~~

`config.example.toml` 的 `[obs].password` 是空值，不是可用密碼。真實密碼只放在本機 `config.toml`，不要放入文件、命令輸出、issue、log 或 commit。`[zoom].os_level = 2.0` 是範例檔明確設定的 override；若設定檔省略它，程式的 `--os` 預設倍率是 `1.5`。

重要設定：

~~~toml
[obs]
host = "localhost"
port = 4455
password = ""
scene = ""          # 空值 = 目前 program scene
source = ""         # 空值 = 恰好一個支援 capture source 時才自動選取

[audio]
device = ""        # 空值 = 系統預設；可填 --list-devices 顯示的 exact name
~~~

## 3. OBS 設定

1. 開啟 OBS Studio。
2. 開啟 **工具 → WebSocket 伺服器設定**。英文介面可能顯示 **Tools → WebSocket Server Settings** 或 **Tools → obs-websocket Settings**。
3. 勾選 **啟用 WebSocket server**（Enable WebSocket server）。確認 port 為 `4455`；如果你刻意改了 port，必須同步修改 `[obs].port`。
4. 建議啟用 authentication/password。把 OBS 顯示的密碼只填入本機 `config.toml` 的 `[obs].password`。
5. 在要控制的 scene 的 **Sources／來源** 面板，使用 **Add → Display Capture／顯示器擷取** 新增 source，並選取要追蹤的 Windows 顯示器。
6. 如果 Sources 中有多個支援 capture source，請把其完整名稱逐字填到 `[obs].source`。例如 source 的名稱確實是 `Display Capture` 時才寫：

~~~toml
source = "Display Capture"
~~~

不要用解析度、排列位置或 source 的大概名稱替代 exact match。

OBS Studio 28 起已內建 WebSocket；5.x 的預設 port 是 `4455`。文件底部列有 OBS 與 obs-websocket 官方說明連結。

### Transform 啟動契約

程式在啟動時讀取選定 scene item 的 transform，只有完整 canvas capture 才會繼續。請在 OBS 選取 source，開啟 **Transform → Edit Transform**，確認實際讀值符合：

- Alignment 是左上（left/top）；程式的 zoom math 以左上原點計算。
- Position X/Y 都是 `0`，即 `(0, 0)`。
- Rotation 是 `0`。
- Crop Left/Right/Top/Bottom 都是 `0`。
- Scale X 與 Scale Y 相同且為正值（uniform positive scale）。
- `boundsType` 是 `none`，`boundsWidth`、`boundsHeight` 都是 `0`，`boundsAlignment` 是 `0`。
- `sourceWidth × scaleX` 與 `sourceHeight × scaleY` 必須分別精確等於 OBS base canvas 的寬、高；不得有 letterboxing。

**Fit to Screen 不是保證。** 它可能留下不相同的 aspect ratio、舊 bounds、置中 alignment 或不符合 canvas 的 geometry；若程式回報 `unsupported transform`，請按上面條件重新檢查，不要靠猜解析度修正。

## 4. Windows 麥克風權限與操作命令

在 Windows 11 開啟 **Settings → Privacy & security → Microphone**：

1. 開啟 **Microphone access**。
2. 開啟 **Let apps access your microphone**。
3. 開啟 **Let desktop apps access your microphone**。

這個程式是從 PowerShell 啟動的 desktop app；Microsoft 的官方說明連結在文件底部。

先確認 CLI 旗標：

~~~powershell
& .\.venv\Scripts\obs-voice-command.exe --help
~~~

查詢裝置並退出：

~~~powershell
& .\.venv\Scripts\obs-voice-command.exe --list-devices
~~~

這個路徑只查詢 audio devices，不載入 ASR、不建立 OBS、不連 WebSocket。把要用的裝置名稱逐字填入 `[audio].device`；留空則使用系統預設裝置。

先跑音訊與辨識路徑、但不連 OBS：

~~~powershell
& .\.venv\Scripts\obs-voice-command.exe --config .\config.toml --dry-run
~~~

`--dry-run` 只略過 OBS client 的建立與連線，仍會載入 ASR model/recognizer、讀取 pointer/display，並建立真實麥克風 stream；它不是無硬體、無下載的檢查。輸出 transform 但不送到 OBS。

正式執行：

~~~powershell
& .\.venv\Scripts\obs-voice-command.exe --config .\config.toml
~~~

程式在前景執行。正常停止請在同一個 PowerShell 視窗按 `Ctrl+C`，讓 OBS baseline restore 完成；本程式沒有獨立的 `stop` flag。若需要查錯，先保存不含 password 的錯誤文字，勿貼出 `config.toml`。

## 5. ASR 首次下載、cache 與離線執行

ASR 模型由正常啟動或 `--dry-run` 的 startup path 準備：

- cache 根目錄是 `Path.home()/.cache/obs-voice-command`；Windows 通常是 `C:\Users\<你的使用者>\.cache\obs-voice-command`。
- 若抽出的模型目錄內有 `tokens.txt`，目前程式就把它當作 cache sentinel，跳過下載。
- 第一次沒有 sentinel 時，程式會下載 archive、保留 archive，並解壓到同一個 cache 根目錄。這需要網路，而且同時需要下載檔與解壓後模型的磁碟空間；不要只按約 488 MB 的 archive 估算可用空間。
- 程式目前沒有 checksum、簽章或完整檔案清單驗證；`tokens.txt` 存在不等於模型已被下載驗證或所有 ONNX 檔都完整。不要在文件或 log 中宣稱 cache 已驗證。
- archive 與抽出的模型都保留，因此離線後續執行的前提是：cache sentinel 與 ASR 所需檔案已經由使用者先準備好。若 sentinel 不存在，正常 startup 會嘗試從網路下載；本 CLI 沒有獨立的 model-offline flag。
- 完全離線執行還需要 project-local `.venv` 的 dependencies 已經在可連網時完成 `uv sync --frozen --no-python-downloads`；只有 ASR cache 而沒有已同步的套件，不能完成離線首次啟動。若套件 wheel 不在 uv cache，離線的 `uv sync` 也不會憑空準備它們。

查看 cache（只讀）：

~~~powershell
$modelCache = [System.IO.Path]::Combine(
    [Environment]::GetFolderPath('UserProfile'),
    '.cache',
    'obs-voice-command'
)
Get-ChildItem -LiteralPath $modelCache -Force -ErrorAction SilentlyContinue
~~~

這個檢查不會下載或修改模型。若磁碟不足，先停止程式並保留可回復的 cache；不要在未確認目標的情況下刪除整個使用者 cache。

本文件中的 config-copy/TOML smoke evidence 見 [`config-copy-smoke.md`](config-copy-smoke.md)。

## 6. 支援邊界與跨平台行為

- Windows OBS mode 與 macOS OBS mode 都使用同一組 `zoom_in`/`zoom_out` commands；平台差異在 pointer/display 與 OBS backend。
- `--os` 是 macOS-only Accessibility Zoom。Windows 上會在載入 model、ASR、audio、pointer/display 或 OBS 之前拒絕，不能用它來取代 Windows OBS mode。
- Windows 不需要、也不應安裝 Quartz。`pyobjc-framework-Quartz` 是 macOS-only dependency marker；Windows 若出現 Quartz import error，通常代表使用了錯的 interpreter 或 environment。
- 多螢幕 mapping 依 OBS capture 的 stable monitor identity 與 Windows `szDevice` alias；不以相同解析度猜測顯示器。游標在非選中 capture monitor 時，zoom center 會保持不變。
- 不支援 crop、rotation、bounds、letterboxing、非 uniform scale 或不能精確填滿 canvas 的 source transform。這些是安全拒絕，不是建議用另一個 source 名稱或解析度猜測繞過。

## 7. Troubleshooting

### Python 找不到或用了錯的 environment

在 `.venv` 尚未建立時，不要先執行 `.\.venv\Scripts\python.exe`；先確認有可用的 Python 3.12+，再讓 uv 建立 project-local environment：

~~~powershell
$pythonCommand = Get-Command python.exe -CommandType Application -ErrorAction SilentlyContinue
if ($null -eq $pythonCommand) {
    throw 'Python 3.12+ is not available. Provide an approved runtime; this project does not install one or edit global PATH.'
}
$existingPython = [System.IO.Path]::GetFullPath($pythonCommand.Source)
& $existingPython --version
& uv sync --frozen --no-python-downloads --python $existingPython
& .\.venv\Scripts\python.exe -c "import sys; print(sys.executable); print(sys.platform)"
& .\.venv\Scripts\obs-voice-command.exe --help
~~~

若 `--version` 顯示低於 3.12、Windows Store alias，或 `Get-Command` 找不到 executable，請停止並使用使用者/組織核准的 Python 3.12+ 安裝流程；不要使用全域 `pip`、不要修改 PATH，也不要讓 uv 下載另一個 Python。若錯誤提到 Quartz，確認 `sys.platform` 是 `win32` 與 `sys.executable` 是 project-local 路徑。

### Quartz / macOS-only dependency

Windows backend 使用 Win32 pointer/display API；Quartz 只在 Darwin import。不要在 Windows 補裝 Quartz，也不要改 production dependency marker。用本文件的 project-local `uv sync` 與 `.venv` executable 重試。

### PortAudio、裝置或麥克風 permission

1. 先執行 `--list-devices`，把有效的 input device 名稱精確填入 `[audio].device`；也可先留空使用預設裝置。
2. 重新確認 **Settings → Privacy & security → Microphone** 的三個 desktop/microphone toggles。
3. 關閉同時獨占麥克風的程式後再試。`--dry-run` 會真的建立 16 kHz、mono、float32 input stream，所以它會暴露 PortAudio 或權限問題。

### OBS WebSocket 連不上

確認 OBS 正在執行，且 **工具 → WebSocket 伺服器設定** 已啟用 server；核對 `[obs].host`、`[obs].port`（預設 `4455`）與本機 password。不要把 password 貼到錯誤報告。OBS 28+ 不需要另外下載 obs-websocket；若使用舊版 OBS，請先升級到本專案文件支援的 28+。

### Source 找不到或多個 source

- `source = ""` 只有在 scene 中恰好一個支援 `monitor_capture`、`display_capture` 或 `screen_capture` 的 capture source 時才會自動選取。
- 有多個候選時，將 `[obs].source` 設為 OBS Sources 面板的 exact name；不要填 input kind、解析度或 display 名稱來代替 source name。
- 若設定了 exact name 仍失敗，確認它位於所選 scene（`scene = ""` 代表目前 program scene），且 input kind 是上述支援類型。

### Monitor mapping 失敗

Windows `monitor_capture` 會從 OBS 的 `GetInputSettings` 取得 `monitor_id`，再用 stable display ID 或 `szDevice` alias 對應 Windows display。遇到 mapping error 時：

1. 確認 OBS source 的 Display Capture 真的選到要控制的 monitor。
2. 確認 `[obs].source` 指向該 source 的 exact name。
3. 重新啟動 OBS 與程式後再讀一次現有 display identity。
4. 不要用解析度、座標或 primary monitor 猜測替代 stable identity；程式會在無法唯一對應時 fail fast。

### `unsupported transform`、letterboxing 或 geometry 錯誤

回到 **Transform → Edit Transform**，逐項確認 position `(0,0)`、left/top alignment、rotation/crop 全為 0、bounds disabled 且 bounds 寬高為 0、bounds alignment 為 0、X/Y uniform positive scale，並確認 source geometry 精確等於 canvas。比例不合時，調整 OBS base canvas 或 source 的真實比例；不要以 Fit-to-Screen 當成修正完成的證據。只要有 letterboxing、殘留 bounds、非零 crop/rotation、非 uniform scale 或 source 沒有填滿 canvas，程式會拒絕啟動。

## 官方來源與上游

- [OBS Remote Control Guide](https://obsproject.com/kb/remote-control-guide) — OBS Studio 28 起內建 WebSocket、設定位置與 password 建議。
- [obs-websocket: Notable changes between 4.x and 5.x](https://github.com/obsproject/obs-websocket/wiki/Notable-changes-between-4.x-and-5.x) — 5.x 預設 port `4455` 與 authentication 說明。
- [Microsoft: Turn on app permissions for your microphone in Windows](https://support.microsoft.com/en-us/windows/privacy/turn-on-app-permissions-for-your-microphone-in-windows) — Windows 11 的 Microphone access、app access 與 desktop app access 路徑。
- [uv CLI reference](https://docs.astral.sh/uv/reference/cli/) — `uv sync` 與 `--no-python-downloads` 的 CLI 說明；實際安裝命令以本機 `uv sync --help` 為準。
- [原始上游專案](https://github.com/htlin222/obs-voice-command) — macOS 原始流程與作者 credit。
