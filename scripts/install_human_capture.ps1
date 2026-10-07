param(
    [string]$GameRoot = 'D:\SteamLibrary\steamapps\common\Slay the Spire 2'
)

$ErrorActionPreference = 'Stop'
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$Project = Join-Path $RepoRoot 'mod\STS2HumanCapture\STS2HumanCapture.csproj'
$BuiltDll = Join-Path $RepoRoot 'mod\STS2HumanCapture\bin\Release\net9.0\STS2HumanCapture.dll'
$Manifest = Join-Path $RepoRoot 'mod\STS2HumanCapture\STS2HumanCapture.json'
$InstallDir = Join-Path $GameRoot 'mods\STS2HumanCapture'

if (Get-Process -Name 'SlayTheSpire2' -ErrorAction SilentlyContinue) {
    throw 'Slay the Spire 2 is running. Close it normally before installing the capture Mod.'
}
& dotnet build $Project -c Release
if ($LASTEXITCODE -ne 0) { throw 'Human capture Mod build failed' }
$SmokeProject = Join-Path $RepoRoot 'tools/human-capture-patch-smoke/HumanCapturePatchSmoke.csproj'
$GameAssemblyDir = Join-Path $GameRoot 'data_sts2_windows_x86_64'
& dotnet run --project $SmokeProject -c Release -- $GameAssemblyDir $BuiltDll
if ($LASTEXITCODE -ne 0) { throw 'Capture and potion target regressions failed; installation stopped' }
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
Copy-Item -LiteralPath $BuiltDll -Destination (Join-Path $InstallDir 'STS2HumanCapture.dll') -Force
Copy-Item -LiteralPath $Manifest -Destination (Join-Path $InstallDir 'STS2HumanCapture.json') -Force
Get-FileHash -Algorithm SHA256 -LiteralPath (Join-Path $InstallDir 'STS2HumanCapture.dll')
