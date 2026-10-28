$ErrorActionPreference = "Stop"

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Error "uv is required to run ivy from an unactivated shell."
    exit 127
}

uv run ivy @args
exit $LASTEXITCODE
