[CmdletBinding()]
param(
    [string]$OutputPath
)

$ErrorActionPreference = 'Stop'
if ([string]::IsNullOrWhiteSpace($OutputPath)) {
    $OutputPath = Join-Path (Split-Path $PSScriptRoot -Parent) 'local-audio-fast.ankiaddon'
}
Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem

$rootFiles = @(
    '__init__.py',
    'config.py',
    'consts.py',
    'cleanup.py',
    'db_utils.py',
    'default_config.json',
    'fast_pack.py',
    'fast_store.py',
    'gui.py',
    'import_dialog.py',
    'jp_util.py',
    'manifest.json',
    'migration.py',
    'progress_ui.py',
    'server.py',
    'util.py',
    'version.txt'
)
$relativeFiles = [Collections.Generic.List[string]]::new()
foreach ($name in $rootFiles) {
    $relativeFiles.Add($name)
}
foreach ($directory in @('source')) {
    Get-ChildItem -LiteralPath (Join-Path $PSScriptRoot $directory) -File |
        Where-Object { $_.Extension -eq '.py' } |
        Sort-Object Name |
        ForEach-Object {
            $relativeFiles.Add("$directory/$($_.Name)")
        }
}

$resolvedOutput = [IO.Path]::GetFullPath($OutputPath)
$resolvedRoot = [IO.Path]::GetFullPath($PSScriptRoot)
if ($resolvedOutput.StartsWith($resolvedRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'The package output must be outside the add-on tree.'
}
if (Test-Path -LiteralPath $resolvedOutput) {
    Remove-Item -LiteralPath $resolvedOutput -Force
}
$archive = [IO.Compression.ZipFile]::Open(
    $resolvedOutput,
    [IO.Compression.ZipArchiveMode]::Create
)
try {
    foreach ($relative in $relativeFiles) {
        $source = Join-Path $PSScriptRoot $relative
        if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
            throw "Missing package input: $source"
        }
        $entryName = $relative.Replace('\', '/')
        [IO.Compression.ZipFileExtensions]::CreateEntryFromFile(
            $archive,
            $source,
            $entryName,
            [IO.Compression.CompressionLevel]::Optimal
        ) | Out-Null
    }
} finally {
    $archive.Dispose()
}

$item = Get-Item -LiteralPath $resolvedOutput
$hash = Get-FileHash -LiteralPath $resolvedOutput -Algorithm SHA256
[pscustomobject]@{
    Path = $item.FullName
    Bytes = $item.Length
    Entries = $relativeFiles.Count
    SHA256 = $hash.Hash.ToLowerInvariant()
}
