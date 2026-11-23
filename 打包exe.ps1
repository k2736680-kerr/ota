# 打单文件 WearableOTA.exe。
# Run: powershell -ExecutionPolicy Bypass -File .\打包exe.ps1
#
# 以前踩过、这里避开的坑：
# - 解压目录用 --runtime-tmpdir "."，不要进 %TEMP%\_MEI*（杀软/策略会报 Could not create temporary directory）
# - 用 --windowed，窗口是 tkinter（和 GlassesTest 一样）。不要用 pywebview/pythonnet，单文件里加载 Python.Runtime.dll 会直接崩
# - 认窗口必须看窗口类名 Tk。错误对话框标题也含「穿戴」，只匹配标题会把崩溃当成成功
# - --noupx。UPX 会把 adb 的 dll / 解释器压坏
# - adb 必须 exe+两个 dll 一起带走，运行时再拷到数据目录。不能放进 runtime\，页面清空会把整个 runtime 删掉
# - 配置、报告是可写的，放 exe 旁的 WearableOTA_data，不烤进只读包里。配置只在缺失时种入，不覆盖改过的 yaml
# - uiautomator2 / adbutils 用 --collect-all；三条 flow 和 uvicorn 协议是函数里 import 的，要单独 hidden-import
# - 试运行用 8766，不要去动用户正在开着的 8765
# - 分发目录只留 exe 和说明。pyi 工作目录、试运行数据、_MEI* 都清掉

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

$Py = $null
if (Test-Path "D:\AutoTestTools\Tools\Python-3.12.10\python.exe") {
  $Py = "D:\AutoTestTools\Tools\Python-3.12.10\python.exe"
} elseif (Test-Path ".\.venv\Scripts\python.exe") {
  $Py = (Resolve-Path ".\.venv\Scripts\python.exe").Path
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
  $Py = "python"
} else {
  throw "python not found"
}

Write-Host "Python: $Py"
& $Py -c "import PyInstaller, fastapi, uvicorn, uiautomator2, adbutils, yaml, PIL"
if ($LASTEXITCODE -ne 0) {
  & $Py -m pip install -q -r requirements.txt pyinstaller pillow
  if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
}

$AdbSrc = "D:\AutoTestTools\Tools\AndroidSdk\platform-tools"
$Dist = Join-Path $Root "_dist"
$Stage = Join-Path $Dist "stage"
$Bundle = Join-Path $Stage "bundled_tools"
$Work = Join-Path $Dist "pyi_work"
New-Item -ItemType Directory -Force -Path $Bundle | Out-Null
if (Test-Path $Work) { Remove-Item $Work -Recurse -Force }
New-Item -ItemType Directory -Force -Path $Work | Out-Null

foreach ($name in @("adb.exe", "AdbWinApi.dll", "AdbWinUsbApi.dll")) {
  $src = Join-Path $AdbSrc $name
  if (-not (Test-Path $src)) { throw "missing $src" }
  Copy-Item $src (Join-Path $Bundle $name) -Force
}

$AddData = @(
  @( $Bundle, "bundled_tools" ),
  @( (Join-Path $Root "web"), "web" ),
  @( (Join-Path $Root "config"), "config" )
)

$Hidden = @(
  "uvicorn.logging",
  "uvicorn.loops",
  "uvicorn.loops.auto",
  "uvicorn.loops.asyncio",
  "uvicorn.protocols.http",
  "uvicorn.protocols.http.auto",
  "uvicorn.protocols.http.h11_impl",
  "uvicorn.protocols.http.httptools_impl",
  "uvicorn.protocols.websockets",
  "uvicorn.protocols.websockets.auto",
  "uvicorn.protocols.websockets.websockets_impl",
  "uvicorn.protocols.websockets.wsproto_impl",
  "uvicorn.lifespan",
  "uvicorn.lifespan.on",
  "uvicorn.lifespan.off",
  "h11",
  "httptools",
  "websockets",
  "wsproto",
  "yaml",
  "PIL",
  "PIL.Image",
  "server.scripts.run_dafit_flow",
  "server.scripts.run_glass_flow",
  "server.scripts.run_ring_flow",
  "desktop.app"
)

$pyiArgs = @(
  "-m", "PyInstaller",
  "--noconfirm",
  "--clean",
  "--onefile",
  "--noupx",
  "--windowed",
  "--runtime-tmpdir", ".",
  "--name", "WearableOTA",
  "--distpath", $Dist,
  "--workpath", $Work,
  "--specpath", $Work,
  "--paths", $Root,
  "--collect-all", "uiautomator2",
  "--collect-all", "adbutils"
)
foreach ($name in $Hidden) {
  $pyiArgs += @("--hidden-import", $name)
}
foreach ($pair in $AddData) {
  $pyiArgs += @("--add-data", ("{0};{1}" -f $pair[0], $pair[1]))
}
$pyiArgs += (Join-Path $Root "start.py")

Write-Host "Running PyInstaller (this may take several minutes)..."
& $Py @pyiArgs
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

$Exe = Join-Path $Dist "WearableOTA.exe"
if (-not (Test-Path $Exe)) { throw "exe missing: $Exe" }

Copy-Item (Join-Path $Root "分发说明.txt") (Join-Path $Dist "分发说明.txt") -Force

