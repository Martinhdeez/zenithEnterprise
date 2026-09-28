# Windows equivalent of check-licences.sh: inspect only shipped dependencies.
$ErrorActionPreference = 'Stop'
$backendPath = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\backend')).Path
Push-Location -LiteralPath $backendPath
try {
    $exported = & uv export --no-dev --no-hashes --no-emit-project
    if ($LASTEXITCODE -ne 0) { throw "uv export failed with exit $LASTEXITCODE" }
    $shippedPackages = @(
        $exported | Where-Object { $_ -match '^[a-zA-Z0-9]' } |
            ForEach-Object { $_ -replace '[=;\[].*', '' }
    )
    if ($shippedPackages.Count -eq 0) { throw 'Production dependency export is empty' }
    & uv run pip-licenses --packages $shippedPackages --fail-on='GNU General Public License v3 (GPLv3);GNU Affero General Public License v3;GNU Affero General Public License v3 or later (AGPLv3+);GNU General Public License v2 (GPLv2);Other/Proprietary License'
    if ($LASTEXITCODE -ne 0) { throw "pip-licenses failed with exit $LASTEXITCODE" }
} finally {
    Pop-Location
}
