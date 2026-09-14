# load-env.ps1
#
# dbt reads credentials through env_var(), which looks at the process
# environment. It does not read .env files the way python-dotenv does.
# This loads every KEY=VALUE line from the project root .env into the
# current PowerShell session only, so nothing is written to the user or
# machine environment and nothing persists after the terminal closes.
#
# Run once per terminal session, before any dbt command.
#
# Usage, from the project root:
#   . .\dbt\load-env.ps1

$ErrorActionPreference = "Stop"

$envPath = Join-Path (Split-Path $PSScriptRoot -Parent) ".env"

if (-not (Test-Path $envPath)) {
    throw "No .env found at $envPath"
}

$loaded = 0

Get-Content $envPath | ForEach-Object {
    $line = $_.Trim()

    # Skip blanks and comments
    if ($line -eq "" -or $line.StartsWith("#")) { return }

    $idx = $line.IndexOf("=")
    if ($idx -lt 1) { return }

    $key = $line.Substring(0, $idx).Trim()
    $val = $line.Substring($idx + 1).Trim()

    # Strip one layer of surrounding quotes if present
    if (($val.StartsWith('"') -and $val.EndsWith('"')) -or
        ($val.StartsWith("'") -and $val.EndsWith("'"))) {
        if ($val.Length -ge 2) { $val = $val.Substring(1, $val.Length - 2) }
    }

    [Environment]::SetEnvironmentVariable($key, $val, "Process")
    $script:loaded++

    # Print the key only. Never print the value.
    Write-Host "loaded $key"
}

Write-Host ""
Write-Host "$loaded variables loaded into this session."
