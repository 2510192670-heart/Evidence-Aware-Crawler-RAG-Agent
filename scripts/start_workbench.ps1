param(
    [string]$PolicyFile = '',
    [ValidateRange(1024, 65535)][int]$Port = 8002,
    [switch]$NoBuild
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $projectRoot
try {
    if ($PolicyFile) {
        $env:WDA_TARGET_POLICY_FILE = (Resolve-Path -LiteralPath $PolicyFile).Path
    }
    if (-not $NoBuild) {
        & npm.cmd --prefix frontend run build
        if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed.' }
    }
    Write-Host "Workbench: http://127.0.0.1:$Port/console/"
    Write-Host 'Model credentials can be entered in the local UI; they remain in server memory.'
    & ./.venv/Scripts/python.exe -m uvicorn backend.app.main:app --host 127.0.0.1 --port $Port
    if ($LASTEXITCODE -ne 0) { throw 'Backend exited with an error.' }
}
finally { Pop-Location }
