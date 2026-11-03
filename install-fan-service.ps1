param([string]$SourceDirectory = $PSScriptRoot, [string]$ConfigPath = (Join-Path $env:LOCALAPPDATA 'LecooControlCenter\config.json'))
$ErrorActionPreference = 'Stop'
$name = 'LecooFanControl'
$installed = Join-Path $env:ProgramFiles 'LecooControlCenter'
$source = Join-Path $SourceDirectory 'lecoo-fan-service.exe'
$binary = Join-Path $installed 'lecoo-fan-service.exe'
if (-not (Test-Path -LiteralPath $source)) { throw "Missing fan service: $source" }
if (-not [IO.Path]::IsPathRooted($ConfigPath)) { throw 'ConfigPath must be absolute' }
$existing = Get-Service -Name $name -ErrorAction SilentlyContinue
if ($existing) {
    Stop-Service -Name $name
    (Get-Service $name).WaitForStatus('Stopped', [TimeSpan]::FromSeconds(15))
}
foreach ($process in @(Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -eq $binary })) {
    Stop-Process -Id $process.ProcessId -Force
    Wait-Process -Id $process.ProcessId -Timeout 10 -ErrorAction SilentlyContinue
}
Copy-Item -LiteralPath $source -Destination $binary -Force
$command = '"' + $binary + '" --service --config "' + $ConfigPath + '"'
if ($existing) {
    $result = Invoke-CimMethod -InputObject (Get-CimInstance Win32_Service -Filter "Name='$name'") -MethodName Change -Arguments @{ PathName = $command; StartMode = 'Automatic'; StartName = 'LocalSystem'; ServiceDependencies = [string[]]@('LecooControlDaemon') }
} else {
    $result = Invoke-CimMethod -ClassName Win32_Service -MethodName Create -Arguments @{ Name = $name; DisplayName = 'Lecoo Fan Control'; PathName = $command; StartMode = 'Automatic'; StartName = 'LocalSystem'; ServiceType = [byte]16; ErrorControl = [byte]1; DesktopInteract = $false; ServiceDependencies = [string[]]@('LecooControlDaemon') }
}
if ($result.ReturnValue -ne 0) { throw ('Could not register fan service: ' + $result.ReturnValue) }
& sc.exe description $name 'Applies the saved fan policy without a user login or GUI process.' | Out-Null
& sc.exe failure $name reset= 86400 actions= restart/5000/restart/10000/restart/30000 | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Could not set fan service recovery' }
& sc.exe failureflag $name 1 | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Could not enable fan service recovery' }
Start-Service $name
(Get-Service $name).WaitForStatus('Running', [TimeSpan]::FromSeconds(15))
Write-Host 'Fan control is now a system service; no login is required.'
