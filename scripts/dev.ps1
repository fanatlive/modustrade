param([int]$Port = 8000)
$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
Set-Location -LiteralPath $projectRoot
if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
    throw "Port $Port is already occupied. Stop the existing server or choose -Port 8001."
}
$env:DEBUG = '1'
$env:DEMO_MODE = '1'
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Cannot create the virtual environment.' }
}
& $pythonPath -m pip install --disable-pip-version-check -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw 'Cannot install dependencies.' }
if (-not (Test-Path -LiteralPath '.env')) { Copy-Item -LiteralPath '.env.example' -Destination '.env' }
& $pythonPath manage.py migrate --noinput
if ($LASTEXITCODE -ne 0) { throw 'Database migration failed.' }
& $pythonPath manage.py load_demo
if ($LASTEXITCODE -ne 0) { throw 'Demo import failed.' }
& $pythonPath manage.py collectstatic --noinput
if ($LASTEXITCODE -ne 0) { throw 'Static asset collection failed.' }
New-Item -ItemType Directory -Force -Path '.local' | Out-Null
$workerProcess = Start-Process -FilePath $pythonPath -ArgumentList @('manage.py', 'process_outbox', '--loop') -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput '.local\worker.stdout.log' -RedirectStandardError '.local\worker.stderr.log'
try {
    & $pythonPath manage.py runserver "127.0.0.1:$Port" --noreload
} finally {
    if (-not $workerProcess.HasExited) { & taskkill.exe /PID $workerProcess.Id /T /F | Out-Null }
}
