[CmdletBinding()]
param(
    [ValidateRange(1, 65535)]
    [int]$Port = 5051,

    [ValidateSet('sqlite', 'memory')]
    [string]$LookupMode = 'sqlite'
)

$ErrorActionPreference = 'Stop'
$ankiPython = Join-Path $env:LOCALAPPDATA 'AnkiProgramFiles\.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $ankiPython -PathType Leaf)) {
    throw "Anki's bundled Python was not found at $ankiPython"
}

& $ankiPython (Join-Path $PSScriptRoot 'standalone.py') `
    --root $PSScriptRoot `
    --port $Port `
    --lookup-mode $LookupMode
exit $LASTEXITCODE
