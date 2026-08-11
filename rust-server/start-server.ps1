param(
    [int]$Port = 5050,
    [ValidateSet("mph", "sorted", "preload")]
    [string]$LookupMode = "sorted"
)

$ErrorActionPreference = "Stop"
$serverExe = Join-Path $PSScriptRoot "target\release\yomitan-audio-rs.exe"
$bundleRoot = Join-Path (Split-Path $PSScriptRoot -Parent) "bundle"

if (-not (Test-Path -LiteralPath $serverExe -PathType Leaf)) {
    throw "Release executable is missing. Run 'cargo build --release' first: $serverExe"
}
if (-not (Test-Path -LiteralPath (Join-Path $bundleRoot "manifest.json") -PathType Leaf)) {
    throw "Compiled bundle is missing: $bundleRoot"
}

& $serverExe serve --bundle $bundleRoot --host 127.0.0.1 --port $Port --lookup-mode $LookupMode --asset-mode pack
