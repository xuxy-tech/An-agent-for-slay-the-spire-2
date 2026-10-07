param(
    [string]$EnvironmentName = "sts2-agent",
    [string]$GameDirectory = "C:\Program Files (x86)\Steam\steamapps\common\Slay the Spire 2",
    [switch]$RefreshGameRuntime
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$EnvironmentFile = Join-Path $ProjectRoot "environment.yml"
$RuntimeSetup = Join-Path $ProjectRoot "third_party\sts2-cli\setup.ps1"
$GameDll = Join-Path $ProjectRoot "third_party\sts2-cli\lib\sts2.dll"
$HeadlessProject = Join-Path $ProjectRoot "third_party\sts2-cli\src\Sts2Headless\Sts2Headless.csproj"

if (-not (Get-Command conda -ErrorAction SilentlyContinue)) {
    throw "Conda was not found. Install Miniconda or Anaconda and reopen PowerShell."
}

Push-Location $ProjectRoot
try {
    $KnownEnvironments = conda env list --json | ConvertFrom-Json
    $Existing = $KnownEnvironments.envs | Where-Object {
        (Split-Path -Leaf $_) -eq $EnvironmentName
    }

    if ($Existing) {
        Write-Host "Updating Conda environment '$EnvironmentName'..."
        conda env update --name $EnvironmentName --file $EnvironmentFile --prune
    }
    else {
        Write-Host "Creating Conda environment '$EnvironmentName'..."
        conda env create --name $EnvironmentName --file $EnvironmentFile
    }

    if ($LASTEXITCODE -ne 0) {
        throw "Conda environment setup failed with exit code $LASTEXITCODE."
    }

    if (-not (Get-Command dotnet -ErrorAction SilentlyContinue)) {
        throw ".NET was not found. Install the .NET 9 SDK and rerun this script."
    }

    $SdkVersion = dotnet --version
    if ([int]($SdkVersion.Split('.')[0]) -lt 9) {
        throw ".NET 9 or newer is required; found $SdkVersion."
    }

    if ($RefreshGameRuntime -or -not (Test-Path -LiteralPath $GameDll -PathType Leaf)) {
        & $RuntimeSetup -GameDirectory $GameDirectory
    }
    else {
        dotnet build $HeadlessProject -c Release
        if ($LASTEXITCODE -ne 0) {
            throw "Headless runtime build failed with exit code $LASTEXITCODE."
        }
    }

    Write-Host "Environment ready. Run: conda activate $EnvironmentName"
    Write-Host "Then verify the project with: .\scripts\smoke.ps1"
}
finally {
    Pop-Location
}
