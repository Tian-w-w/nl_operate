param([string]$Python = 'python')
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)
& $Python -c "import sys; assert sys.version_info >= (3, 11), 'Python 3.11+ required'"
if ($LASTEXITCODE -ne 0) { throw 'Please pass a Python 3.11+ interpreter via -Python' }
if (-not (Test-Path '.venv\Scripts\python.exe')) { & $Python -m venv .venv }
if ($LASTEXITCODE -ne 0) { throw 'venv initialization failed' }
& .\.venv\Scripts\python.exe -m pip install -r requirements.lock
if ($LASTEXITCODE -ne 0) { throw 'Python installation failed' }
Push-Location frontend
try { npm ci; if ($LASTEXITCODE -ne 0) { throw 'npm ci failed' }; npm run build; if ($LASTEXITCODE -ne 0) { throw 'frontend build failed' } } finally { Pop-Location }
& .\.venv\Scripts\python.exe -m scripts.manage db-migrate
if ($LASTEXITCODE -ne 0) { throw 'migration failed' }
Write-Host 'Bootstrap complete. Run db-seed on an empty DB, then dev. See README.md.'
