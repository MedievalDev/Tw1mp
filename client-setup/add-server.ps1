# =====================================================================
#  TW1MP — Two Worlds 1 server setup
#  Adds a community server to the game's server list (per-user registry,
#  no admin rights needed). Creates a .reg backup before changing anything.
#
#  Usage:
#    .\add-server.ps1                          -> interactive
#    .\add-server.ps1 -ServerHost 1.2.3.4      -> add with default name
#    .\add-server.ps1 -Name "My Server" -ServerHost play.example.com
#    .\add-server.ps1 -ServerHost 1.2.3.4 -Remove
# =====================================================================

param(
    [string]$Name = "TW1MP Community Server",
    [string]$ServerHost = "",
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'
$KeyPath = 'HKCU:\SOFTWARE\Reality Pump\TwoWorlds\Network'
$RegExportPath = "HKCU\SOFTWARE\Reality Pump\TwoWorlds\Network"
$ValueName = 'EarthNet_ServersAddresses'
$PortName = 'EarthNet_ServerPort'
$DefaultPort = 17171

Write-Host ""
Write-Host "  TW1MP server setup for Two Worlds (2007)" -ForegroundColor Yellow
Write-Host "  -----------------------------------------"

if (-not $ServerHost) {
    $ServerHost = Read-Host "  Server address (IP or hostname)"
    if (-not $ServerHost) { Write-Host "  No address given - nothing to do." -ForegroundColor Red; exit 1 }
    $n = Read-Host "  Display name [$Name]"
    if ($n) { $Name = $n }
}

# sanity: quotes would corrupt the registry format
if ($Name -match '"' -or $ServerHost -match '"') {
    Write-Host '  Names/addresses must not contain quotes (").' -ForegroundColor Red; exit 1
}

# ensure key exists (created fresh if the game never ran)
$existedBefore = Test-Path $KeyPath
if (-not $existedBefore) {
    New-Item -Path $KeyPath -Force | Out-Null
    Write-Host "  Registry key created (game had no network settings yet)."
}

# backup
if ($existedBefore) {
    $backup = Join-Path $env:USERPROFILE ("Desktop\TwoWorlds-Network-Backup-{0:yyyyMMdd-HHmmss}.reg" -f (Get-Date))
    reg.exe export $RegExportPath $backup /y | Out-Null
    Write-Host "  Backup written: $backup"
}

$current = (Get-ItemProperty -Path $KeyPath -Name $ValueName -ErrorAction SilentlyContinue).$ValueName
if ($null -eq $current) { $current = "" }

$entry = '"' + $Name + '""' + $ServerHost + '"'

if ($Remove) {
    if ($current -notlike "*""$ServerHost""*") {
        Write-Host "  '$ServerHost' is not in the server list - nothing removed." -ForegroundColor Yellow
    } else {
        # rebuild list without any pair whose host matches
        $pairs = [regex]::Matches($current, '"([^"]*)""([^"]*)"')
        $kept = foreach ($p in $pairs) {
            if ($p.Groups[2].Value -ne $ServerHost) { '"' + $p.Groups[1].Value + '""' + $p.Groups[2].Value + '"' }
        }
        Set-ItemProperty -Path $KeyPath -Name $ValueName -Value ($kept -join '')
        Write-Host "  Removed '$ServerHost' from the server list." -ForegroundColor Green
    }
    exit 0
}

if ($current -like "*""$ServerHost""*") {
    Write-Host "  '$ServerHost' is already in the server list - nothing to do." -ForegroundColor Yellow
} else {
    Set-ItemProperty -Path $KeyPath -Name $ValueName -Value ($entry + $current)
    Write-Host "  Added '$Name' ($ServerHost) to the top of the server list." -ForegroundColor Green
}

# port: create if missing, warn if customized
$port = (Get-ItemProperty -Path $KeyPath -Name $PortName -ErrorAction SilentlyContinue).$PortName
if ($null -eq $port) {
    New-ItemProperty -Path $KeyPath -Name $PortName -Value $DefaultPort -PropertyType DWord -Force | Out-Null
    Write-Host "  $PortName set to $DefaultPort."
} elseif ($port -ne $DefaultPort) {
    Write-Host "  Note: $PortName is $port (default is $DefaultPort). Change it in regedit if your server uses the default." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "  Done. Start Two Worlds - the server appears in the server selection." -ForegroundColor Green
Write-Host ""
