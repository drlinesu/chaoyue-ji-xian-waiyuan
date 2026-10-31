# 超越次元 - 独立窗口启动器
# 启动内嵌 Python 后端 -> 用 Edge 应用模式打开独立窗口 -> 窗口关闭后自动退出后端
$ErrorActionPreference = "SilentlyContinue"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$port = 8965

function Test-Port($p) {
    try {
        $c = New-Object Net.Sockets.TcpClient
        $c.Connect("127.0.0.1", $p); $c.Close(); return $true
    } catch { return $false }
}

# 1. 启动后端 (若已在运行则复用)
$server = $null
if (-not (Test-Port $port)) {
    $py = Join-Path $root "runtime\python\pythonw.exe"
    if (-not (Test-Path $py)) { $py = "pythonw.exe" }
    $server = Start-Process -FilePath $py -ArgumentList "`"$(Join-Path $root 'server.py')`"" -WorkingDirectory $root -PassThru
    for ($i = 0; $i -lt 80; $i++) {
        if (Test-Port $port) { break }
        Start-Sleep -Milliseconds 400
    }
}

# 2. 应用模式独立窗口
$url = "http://127.0.0.1:$port/"
$browsers = @(
    "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe",
    "${env:ProgramFiles}\Microsoft\Edge\Application\msedge.exe",
    "${env:LOCALAPPDATA}\Microsoft\Edge\Application\msedge.exe",
    "${env:ProgramFiles}\Google\Chrome\Application\chrome.exe",
    "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
    "${env:LOCALAPPDATA}\Google\Chrome\Application\chrome.exe"
)
$browser = $browsers | Where-Object { Test-Path $_ } | Select-Object -First 1
$appProc = $null
if ($browser) {
    $appProc = Start-Process -FilePath $browser -PassThru -ArgumentList @(
        "--app=$url",
        "--user-data-dir=`"$(Join-Path $root '.app-data')`"",
        "--window-size=1380,880",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-session-crashed-bubble",
        "--hide-crash-restore-bubble",
        "--disk-cache-size=268435456",
        "--enable-features=WinFramelessWindowControls,WebFloatingUIVar,WebUIRoundedCorners",
        "--disable-features=msWebOOUI"
    )
} else {
    Start-Process $url   # 无 Edge/Chrome 时退回默认浏览器
}

# 3. 窗口关闭后退出本次启动的后端
if ($appProc) { $appProc.WaitForExit() }
if ($server) { Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue }
