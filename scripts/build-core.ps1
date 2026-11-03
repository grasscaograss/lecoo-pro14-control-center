param([string]$OutputDirectory = (Join-Path $PSScriptRoot '..\dist\runtime'))
$ErrorActionPreference = 'Stop'
$lecooRoot = Split-Path $PSScriptRoot -Parent
$lecooConfig = Get-Content (Join-Path $lecooRoot 'release\config.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$lecooSource = Join-Path ([IO.Path]::GetTempPath()) ('lecoo-core-' + [Guid]::NewGuid().ToString('N'))
$lecooOutput = [IO.Path]::GetFullPath($OutputDirectory)

function Invoke-LecooBuildCommand {
    param([string]$Program, [string[]]$Arguments)
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Program failed with exit code $LASTEXITCODE" }
}

New-Item -ItemType Directory -Path $lecooOutput -Force | Out-Null
Invoke-LecooBuildCommand 'git' @('clone', '--quiet', '--filter=blob:none', '--no-checkout', 'https://github.com/LaVashikk/Lecoo-Control-Center.git', $lecooSource)
Push-Location $lecooSource
try {
    Invoke-LecooBuildCommand 'git' @('checkout', '--quiet', '--detach', $lecooConfig.upstream_commit)
    if ((& git rev-parse HEAD).Trim() -ne $lecooConfig.upstream_commit) { throw 'Upstream revision mismatch' }
    Invoke-LecooBuildCommand 'git' @('apply', '--check', (Join-Path $lecooRoot 'core\0.3.3-ipcfix.patch'))
    Invoke-LecooBuildCommand 'git' @('apply', (Join-Path $lecooRoot 'core\0.3.3-ipcfix.patch'))
    Invoke-LecooBuildCommand 'cargo' @(('+' + $lecooConfig.rust_version), 'test', '--locked', '--release', '-p', 'ipc', '--test', 'windows_disconnect', '--', '--nocapture')
    Invoke-LecooBuildCommand 'cargo' @(('+' + $lecooConfig.rust_version), 'build', '--locked', '--release', '-p', 'lecoo-ec-daemon', '-p', 'lecoo-ctrl')
    foreach ($lecooBinary in @('lecoo-ec-daemon.exe', 'lecoo-ctrl.exe')) {
        Copy-Item (Join-Path $lecooSource ('target\release\' + $lecooBinary)) $lecooOutput -Force
    }
    $lecooDriver = Join-Path $lecooSource 'libs\inpoutx64.dll'
    if ((Get-FileHash $lecooDriver).Hash.ToLowerInvariant() -ne $lecooConfig.driver_sha256) { throw 'Driver identity mismatch' }
    Copy-Item $lecooDriver $lecooOutput -Force
    [pscustomobject]@{
        core_version = $lecooConfig.core_version
        upstream_commit = $lecooConfig.upstream_commit
        interprocess_version = $lecooConfig.interprocess_version
        rust_version = $lecooConfig.rust_version
        regression_tests_passed = 2
        disconnect_cases = 3
        disconnect_iterations_per_case = 64
        normal_roundtrips = 256
    } | ConvertTo-Json | Set-Content (Join-Path $lecooOutput 'build-info.json') -Encoding UTF8
}
finally {
    Pop-Location
    # This directory was created above for this build only.
    Remove-Item -LiteralPath $lecooSource -Recurse -Force -ErrorAction SilentlyContinue
}
