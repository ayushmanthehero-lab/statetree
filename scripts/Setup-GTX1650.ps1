<# One-time local setup. No administrator rights or permanent policy changes needed. #>
[CmdletBinding()]
param(
    [string]$Python = 'python',
    [string]$CacheDir = '',
    [string]$ModelPath = '',
    [string]$ServerPath = '',
    [switch]$SkipPythonInstall
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$Root = Split-Path -Parent $PSScriptRoot
$VenvPython = Join-Path $Root '.venv\Scripts\python.exe'
Push-Location $Root
try {
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        throw 'Git for Windows must be installed and available on PATH.'
    }
    if (-not (Test-Path -LiteralPath $VenvPython -PathType Leaf)) {
        & $Python -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) and sys.maxsize > 2**32 else 1)'
        if ($LASTEXITCODE -ne 0) { throw 'Use a working 64-bit Python 3.11 or newer.' }
        & $Python -m venv (Join-Path $Root '.venv')
        if ($LASTEXITCODE -ne 0) { throw 'Virtual environment creation failed; no GPU assets downloaded.' }
    }
    if (-not $SkipPythonInstall) {
        & $VenvPython -m pip install -e .
        if ($LASTEXITCODE -ne 0) { throw 'Installing StateTree and its real Strands dependency failed.' }
    }
    $Arguments = @('-m', 'statetree.local_gpu', 'setup')
    if ($CacheDir) { $Arguments += @('--cache', $CacheDir) }
    if ($ModelPath) { $Arguments += @('--model', $ModelPath) }
    if ($ServerPath) { $Arguments += @('--server', $ServerPath) }
    & $VenvPython @Arguments
    if ($LASTEXITCODE -ne 0) { throw 'GPU asset setup failed. Fix the error above and rerun this script.' }
    Write-Host ''
    Write-Host 'Setup files are ready. Next: powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\Start-GTX1650.ps1'
} finally {
    Pop-Location
}
