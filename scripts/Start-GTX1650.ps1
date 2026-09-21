<# Foreground launcher. Ctrl+C stops the workbench and its owned llama-server. #>
[CmdletBinding()]
param(
    [string]$Repo = '',
    [string]$CacheDir = '',
    [string]$ModelPath = '',
    [string]$ServerPath = '',
    [string]$GpuLayers = 'auto',
    [int]$ContextSize = 4096,
    [int]$MaxTokens = 256,
    [int]$ReserveMiB = 768,
    [int]$Port = 8080,
    [int]$WebPort = 8765,
    [int]$Threads = 4,
    [int]$Timeout = 300,
    [string]$Device = '',
    [switch]$DryRun
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$Root = Split-Path -Parent $PSScriptRoot
$VenvPython = Join-Path $Root '.venv\Scripts\python.exe'
if (-not $Repo) { $Repo = $Root }
if (-not (Test-Path -LiteralPath $VenvPython -PathType Leaf)) {
    throw 'Run scripts\Setup-GTX1650.ps1 first, using the same extracted project.'
}
Push-Location $Root
try {
    $Arguments = @('-m', 'statetree.local_gpu', 'start', '--repo', $Repo,
                   '--gpu-layers', $GpuLayers, '--context-size', "$ContextSize", '--max-tokens', "$MaxTokens",
                   '--reserve-mib', "$ReserveMiB", '--port', "$Port", '--web-port', "$WebPort",
                   '--threads', "$Threads", '--timeout', "$Timeout")
    if ($CacheDir) { $Arguments += @('--cache', $CacheDir) }
    if ($ModelPath) { $Arguments += @('--model', $ModelPath) }
    if ($ServerPath) { $Arguments += @('--server', $ServerPath) }
    if ($Device) { $Arguments += @('--device', $Device) }
    if ($DryRun) { $Arguments += '--dry-run' }
    & $VenvPython @Arguments
    if ($LASTEXITCODE -notin @(0,130)) { throw 'StateTree GPU launcher stopped with an error. Check the message and server log above.' }
} finally {
    Pop-Location
}
