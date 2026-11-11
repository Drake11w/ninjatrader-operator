<#
Registers (or removes) a Windows Task Scheduler task that runs `scripts\export_feedback.py` once a day (default 16:30,
after the CT session) to write feedback snapshots for the Lucid research side. Nothing is registered unless you run
this script yourself. The export is read-only: one GET to the add-on to sync fills, then files under NT_FEEDBACK_DIR.

  powershell -File scripts\install_export_task.ps1                  # install, daily 16:30 local time
  powershell -File scripts\install_export_task.ps1 -At 17:00        # another time
  powershell -File scripts\install_export_task.ps1 -Remove          # uninstall
  powershell -File scripts\install_export_task.ps1 -WhatIf          # show what would happen

Runs as the current user at the current user's privilege (no elevation, no stored password). Dry run any time with:
python scripts\export_feedback.py
#>
param([switch]$Remove, [switch]$WhatIf, [string]$Python = "python", [string]$At = "16:30")

$TaskName = "NinjaTrader Feedback Export"
$Repo = Split-Path -Parent $PSScriptRoot
$Script = Join-Path $Repo "scripts\export_feedback.py"

if ($Remove) {
    if ($WhatIf) { Write-Host "Would remove task '$TaskName'"; exit 0 }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction Stop
    Write-Host "Removed '$TaskName'"
    exit 0
}

$py = (Get-Command $Python -ErrorAction Stop).Source
# pythonw.exe: no console window (a console steals focus from a fullscreen game).
$pyw = Join-Path (Split-Path -Parent $py) "pythonw.exe"
if (Test-Path $pyw) { $py = $pyw } else { Write-Warning "pythonw.exe not found next to $py; a console window will flash. Pass -Python <path to pythonw.exe>." }
$action = New-ScheduledTaskAction -Execute $py -Argument "`"$Script`"" -WorkingDirectory $Repo
$trigger = New-ScheduledTaskTrigger -Daily -At $At
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 5) -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

if ($WhatIf) {
    Write-Host "Would register '$TaskName': $py `"$Script`" daily at $At for $env:USERNAME"
    exit 0
}
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
    -Description "Writes read-only feedback snapshots (trades, verdict, readiness) for the Lucid research side." -Force | Out-Null
Write-Host "Registered '$TaskName' (daily at $At). Check with: python scripts\export_feedback.py"