Write-Host "Smoke test on 127.0.0.1:8766 (does not touch a running 8765)..."
$smokeLog = Join-Path $Dist "smoke.log"
$smokeErr = Join-Path $Dist "smoke.err"
if (Test-Path $smokeLog) { Remove-Item $smokeLog -Force }
if (Test-Path $smokeErr) { Remove-Item $smokeErr -Force }
$proc = Start-Process -FilePath $Exe -ArgumentList @("--dry-run", "--serve-only", "--port", "8766") `
  -WorkingDirectory $Dist -PassThru -RedirectStandardOutput $smokeLog -RedirectStandardError $smokeErr
$ok = $false
$detail = ""
try {
  for ($i = 0; $i -lt 90; $i++) {
    Start-Sleep -Seconds 1
    if ($proc.HasExited) {
      $detail = "exe exited early code=$($proc.ExitCode)"
      break
    }
    try {
      $page = Invoke-WebRequest -Uri "http://127.0.0.1:8766/" -UseBasicParsing -TimeoutSec 3
      $apps = Invoke-WebRequest -Uri "http://127.0.0.1:8766/api/config/apps" -UseBasicParsing -TimeoutSec 3
      if ($page.StatusCode -eq 200 -and $page.Content -match "穿戴" -and $apps.StatusCode -eq 200 -and $apps.Content -match "watch") {
        $ok = $true
        break
      }
      $detail = "unexpected status page=$($page.StatusCode) apps=$($apps.StatusCode)"
    } catch {
      $detail = $_.Exception.Message
    }
  }
} finally {
  $PSNativeCommandUseErrorActionPreference = $false
  if (-not $proc.HasExited) {
    & taskkill.exe /PID $proc.Id /T /F | Out-Null
    $proc.WaitForExit(15000) | Out-Null
  }
  Start-Sleep -Seconds 1
}

if (-not $ok) {
  Write-Host "SMOKE FAILED: $detail"
  if (Test-Path $smokeLog) { Write-Host "---- stdout ----"; Get-Content $smokeLog -Tail 80 }
  if (Test-Path $smokeErr) { Write-Host "---- stderr ----"; Get-Content $smokeErr -Tail 80 }
  throw "smoke test failed"
}

$adbOut = Join-Path $Dist "WearableOTA_data\runtime_tools\adb.exe"
$cfgOut = Join-Path $Dist "WearableOTA_data\config\global.yaml"
if (-not (Test-Path $adbOut)) { throw "bundled adb was not extracted: $adbOut" }
if (-not (Test-Path $cfgOut)) { throw "config was not seeded: $cfgOut" }
Write-Host "Smoke OK"

Write-Host "Opening tkinter window..."
if (-not ("WinFind" -as [type])) {
  Add-Type -TypeDefinition @"
using System;
using System.Text;
using System.Runtime.InteropServices;
public class WinFind {
  public delegate bool EnumProc(IntPtr hWnd, IntPtr lParam);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc lpEnumFunc, IntPtr lParam);
  [DllImport("user32.dll")] public static extern int GetWindowText(IntPtr hWnd, StringBuilder lpString, int nMaxCount);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetClassName(IntPtr hWnd, StringBuilder lpString, int nMaxCount);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hWnd);
  public static bool HasTk(string title) {
    bool found = false;
    EnumWindows((h, l) => {
      if (!IsWindowVisible(h)) return true;
      var cls = new StringBuilder(256);
      GetClassName(h, cls, cls.Capacity);
      var name = cls.ToString();
      if (name != "Tk" && name != "TkTopLevel") return true;
      var sb = new StringBuilder(512);
      GetWindowText(h, sb, sb.Capacity);
      if (sb.ToString().Contains(title)) found = true;
      return true;
    }, IntPtr.Zero);
    return found;
  }
}
"@
}
$gui = Start-Process -FilePath $Exe -ArgumentList @("--dry-run") -WorkingDirectory $Dist -PassThru
$seen = $false
$guiDetail = ""
try {
  for ($i = 0; $i -lt 20; $i++) {
    Start-Sleep -Seconds 1
    if ($gui.HasExited) {
      $guiDetail = "window process exited early code=$($gui.ExitCode)"
      break
    }
    $titleOk = $false
    try { $titleOk = [WinFind]::HasTk("穿戴 OTA") } catch { $titleOk = $false }
    if ($titleOk) {
      $seen = $true
      break
    }
    $guiDetail = "waiting for Tk window"
  }
} finally {
  $PSNativeCommandUseErrorActionPreference = $false
  if (-not $gui.HasExited) {
    & taskkill.exe /PID $gui.Id /T /F | Out-Null
    $gui.WaitForExit(15000) | Out-Null
  }
  Start-Sleep -Seconds 1
}
if (-not $seen) {
  Write-Host "WINDOW FAILED: $guiDetail"
  throw "desktop window did not appear"
}
Write-Host "Window OK"

Get-ChildItem $Dist -Force | Where-Object {
  $_.Name -notin @("WearableOTA.exe", "分发说明.txt")
} | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

Write-Host "OK: $Exe"
Get-Item $Exe | Format-List FullName, Length, LastWriteTime
Write-Host "分发时只需拷贝: $Exe"
