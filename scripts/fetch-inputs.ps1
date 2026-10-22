$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$downloadRoot = Join-Path $projectRoot 'downloads'
New-Item -ItemType Directory -Path $downloadRoot -Force | Out-Null
$sourceLock = Get-Content -LiteralPath (Join-Path $projectRoot 'sources.lock.json') -Raw -Encoding UTF8 | ConvertFrom-Json
foreach ($source in $sourceLock.sources) {
    if ([IO.Path]::GetFileName($source.filename) -ne $source.filename) {
        throw "Invalid source filename: $($source.filename)"
    }
    $destination = Join-Path $downloadRoot $source.filename
    if (-not (Test-Path -LiteralPath $destination)) {
        $partial = "$destination.partial"
        Write-Host "Downloading $($source.name) $($source.version)"
        Invoke-WebRequest -UseBasicParsing -Uri $source.url -OutFile $partial
        $actualHash = (Get-FileHash -LiteralPath $partial -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actualHash -ne $source.sha256) {
            throw "Downloaded SHA256 mismatch: $($source.filename)"
        }
        Move-Item -LiteralPath $partial -Destination $destination
    }
    $actualHash = (Get-FileHash -LiteralPath $destination -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualHash -ne $source.sha256) {
        throw "Existing source SHA256 mismatch: $($source.filename)"
    }
    Write-Host "Verified $($source.filename)"
}
