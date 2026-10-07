param(
    [string]$EnvironmentName = "sts2-agent",
    [switch]$SkipEngine
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$SmokeScript = Join-Path $PSScriptRoot "windows_smoke.py"
$SmokeArgs = @($SmokeScript)

if ($SkipEngine) {
    $SmokeArgs += "--skip-engine"
}

Push-Location $ProjectRoot
try {
    if ($env:CONDA_DEFAULT_ENV -eq $EnvironmentName) {
        python @SmokeArgs
    }
    else {
        if (-not (Get-Command conda -ErrorAction SilentlyContinue)) {
            throw "Conda was not found. Activate '$EnvironmentName' and retry."
        }
        conda run --no-capture-output --name $EnvironmentName python @SmokeArgs
    }

    if ($LASTEXITCODE -ne 0) {
        throw "Smoke test failed with exit code $LASTEXITCODE."
    }
}
finally {
    Pop-Location
}
