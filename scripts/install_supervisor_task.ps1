<#
Registers (or removes) a Windows Task Scheduler task that starts the Strategy Supervisor daemon at logon and
restarts it if it exits. Nothing is registered unless you run this script yourself.

  powershell -File scripts\install_supervisor_task.ps1            # install
  powershell -File scripts\install_supervisor_task.ps1 -Remove    # uninstall
  powershell -File scripts\install_supervisor_task.ps1 -WhatIf    # show what would happen

Runs as the current user, no elevation, no stored password. Check it with: python sup_daemon.py --status
#>
param([switch]$Remove, [switch]$WhatIf, [string]$Python = "python")

$TaskName = "NinjaTrader Strategy Supervisor"
$Repo = Split-Path -Parent $PSScriptRoot
$Script = Join-Path $Repo "sup_daemon.py"

if ($Remove) {
    if ($WhatIf) { Write-Host "Would remove task '$TaskName'"; exit 0 }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction Stop
    Write-Host "Removed '$TaskName'"
    exit 0
}

$py = (Get-Command $Python -ErrorAction Stop).Source
$action = New-ScheduledTaskAction -Execute $py -Argument "`"$Script`"" -WorkingDirectory $Repo
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

if ($WhatIf) {
    Write-Host "Would register '$TaskName': $py `"$Script`" at logon for $env:USERNAME, restart every 1 min on failure"
    exit 0
}
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
    -Description "Read-only NinjaTrader Strategy Supervisor daemon (observe, record, notify)." -Force | Out-Null
Write-Host "Registered '$TaskName'. It starts at your next logon; start it now with: Start-ScheduledTask -TaskName '$TaskName'"
