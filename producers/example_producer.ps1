<#
.SYNOPSIS
  Reference feed producer. Copy this, replace Get-FeedPayload, keep the writer.

.DESCRIPTION
  Shows the whole producer contract: gather something, build the envelope, write
  it atomically to FEED_DIR\<source>\current.json. It imports nothing from Otto
  and does not care whether the daemon is running.

  This script is a TEMPLATE and is not scheduled. `example` is not declared in
  config.FEED_SOURCES, so a drop it writes is reported by `otto feeds` as an
  undeclared directory and is deliberately NOT ingested. That is the design: a
  directory is not consent. Declare the source to turn it on.

  See producers\README.md for the envelope, the trust levels, and why this is a
  file rather than an HTTP push.

.EXAMPLE
  powershell -NoProfile -File producers\example_producer.ps1 -Source example
#>

[CmdletBinding()]
param(
    # Must match a declared FeedSource name to be ingested.
    [string]$Source = 'example',

    # Feed root. Matches config.FEED_DIR; override for a dry run somewhere safe.
    [string]$FeedRoot = (Join-Path $env:USERPROFILE '.claude\otto\feed')
)

$ErrorActionPreference = 'Stop'

function Get-FeedPayload {
    <#
      REPLACE THIS. Return a hashtable with:
        summary  one line, the panel headline. Say the number.
        items    at most 10, each a flat object. This is a panel, not a log.

      Return $null to write nothing at all. That matters: a producer with nothing
      to say should leave the previous drop alone rather than overwrite it with an
      empty one, because "0 items" and "I did not run" must stay distinguishable.
    #>

    $stale = Get-ChildItem -Path $env:TEMP -File -ErrorAction SilentlyContinue |
             Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-30) }

    return @{
        summary = "$($stale.Count) file(s) in TEMP older than 30 days"
        items   = @(
            $stale | Sort-Object Length -Descending | Select-Object -First 10 |
            ForEach-Object {
                @{
                    when  = $_.LastWriteTime.ToString('yyyy-MM-dd')
                    title = "$($_.Name) ($([math]::Round($_.Length / 1MB, 1)) MB)"
                }
            }
        )
    }
}

$payload = Get-FeedPayload
if ($null -eq $payload) {
    Write-Verbose 'nothing to report; leaving the previous drop in place'
    exit 0
}

# produced_at is when the DATA was gathered, and it is what Otto renders as age.
# A future stamp is rejected by the ingester on purpose: it is how a producer that
# has gone quiet would look permanently fresh.
$envelope = @{
    produced_at = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    producer    = "example_producer.ps1 on $env:COMPUTERNAME"
    summary     = $payload.summary
    items       = @($payload.items)
}

$dir = Join-Path $FeedRoot $Source
if (-not (Test-Path -LiteralPath $dir)) {
    New-Item -ItemType Directory -Path $dir -Force | Out-Null
}
$target = Join-Path $dir 'current.json'

# Atomic: write beside the target, then move over it. A reader must never see a
# half-written file. Otto tolerates one (it retries next tick) but tolerating is
# not the same as being correct, and the temp-then-move costs nothing.
$tmp = Join-Path $dir ('.current.json.' + [guid]::NewGuid().ToString('N') + '.tmp')
try {
    # -Depth matters: the default of 2 silently flattens nested items to strings.
    $envelope | ConvertTo-Json -Depth 6 |
        Out-File -LiteralPath $tmp -Encoding utf8 -NoNewline
    Move-Item -LiteralPath $tmp -Destination $target -Force
}
finally {
    if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Force }
}

Write-Host "wrote $target ($($envelope.items.Count) item(s))"
