param(
    [string]$PythonPath = ""
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot

if ([string]::IsNullOrWhiteSpace($PythonPath)) {
    $PythonPath = (Get-Command python).Source
}

$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable

$LegacyTasks = @(
    "SZTU Gym Auto Booking",
    "SZTU GymRoom Auto Booking",
    "SZTU Badminton Auto Cancel 19",
    "SZTU Badminton Auto Cancel 2020",
    "SZTU GymRoom Auto Cancel 19",
    "SZTU GymRoom Auto Cancel 2020",
    "SZTU Gym Auto Cancel 19",
    "SZTU Gym Auto Cancel 2020",
    "SZTU FitnessCenter Auto Cancel 19",
    "SZTU FitnessCenter Auto Cancel 2020"
)

foreach ($Name in $LegacyTasks) {
    $Task = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    if ($Task) {
        Unregister-ScheduledTask -TaskName $Name -Confirm:$false
        Write-Host "Removed legacy scheduled task: $Name"
    }
}

$PrecheckAction = New-ScheduledTaskAction `
    -Execute $PythonPath `
    -Argument ".\scripts\precheck_badminton.py --config config.json --venue-id 3 --block-type 2 --site-date-type 2 --slots 19:00,20:20 --account-profile badminton" `
    -WorkingDirectory $ProjectRoot

$PrecheckTrigger = New-ScheduledTaskTrigger -Daily -At "17:55"
Register-ScheduledTask `
    -TaskName "SZTU Smart Badminton Precheck 1755" `
    -Action $PrecheckAction `
    -Trigger $PrecheckTrigger `
    -Settings $Settings `
    -Description "Refresh login and precheck tomorrow badminton package sessions before 18:00 booking." `
    -Force | Out-Null
Write-Host "Installed scheduled task: SZTU Smart Badminton Precheck 1755"

$BookTasks = @(
    @{
        Name = "SZTU Smart Booking Badminton 18"
        At = "17:59"
        Profile = "badminton_18pm"
    },
    @{
        Name = "SZTU Smart Booking FitnessCenter 19"
        At = "18:59"
        Profile = "fitness_center_19pm"
    },
    @{
        Name = "SZTU Smart Booking GymRoom 19"
        At = "18:59"
        Profile = "gym_room_19pm"
    }
)

foreach ($Task in $BookTasks) {
    $Action = New-ScheduledTaskAction `
        -Execute $PythonPath `
        -Argument ".\scripts\automation_dispatch.py --config config.json book-profile --profile $($Task.Profile)" `
        -WorkingDirectory $ProjectRoot

    $Trigger = New-ScheduledTaskTrigger -Daily -At $Task.At
    Register-ScheduledTask `
        -TaskName $Task.Name `
        -Action $Action `
        -Trigger $Trigger `
        -Settings $Settings `
        -Description "Smart booking via profile: $($Task.Profile)" `
        -Force | Out-Null
    Write-Host "Installed scheduled task: $($Task.Name)"
}

$OldTask = Get-ScheduledTask -TaskName "SZTU Smart Auto Cancel Due" -ErrorAction SilentlyContinue
if ($OldTask) {
    Unregister-ScheduledTask -TaskName "SZTU Smart Auto Cancel Due" -Confirm:$false
    Write-Host "Removed scheduled task: SZTU Smart Auto Cancel Due"
}

$PlannerAction = New-ScheduledTaskAction `
    -Execute $PythonPath `
    -Argument ".\scripts\plan_cancel_tasks.py --config config.json --grace-minutes 61" `
    -WorkingDirectory $ProjectRoot

$PlannerTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).Date `
    -RepetitionInterval (New-TimeSpan -Hours 1) `
    -RepetitionDuration (New-TimeSpan -Days 3650)

Register-ScheduledTask `
    -TaskName "SZTU Smart Cancel Planner Hourly" `
    -Action $PlannerAction `
    -Trigger $PlannerTrigger `
    -Settings $Settings `
    -Description "Hourly planner: scan today's orders and schedule exact cancel-time tasks." `
    -Force | Out-Null

Write-Host "Installed scheduled task: SZTU Smart Cancel Planner Hourly"
