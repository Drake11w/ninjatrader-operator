<#
Registers (or removes) a Windows Task Scheduler task that runs `launcher.py --apply` every 30 minutes: starts
NinjaTrader and enables the live strategy inside the run window. Nothing is registered unless you run this yourself,
and the launcher still does nothing until "enabled": true is set in ~/.ninjatrader-operator/launcher.json.

  powershell -File scripts\install_launcher_task.ps1            # install
  powershell -File scripts\install_launcher_task.ps1 -Remove    # uninstall
  powershell -File scripts\install_launcher_task.ps1 -WhatIf    # show what would happen

Runs as the current user at the current user's privilege (no elevation, no stored password). It inherits your USER
environment variables, which is where the NT_OPERATOR_* gates live; NinjaTrader inherits them from it.
Dry run any time with: python launcher.py
#>
param([switch]$Remove, [switch]$WhatIf, [string]$Python = "python")

$TaskName = "NinjaTrader Launcher"
$Repo = Split-Path -Parent $PSScriptRoot
$Script = Join-Path $Repo "launcher.py"

if ($Remove) {
    if ($WhatIf) { Write-Host "Would remove task '$TaskName'"; exit 0 }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction Stop
    Write-Host "Removed '$TaskName'"
    exit 0
}

$py = (Get-Command $Python -ErrorAction Stop).Source
# pythonw.exe: no console window (a console steals focus from a fullscreen game).
$pyw = Join-Path (Split-Path -Parent $py) "pythonw.exe"
if (Test-Path $pyw) { $py = $pyw } else { Write-Warning "pythonw.exe not found next to $py; a console window will flash on each run. Pass -Python <path to pythonw.exe>." }
$action = New-ScheduledTaskAction -Execute $py -Argument "`"$Script`" --apply" -WorkingDirectory $Repo
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 30)
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

if ($WhatIf) {
    Write-Host "Would register '$TaskName': $py `"$Script`" --apply every 30 minutes for $env:USERNAME"
    exit 0
}
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
    -Description "Starts NinjaTrader and enables the live-small strategy inside the run window (gated, once per day)." -Force | Out-Null
Write-Host "Registered '$TaskName' (every 30 minutes). Set enabled=true in launcher.json to activate it."
