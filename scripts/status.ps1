$ErrorActionPreference = "Continue"

$ProjectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Candidates = @(
    (Join-Path $ProjectRoot "venv\Scripts\python.exe"),
    (Join-Path $ProjectRoot ".tools\notebooklm-cli\Scripts\python.exe")
)

foreach ($Python in $Candidates) {
    if (-not (Test-Path -LiteralPath $Python)) {
        continue
    }

    & $Python -c "import sys; print(sys.executable)" *> $null
    if ($LASTEXITCODE -eq 0) {
        & $Python (Join-Path $ProjectRoot "scripts\status.py")
        exit $LASTEXITCODE
    }
}

Write-Host "No working Python found for status.py."
Write-Host "Tried:"
foreach ($Python in $Candidates) {
    Write-Host "  $Python"
}
exit 1
