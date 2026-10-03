<#
.SYNOPSIS
    Wrapper so `otto` works from any directory.

.DESCRIPTION
    Forwards every argument to `python -m otto`. Install-OttoDaemon.ps1 adds a
    profile function that calls this, so you do not need it on PATH yourself.
#>

$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
$env:PYTHONPATH = if ($env:PYTHONPATH) { "$repoRoot;$env:PYTHONPATH" } else { $repoRoot }

& python -m otto @args
exit $LASTEXITCODE
