param(
    [string]$GameRoot = 'D:\SteamLibrary\steamapps\common\Slay the Spire 2'
)

$ErrorActionPreference = 'Stop'
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$ModsDir = Join-Path $GameRoot 'mods'
$Observer = Join-Path $ModsDir 'STS2AIAgent.dll'
$BridgeProject = Join-Path $RepoRoot 'mod\STS2RngBridge\STS2RngBridge.csproj'
$BridgeDll = Join-Path $RepoRoot 'mod\STS2RngBridge\bin\Release\net9.0\STS2RngBridge.dll'
$BridgeManifest = Join-Path $RepoRoot 'mod\STS2RngBridge\STS2RngBridge.json'
$BridgeInstallDir = Join-Path $ModsDir 'STS2RngBridge'
$PatcherProject = Join-Path $RepoRoot 'tools\mod-rng-bridge-patcher\ModRngBridgePatcher.csproj'
$PatcherDll = Join-Path $RepoRoot 'tools\mod-rng-bridge-patcher\bin\Release\net9.0\ModRngBridgePatcher.dll'
$StageDir = Join-Path $RepoRoot 'logs\rng_bridge_build'
$UnpatchedObserver = Join-Path $StageDir 'STS2AIAgent.unpatched.dll'
$LegacyBridge = Join-Path $ModsDir 'STS2RngBridge.dll'

if (-not (Test-Path -LiteralPath $Observer)) {
    throw "Observer Mod not found: $Observer"
}
$gameProcess = Get-Process -Name 'SlayTheSpire2' -ErrorAction SilentlyContinue
if ($gameProcess) {
    throw 'Slay the Spire 2 is running. Close it normally before installing the RNG bridge.'
}
New-Item -ItemType Directory -Force -Path $StageDir | Out-Null
& dotnet build $BridgeProject -c Release
if ($LASTEXITCODE -ne 0) { throw 'RNG bridge build failed' }
& dotnet build $PatcherProject -c Release
if ($LASTEXITCODE -ne 0) { throw 'RNG bridge patcher build failed' }
& dotnet $PatcherDll remove $Observer $UnpatchedObserver
if ($LASTEXITCODE -eq 0) {
    Copy-Item -LiteralPath $UnpatchedObserver -Destination $Observer -Force
} elseif ($LASTEXITCODE -ne 3) {
    throw 'Observer Mod unpatching failed'
}
New-Item -ItemType Directory -Force -Path $BridgeInstallDir | Out-Null
Copy-Item -LiteralPath $BridgeDll -Destination (Join-Path $BridgeInstallDir 'STS2RngBridge.dll') -Force
Copy-Item -LiteralPath $BridgeManifest -Destination (Join-Path $BridgeInstallDir 'STS2RngBridge.json') -Force
if (Test-Path -LiteralPath $LegacyBridge) {
    Remove-Item -LiteralPath $LegacyBridge -Force
}
Get-FileHash -Algorithm SHA256 -LiteralPath $Observer,(Join-Path $BridgeInstallDir 'STS2RngBridge.dll') |
    Select-Object Path,Hash
