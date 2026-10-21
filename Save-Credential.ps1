$ErrorActionPreference = 'Stop'
$localFolder = Join-Path $PSScriptRoot '.local'
New-Item -ItemType Directory -Path $localFolder -Force | Out-Null
$credential = Get-Credential -Message 'HDU school SSO account and password'
if ($null -eq $credential) { throw 'Credential entry cancelled.' }
$credential | Export-Clixml -LiteralPath (Join-Path $localFolder 'credentials.xml')
Write-Output 'Saved with Windows DPAPI encryption for this Windows user on this PC.'
