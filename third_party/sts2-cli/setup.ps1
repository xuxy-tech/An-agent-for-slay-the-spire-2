param(
    [string]$GameDirectory = "C:\Program Files (x86)\Steam\steamapps\common\Slay the Spire 2"
)

$ErrorActionPreference = "Stop"
$CliRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$LibDirectory = Join-Path $CliRoot "lib"
$PatcherProject = Join-Path $CliRoot "tools\HeadlessPatcher\HeadlessPatcher.csproj"
$HeadlessProject = Join-Path $CliRoot "src\Sts2Headless\Sts2Headless.csproj"
$RequiredDlls = @(
    "sts2.dll",
    "SmartFormat.dll",
    "SmartFormat.ZString.dll",
    "Sentry.dll",
    "Steamworks.NET.dll",
    "MonoMod.Backports.dll",
    "MonoMod.ILHelpers.dll",
    "0Harmony.dll",
    "System.IO.Hashing.dll"
)

if (-not (Get-Command dotnet -ErrorAction SilentlyContinue)) {
    throw ".NET was not found. Install the .NET 9 SDK and reopen PowerShell."
}

$SdkVersion = dotnet --version
if ([int]($SdkVersion.Split('.')[0]) -lt 9) {
    throw ".NET 9 or newer is required; found $SdkVersion."
}

if (-not (Test-Path -LiteralPath $GameDirectory -PathType Container)) {
    throw "Slay the Spire 2 directory not found: $GameDirectory"
}

New-Item -ItemType Directory -Path $LibDirectory -Force | Out-Null
foreach ($DllName in $RequiredDlls) {
    $Source = Join-Path $GameDirectory $DllName
    if (-not (Test-Path -LiteralPath $Source -PathType Leaf)) {
        $Match = Get-ChildItem -LiteralPath $GameDirectory -Recurse -File -Filter $DllName |
            Select-Object -First 1
        if (-not $Match) {
            throw "Required game library was not found: $DllName"
        }
        $Source = $Match.FullName
    }
    Copy-Item -LiteralPath $Source -Destination (Join-Path $LibDirectory $DllName) -Force
}

Copy-Item -LiteralPath (Join-Path $LibDirectory "sts2.dll") `
    -Destination (Join-Path $LibDirectory "sts2.dll.original") -Force

dotnet run --project $PatcherProject -- (Join-Path $LibDirectory "sts2.dll")
if ($LASTEXITCODE -ne 0) {
    throw "Headless IL patch failed with exit code $LASTEXITCODE."
}

dotnet build $HeadlessProject -c Release
if ($LASTEXITCODE -ne 0) {
    throw "Headless runtime build failed with exit code $LASTEXITCODE."
}

Write-Host "Windows headless runtime is ready."
