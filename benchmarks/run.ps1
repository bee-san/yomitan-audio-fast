param(
    [ValidateSet('smoke', 'standard', 'full')]
    [string]$Profile = 'standard',
    [string]$StartupSpec = '',
    [string[]]$ExtraArgs = @()
)

$ErrorActionPreference = 'Stop'
$python = Join-Path $env:LOCALAPPDATA 'AnkiProgramFiles\.venv\Scripts\python.exe'
$script = Join-Path $PSScriptRoot 'benchmark.py'

if (-not (Test-Path -LiteralPath $python)) {
    throw "Anki Python not found: $python"
}

$arguments = @($script, '--profile', $Profile)
if ($StartupSpec) {
    $arguments += @('--startup-spec', $StartupSpec)
}
$arguments += $ExtraArgs

& $python @arguments
exit $LASTEXITCODE
