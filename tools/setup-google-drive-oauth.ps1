param(
    [Parameter(Mandatory = $true)]
    [string]$ClientJson,
    [string]$RootFolderId = "",
    [string]$Output = ".devkeys/google-drive-personal.env",
    [switch]$Overwrite
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot ".venv\Scripts\pythonw.exe"
$script = Join-Path $projectRoot "tools\google_drive_oauth_setup.py"
$logDirectory = Join-Path $projectRoot ".devkeys"
$stdout = Join-Path $logDirectory "oauth-setup.stdout.log"
$stderr = Join-Path $logDirectory "oauth-setup.stderr.log"

if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Runtime Python do projeto não encontrado: $python"
}

New-Item -ItemType Directory -Force -Path $logDirectory | Out-Null
Remove-Item -LiteralPath $stdout, $stderr -Force -ErrorAction SilentlyContinue

$arguments = @(
    "-u",
    ('"{0}"' -f $script),
    "--client-json",
    ('"{0}"' -f $ClientJson),
    "--output",
    ('"{0}"' -f $Output)
)
if ($RootFolderId) {
    $arguments += @("--root-folder-id", $RootFolderId)
}
if ($Overwrite) {
    $arguments += "--overwrite"
}

$process = Start-Process `
    -FilePath $python `
    -ArgumentList $arguments `
    -WorkingDirectory $projectRoot `
    -RedirectStandardOutput $stdout `
    -RedirectStandardError $stderr `
    -WindowStyle Hidden `
    -PassThru `
    -Wait

if (Test-Path -LiteralPath $stdout) {
    Get-Content -LiteralPath $stdout | Where-Object { -not $_.StartsWith("AUTHORIZATION_URL=") }
}
if (Test-Path -LiteralPath $stderr) {
    Get-Content -LiteralPath $stderr
}
if ($process.ExitCode -ne 0) {
    throw "A autorização não foi concluída. Corrija a mensagem acima e tente novamente."
}

Remove-Item -LiteralPath $stdout, $stderr -Force -ErrorAction SilentlyContinue
