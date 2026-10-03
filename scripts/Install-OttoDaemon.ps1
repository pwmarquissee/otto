<#
.SYNOPSIS
    Installs the `otto` command and the Windows scheduled tasks that keep the daemon alive.

.DESCRIPTION
    Otto's daemon IS the scheduler: it evaluates every schedule's cadence on each tick
    and runs the ones that are armed. So the only thing Windows needs to guarantee is
    that the daemon is running. Two tasks do that:

      OttoDaemon     starts the daemon at logon.
      OttoKeepalive  every 10 minutes, starts it if it is not running.

    The keepalive is the one that matters. A logon trigger alone means a daemon that
    dies at 02:00 stays dead until the next logon, and every schedule silently misses
    its window. `otto ensure` is a no-op when the daemon is healthy, so running it on a
    tight interval is cheap.

    Deliberately NOT one Windows task per schedule. Two schedulers disagreeing about
    what is due is worse than one, cadence already lives in Otto where `otto autorun`
    can show it, and a missed window is recovered automatically: `is_due` stays true
    until the schedule actually runs, so a machine that was asleep at 08:00 picks the
    run up when it wakes.

    Nothing here is destructive: the profile edit is delimited by markers and
    idempotent, and existing tasks are only replaced after you confirm.

.PARAMETER SkipTasks
    Install the `otto` command only.

.PARAMETER Uninstall
    Remove both scheduled tasks and the profile block. State is left alone.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\Install-OttoDaemon.ps1
#>

[CmdletBinding()]
param(
    [switch]$SkipTasks,
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'

$repoRoot   = Split-Path -Parent $PSScriptRoot
$daemonTask = 'OttoDaemon'
$aliveTask  = 'OttoKeepalive'
$beginMark  = '# >>> otto >>>'
$endMark    = '# <<< otto <<<'

function Say($msg, $color = 'Gray') { Write-Host "  $msg" -ForegroundColor $color }

function Remove-ProfileBlock {
    if (-not (Test-Path $PROFILE)) { return }
    $out = New-Object System.Collections.Generic.List[string]
    $inBlock = $false
    foreach ($line in (Get-Content $PROFILE)) {
        if ($line -eq $beginMark) { $inBlock = $true; continue }
        if ($line -eq $endMark)   { $inBlock = $false; continue }
        if (-not $inBlock) { $out.Add($line) }
    }
    Set-Content -Path $PROFILE -Value $out -Encoding utf8
}

function Remove-OttoTask($name) {
    if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $name -Confirm:$false
        Say "removed scheduled task $name"
        return $true
    }
    return $false
}

# ---------------------------------------------------------------- uninstall

if ($Uninstall) {
    Remove-OttoTask $daemonTask | Out-Null
    Remove-OttoTask $aliveTask  | Out-Null
    Remove-ProfileBlock
    Say "removed otto block from $PROFILE"
    Say "state under ~\.claude\otto was left in place" 'DarkGray'
    exit 0
}

# ---------------------------------------------------------------- 1. `otto` command

$profileDir = Split-Path -Parent $PROFILE
if (-not (Test-Path $profileDir)) { New-Item -ItemType Directory -Force -Path $profileDir | Out-Null }
if (-not (Test-Path $PROFILE))    { New-Item -ItemType File -Path $PROFILE | Out-Null }

Remove-ProfileBlock
Add-Content -Path $PROFILE -Encoding utf8 -Value @"
$beginMark
function otto {
    `$env:PYTHONPATH = if (`$env:PYTHONPATH) { "$repoRoot;`$env:PYTHONPATH" } else { "$repoRoot" }
    & python -m otto @args
}
$endMark
"@
Say "installed 'otto' into $PROFILE" 'Green'
Say "open a new shell, or run: . `$PROFILE" 'DarkGray'

if ($SkipTasks) {
    Say "skipped the scheduled tasks (-SkipTasks). Start manually with: otto serve"
    exit 0
}

# ---------------------------------------------------------------- 2. tasks

$python = (Get-Command python).Source
Say ""
Say "python: $python" 'DarkGray'

foreach ($name in @($daemonTask, $aliveTask)) {
    if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
        $answer = Read-Host "  '$name' already exists. Replace it? (y/N)"
        if ($answer -ne 'y') { Say "left $name alone"; continue }
        Unregister-ScheduledTask -TaskName $name -Confirm:$false
    }

    if ($name -eq $daemonTask) {
        $action   = New-ScheduledTaskAction -Execute $python -Argument '-m otto serve' -WorkingDirectory $repoRoot
        $trigger  = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
        $settings = New-ScheduledTaskSettingsSet `
            -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
            -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 2) `
            -ExecutionTimeLimit ([TimeSpan]::Zero) `
            -MultipleInstances IgnoreNew
        $desc = 'Otto - start the assistant daemon at logon'
    }
    else {
        # `otto ensure` exits immediately when the daemon is healthy, so a 10 minute
        # interval costs nothing and closes the "died at 02:00" hole.
        $action  = New-ScheduledTaskAction -Execute $python -Argument '-m otto ensure' -WorkingDirectory $repoRoot
        $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) `
                      -RepetitionInterval (New-TimeSpan -Minutes 10)
        $settings = New-ScheduledTaskSettingsSet `
            -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
            -ExecutionTimeLimit (New-TimeSpan -Minutes 5) `
            -MultipleInstances IgnoreNew
        $desc = 'Otto - restart the daemon if it is not running'
    }

    Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger `
        -Settings $settings -Description $desc | Out-Null
    Say "registered $name" 'Green'
}

Say ""
Say "start it now:  Start-ScheduledTask -TaskName $daemonTask" 'DarkGray'
Say "dashboard:     http://127.0.0.1:8787" 'DarkGray'
Say ""
Say "Nothing runs unattended until you arm it:" 'Yellow'
Say "  otto autorun                 # master switch + what is armed" 'DarkGray'
Say "  otto schedule arm daily      # arm one schedule" 'DarkGray'
