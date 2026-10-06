param(
    [string]$TaskName = "Sports News Aggregator - CFB Refresh",
    [string[]]$Times = @("06:00", "12:00", "18:00", "23:00"),
    [int]$Season = 0,
    # Wake the computer from sleep to run. Off by default: it is your machine's power policy.
    # Without it a run is simply missed while the computer sleeps (StartWhenAvailable then
    # catches up one run when it wakes), which is how a quarter of scheduled runs went missing.
    [switch]$WakeToRun
)

$runner = Join-Path $PSScriptRoot "run_scheduled_refresh.ps1"
$powershell = Join-Path $PSHOME "powershell.exe"
$seasonArgument = if ($Season -gt 0) { " -Season $Season" } else { "" }
$actionArguments = "-NoProfile -ExecutionPolicy Bypass -File `"$runner`"$seasonArgument"
$action = New-ScheduledTaskAction -Execute $powershell -Argument $actionArguments
$triggers = foreach ($time in $Times) {
    New-ScheduledTaskTrigger -Daily -At ([datetime]::ParseExact($time, "HH:mm", $null))
}
# A refresh takes about 30 minutes; three hours is a generous ceiling that still ends a hung run
# (the default of 72 hours would hold the lock and block every later run). A failed launch
# (no network at wake-up, for example) is retried twice, ten minutes apart, and the run
# waits for a network connection instead of starting without one.
$settingsArguments = @{
    StartWhenAvailable         = $true
    MultipleInstances          = "IgnoreNew"
    AllowStartIfOnBatteries    = $true
    DontStopIfGoingOnBatteries = $true
    RunOnlyIfNetworkAvailable  = $true
    ExecutionTimeLimit         = (New-TimeSpan -Hours 3)
    RestartCount               = 2
    RestartInterval            = (New-TimeSpan -Minutes 10)
}
if ($WakeToRun) { $settingsArguments["WakeToRun"] = $true }
$settings = New-ScheduledTaskSettingsSet @settingsArguments
$userId = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers `
    -Settings $settings -Principal $principal `
    -Description "Refresh college-football data, reporting, clusters, and scores." -Force | Out-Null
Write-Output "Registered '$TaskName' for $($Times -join ', ') local time$(if ($WakeToRun) { ' (wakes the computer)' })."
