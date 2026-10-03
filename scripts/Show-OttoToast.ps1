<#
.SYNOPSIS
  Raise a Windows toast from Otto.

.DESCRIPTION
  Uses the WinRT ToastNotificationManager, which needs no third-party module
  (BurntToast is not installed on this box and adding a dependency for one toast is
  not worth it). Falls back to a NotifyIcon balloon if WinRT refuses, so a failure to
  notify never becomes a failure to run.

  Registering a real AUMID would need an installed Start-menu app. Borrowing
  PowerShell's own AUMID is the standard workaround and is why the toast says
  "Windows PowerShell" as the source.

  Exit 0 whether the toast rendered or not. A notification that cannot be shown is
  still recorded in Otto's notice list, which is the durable channel; the toast is
  only the nudge.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Title,
    [string]$Body = "",
    [ValidateSet("info", "warn", "crit")][string]$Level = "info",
    # When set, the toast grows a Reply button that opens this URL (the dashboard,
    # deep-linked to one card's reply box). Protocol activation is the only kind a
    # borrowed AUMID can do, and it cannot carry typed text back, so the typing
    # happens one click away in the dashboard rather than in the toast itself.
    [string]$ReplyUrl = ""
)

$ErrorActionPreference = 'Stop'
$AUMID = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'

function Show-WinRtToast {
    param($Title, $Body, $ReplyUrl)
    $null = [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
    $null = [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom, ContentType = WindowsRuntime]

    # Escape for XML: an ampersand in a task title would otherwise break the payload.
    $t = [System.Security.SecurityElement]::Escape($Title)
    $b = [System.Security.SecurityElement]::Escape($Body)

    $launch = "http://127.0.0.1:8787/"
    $actions = ""
    if ($ReplyUrl) {
        $u = [System.Security.SecurityElement]::Escape($ReplyUrl)
        # Clicking the body lands on the same card as the button. Dismiss is the
        # system action so the toast can be put away without opening anything.
        $launch = $u
        $actions = @"
  <actions>
    <action content="Reply" activationType="protocol" arguments="$u"/>
    <action content="Dismiss" activationType="system" arguments="dismiss"/>
  </actions>
"@
    }

    $xml = @"
<toast activationType="protocol" launch="$launch">
  <visual>
    <binding template="ToastGeneric">
      <text>$t</text>
      <text>$b</text>
    </binding>
  </visual>
$actions
</toast>
"@
    $doc = New-Object Windows.Data.Xml.Dom.XmlDocument
    $doc.LoadXml($xml)
    $toast = New-Object Windows.UI.Notifications.ToastNotification $doc
    [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($AUMID).Show($toast)
}

function Show-BalloonFallback {
    param($Title, $Body, $Level)
    Add-Type -AssemblyName System.Windows.Forms
    Add-Type -AssemblyName System.Drawing
    $icon = New-Object System.Windows.Forms.NotifyIcon
    $icon.Icon = [System.Drawing.SystemIcons]::Information
    $icon.Visible = $true
    $tip = switch ($Level) {
        'crit' { [System.Windows.Forms.ToolTipIcon]::Error }
        'warn' { [System.Windows.Forms.ToolTipIcon]::Warning }
        default { [System.Windows.Forms.ToolTipIcon]::Info }
    }
    $icon.ShowBalloonTip(8000, $Title, $Body, $tip)
    Start-Sleep -Seconds 9
    $icon.Dispose()
}

try {
    Show-WinRtToast -Title $Title -Body $Body -ReplyUrl $ReplyUrl
    Write-Output "toast: winrt"
} catch {
    try {
        Show-BalloonFallback -Title $Title -Body $Body -Level $Level
        Write-Output "toast: balloon (winrt failed: $($_.Exception.Message))"
    } catch {
        Write-Output "toast: none ($($_.Exception.Message))"
    }
}
exit 0
