param([int]$Port = 8765)
$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
Set-Location -LiteralPath $projectRoot
if ($env:DB_ENGINE -ne 'postgres' -or $env:POSTGRES_DB -notmatch '_browser$') {
    throw 'Set DB_ENGINE=postgres and POSTGRES_DB to an isolated seeded database ending in _browser.'
}
if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
    throw "Port $Port is already occupied. Stop the previous owned test server first."
}
$env:DEBUG = '1'
$env:DEMO_MODE = '1'
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
New-Item -ItemType Directory -Force -Path '.local' | Out-Null
$serverProcess = Start-Process -FilePath $pythonPath -ArgumentList @('manage.py', 'runserver', "127.0.0.1:$Port", '--noreload') -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput '.local\browser-server.stdout.log' -RedirectStandardError '.local\browser-server.stderr.log'
$checkExit = 1
try {
    $ready = $false
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        if ($serverProcess.HasExited) { throw 'The browser test server exited. Check .local/browser-server.stderr.log.' }
        try {
            $response = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/health/" -UseBasicParsing -TimeoutSec 1
            if ($response.StatusCode -eq 200) { $ready = $true; break }
        } catch { Start-Sleep -Seconds 1 }
    }
    if (-not $ready) { throw 'The browser test server did not become ready.' }
    & $pythonPath scripts/browser_smoke.py --base-url "http://127.0.0.1:$Port"
    $checkExit = $LASTEXITCODE
} finally {
    if (-not $serverProcess.HasExited) {
        # Windows venv launchers create children; terminate only this owned tree.
        & taskkill.exe /PID $serverProcess.Id /T /F | Out-Null
    }
}
exit $checkExit
