param([switch]$Remove)
$ErrorActionPreference = 'Stop'
$taskNames = @('HDU-Library-Daily')
$legacyTaskNames = @('HDU-Library-2000', 'HDU-Library-2002', 'HDU-Library-2003', 'HDU-Library-2110', 'HDU-Library-2115', 'HDU-Library-2120', 'HDU-Library-2140', 'HDU-Library-2145')
if ($Remove) {
    foreach ($taskName in ($taskNames + $legacyTaskNames)) {
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    }
    Write-Output 'HDU booking tasks removed.'
    exit 0
}
if ((Get-TimeZone).Id -ne 'China Standard Time') {
    throw 'Set the Windows time zone to China Standard Time before installing these tasks.'
}
if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot '.venv\Scripts\python.exe'))) {
    throw 'Create .venv and install requirements.txt first.'
}
if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot '.local\credentials.xml'))) {
    throw 'Run Save-Credential.ps1 first.'
}
$windowsUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name
# Interactive logon lets DPAPI decrypt the password without storing the Windows password.
$principal = New-ScheduledTaskPrincipal -UserId $windowsUser -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -WakeToRun -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 20)
$schedule = @(
    @{Name='HDU-Library-Daily'; Prepare='19:55'}
)
foreach ($entry in $schedule) {
    $arguments = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + `
        (Join-Path $PSScriptRoot 'Run-Scheduled.ps1') + '"'
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arguments -WorkingDirectory $PSScriptRoot
    $trigger = New-ScheduledTaskTrigger -Daily -At $entry.Prepare
    $task = New-ScheduledTask -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
        -Description 'HDU fourth floor only: prepare 19:55; up to five rounds 20:00, 20:00:30, 20:01:30, 20:04, 20:10; stop on success or no free seats.'
    Register-ScheduledTask -TaskName $entry.Name -InputObject $task -Force | Out-Null
    Enable-ScheduledTask -TaskName $entry.Name | Out-Null
}
foreach ($taskName in $legacyTaskNames) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
}
Get-ScheduledTask -TaskName $taskNames | Select-Object TaskName,State
Write-Output 'ENABLED: one daily task, prepare 19:55; up to five booking rounds, session reused, all before 20:15.'
Write-Output 'Enable the desired booking weekdays in user_config.yml. Logs are stored in logs/.'
