$ErrorActionPreference = "Stop"

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Error "uv is required to run ivy-nim-smoke from an unactivated shell."
    exit 127
}

uv run ivy-nim-smoke @args
exit $LASTEXITCODE
