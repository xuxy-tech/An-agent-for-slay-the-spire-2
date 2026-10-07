param(
    [string]$GameRoot = 'D:\SteamLibrary\steamapps\common\Slay the Spire 2'
)

$ErrorActionPreference = 'Stop'
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$GameAssemblyDir = Join-Path $GameRoot 'data_sts2_windows_x86_64'
$ModProject = Join-Path $RepoRoot 'mod\STS2HumanCapture\STS2HumanCapture.csproj'
$ModDll = Join-Path $RepoRoot 'mod\STS2HumanCapture\bin\Release\net9.0\STS2HumanCapture.dll'
$SmokeProject = Join-Path $RepoRoot 'tools\human-capture-patch-smoke\HumanCapturePatchSmoke.csproj'

& dotnet build $ModProject -c Release --nologo
if ($LASTEXITCODE -ne 0) { throw 'Human capture Mod build failed' }
& dotnet build $SmokeProject -c Release --nologo
if ($LASTEXITCODE -ne 0) { throw 'Human capture patch-smoke build failed' }
& dotnet run --project $SmokeProject -c Release --no-restore -- $GameAssemblyDir $ModDll
if ($LASTEXITCODE -ne 0) { throw 'Human capture Harmony patch smoke failed' }
