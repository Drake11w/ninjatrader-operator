<#
Registers (or removes) a Task Scheduler task that runs `sup_watch.py --notify` every 2 minutes, independent of the
supervisor daemon, so a dead daemon or a new severity 2+ incident produces a toast (and a phone push if you set
NT_WATCH_PUSH_URL as a user environment variable, for example an ntfy.sh topic URL).

  powershell -File scripts\install_watch_task.ps1 -Python <path to python.exe>
  powershell -File scripts\install_watch_task.ps1 -Remove
  powershell -File scripts\install_watch_task.ps1 -WhatIf

Runs as the current user, no elevation. Read-only: the watcher never contacts NinjaTrader.
#>
param([switch]$Remove, [switch]$WhatIf, [string]$Python = "python")

$TaskName = "NinjaTrader Supervisor Watch"
$Repo = Split-Path -Parent $PSScriptRoot
$Script = Join-Path $Repo "sup_watch.py"

if ($Remove) {
    if ($WhatIf) { Write-Host "Would remove task '$TaskName'"; exit 0 }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction Stop
    Write-Host "Removed '$TaskName'"
    exit 0
}

$py = (Get-Command $Python -ErrorAction Stop).Source
$action = New-ScheduledTaskAction -Execute $py -Argument "`"$Script`" --notify" -WorkingDirectory $Repo
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 2)
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

if ($WhatIf) {
    Write-Host "Would register '$TaskName': $py `"$Script`" --notify every 2 minutes for $env:USERNAME"
    exit 0
}
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
    -Description "Outside watcher for the NinjaTrader Strategy Supervisor (toast / optional push). Read-only." -Force | Out-Null
Write-Host "Registered '$TaskName' (every 2 minutes)."
