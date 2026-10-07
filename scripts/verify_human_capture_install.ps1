param(
    [string]$GameRoot = 'D:\SteamLibrary\steamapps\common\Slay the Spire 2'
)

$ErrorActionPreference = 'Stop'
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$BuiltDll = Join-Path $RepoRoot 'mod\STS2HumanCapture\bin\Release\net9.0\STS2HumanCapture.dll'
$InstalledDll = Join-Path $GameRoot 'mods\STS2HumanCapture\STS2HumanCapture.dll'

if (-not (Test-Path -LiteralPath $BuiltDll -PathType Leaf)) {
    throw "Workspace Capture Mod build is missing: $BuiltDll"
}
if (-not (Test-Path -LiteralPath $InstalledDll -PathType Leaf)) {
    throw "Installed Capture Mod is missing: $InstalledDll"
}

$BuiltHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $BuiltDll).Hash.ToLowerInvariant()
$InstalledHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $InstalledDll).Hash.ToLowerInvariant()
[pscustomobject]@{
    workspace_dll = $BuiltDll
    installed_dll = $InstalledDll
    workspace_sha256 = $BuiltHash
    installed_sha256 = $InstalledHash
    match = ($BuiltHash -eq $InstalledHash)
} | Format-List

if ($BuiltHash -ne $InstalledHash) {
    throw 'Installed Capture Mod differs from the workspace Release build; install it and restart the game.'
}

Write-Output 'PASS Capture Mod installation matches the workspace Release build.'
