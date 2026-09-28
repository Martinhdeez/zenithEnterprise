param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('ci', 'lint', 'test')]
    [string]$Task
)

$ErrorActionPreference = 'Stop'
$frontendPath = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\frontend')).Path
Push-Location -LiteralPath $frontendPath
try {
    if ($Task -eq 'ci') {
        & npm ci
    } else {
        & npm run $Task
    }
    $result = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $result
