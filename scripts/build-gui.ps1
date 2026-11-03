param(
    [string]$RuntimeDirectory = (Join-Path $PSScriptRoot '..\dist\runtime'),
    [string]$Python = 'python'
)
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$runtime = [IO.Path]::GetFullPath($RuntimeDirectory)
$stage = Join-Path ([IO.Path]::GetTempPath()) ('lecoo-gui-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $stage | Out-Null
foreach ($name in @('GUI.py', '来酷pro14控制中心.spec', 'logo.png', 'install.bat', 'install-fan-service.ps1', 'repair-service-recovery.bat')) {
    Copy-Item -LiteralPath (Join-Path $root $name) -Destination $stage
}
foreach ($name in @('lecoo-ec-daemon.exe', 'lecoo-ctrl.exe', 'lecoo-fan-service.exe', 'inpoutx64.dll')) {
    Copy-Item -LiteralPath (Join-Path $runtime $name) -Destination $stage
}
Push-Location $stage
try {
    & $Python -m PyInstaller --clean -y '来酷pro14控制中心.spec'
    if ($LASTEXITCODE -ne 0) { throw 'GUI build failed' }
    $gui = Join-Path $stage 'dist\来酷pro14控制中心.exe'
    Copy-Item -LiteralPath $gui -Destination $runtime -Force
    [pscustomobject]@{
        source_sha256 = (Get-FileHash (Join-Path $root 'GUI.py')).Hash.ToLowerInvariant()
        binary_sha256 = (Get-FileHash $gui).Hash.ToLowerInvariant()
        python_version = (& $Python -c 'import platform; print(platform.python_version())')
    } | ConvertTo-Json | Set-Content (Join-Path $runtime 'gui-build-info.json') -Encoding UTF8
}
finally {
    Pop-Location
    Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue
}
