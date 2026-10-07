<#
.SYNOPSIS
    Installs the Footage Archive Opener for Windows.

.DESCRIPTION
    Registers the footage-archive:// URL scheme under
    HKCU:\Software\Classes\footage-archive, so that "Open in Photoshop" in
    Footage Archive can hand off to a desktop app. No admin rights are
    required - everything is installed under %APPDATA% and the registry
    key is created under HKCU (the current user hive).

    The scheme's command runs a small wscript.exe wrapper (launch.vbs),
    which in turn starts opener.ps1 hidden, so no console window flashes.
    Both files are embedded in this setup script and written out to
    %APPDATA%\FootageArchiveOpener.

    If -Origin is given, this script also sets the Chrome policy
    AutoLaunchProtocolsFromOrigins under HKCU, so Chrome stops asking
    "Open Footage Archive Opener?" on every click. Chrome must be fully
    quit and restarted for the policy to take effect - check
    chrome://policy afterwards. Edge users would need the same policy
    under HKCU\Software\Policies\Microsoft\Edge; this script does not set
    that.

.PARAMETER Root
    Local path or UNC share where the footage lives, e.g.
    \\192.168.2.230\footage. Written to config.json on first install, or
    whenever -Root is passed again. Defaults to \\192.168.2.230\footage.

.PARAMETER Origin
    Base URL Footage Archive is served from, e.g.
    http://192.168.2.230:8050. When given, sets the Chrome policy
    AutoLaunchProtocolsFromOrigins so Chrome auto-launches the opener for
    this origin without asking every time.

.PARAMETER Uninstall
    Removes the registered scheme, the installed files under %APPDATA%,
    and the Chrome policy (if this script set it), then exits.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File footage-archive-opener-windows.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File footage-archive-opener-windows.ps1 -Root \\192.168.2.230\footage -Origin http://192.168.2.230:8050

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File footage-archive-opener-windows.ps1 -Uninstall
#>

param(
    [string]$Root = '\\192.168.2.230\footage',
    [string]$Origin = '',
    [switch]$Uninstall
)

$RootExplicitlyPassed = $PSBoundParameters.ContainsKey('Root')

# 5.1 is always Windows (no $IsWindows variable exists there at all); only
# guard when running on PowerShell 6+ and NOT on Windows.
if ($PSVersionTable.PSVersion.Major -ge 6 -and -not $IsWindows) {
    Write-Output "This script only runs on Windows."
    exit 1
}

$ErrorActionPreference = 'Stop'

$Dir = "$env:APPDATA\FootageArchiveOpener"
$Config = "$Dir\config.json"
$Scheme = 'footage-archive'
$ClassKey = "HKCU:\Software\Classes\$Scheme"
$PolicyKey = 'HKCU:\Software\Policies\Google\Chrome\AutoLaunchProtocolsFromOrigins'

if ($Uninstall) {
    if (Test-Path -LiteralPath $ClassKey) {
        Remove-Item -LiteralPath $ClassKey -Recurse -Force
    }
    if (Test-Path -LiteralPath $Dir) {
        Remove-Item -LiteralPath $Dir -Recurse -Force
    }
    if (Test-Path -LiteralPath $PolicyKey) {
        $policyItem = Get-ItemProperty -LiteralPath $PolicyKey -ErrorAction SilentlyContinue
        $existing = $null
        if ($policyItem -and ($policyItem.PSObject.Properties.Name -contains '1')) {
            $existing = $policyItem.'1'
        }
        if ($existing -and ($existing -like '*footage-archive*')) {
            Remove-ItemProperty -LiteralPath $PolicyKey -Name '1' -Force
            if ((Get-Item -LiteralPath $PolicyKey).Property.Count -eq 0) {
                Remove-Item -LiteralPath $PolicyKey -Force
            }
            Write-Output "Removed the Chrome policy AutoLaunchProtocolsFromOrigins - quit and restart Chrome for this to take effect."
        }
    }
    Write-Output "Footage Archive Opener uninstalled."
    exit 0
}

New-Item -ItemType Directory -Force -Path $Dir | Out-Null

# --- opener.ps1: the actual logic, as a single-quoted here-string so ---
# --- nothing here expands. ---
$OpenerSource = @'
param([Parameter(Mandatory = $true)][string]$Url)
$ErrorActionPreference = 'Stop'

$script:FormsAvailable = $null

function Test-FormsAvailable {
    if ($null -eq $script:FormsAvailable) {
        try {
            Add-Type -AssemblyName System.Windows.Forms -ErrorAction Stop
            $script:FormsAvailable = $true
        } catch {
            $script:FormsAvailable = $false
        }
    }
    return $script:FormsAvailable
}

function Show-Message([string]$Text, [bool]$IsError) {
    if (Test-FormsAvailable) {
        $icon = if ($IsError) { 'Error' } else { 'Information' }
        [System.Windows.Forms.MessageBox]::Show($Text, 'Footage Archive Opener', 'OK', $icon) | Out-Null
    } else {
        if ($IsError) {
            [Console]::Error.WriteLine($Text)
        } else {
            Write-Output $Text
        }
    }
}

function Fail([string]$Text) {
    Show-Message $Text $true
    exit 1
}

$Config = Join-Path $env:APPDATA 'FootageArchiveOpener\config.json'

function Get-Config {
    if (-not (Test-Path -LiteralPath $Config -PathType Leaf)) {
        Fail "Config not found: $Config"
    }
    try {
        return (Get-Content -LiteralPath $Config -Raw -Encoding UTF8 | ConvertFrom-Json)
    } catch {
        Fail "Config not found: $Config"
    }
}

$cfg = Get-Config

$Apps = @{
    photoshop = @{
        Label      = 'Photoshop'
        Extensions = @('jpg', 'jpeg', 'rw2', 'dng', 'insp', 'png', 'tif', 'tiff', 'psd')
    }
}

function Test-AppAllowed([string]$Id) {
    if (-not $Apps.ContainsKey($Id)) {
        return $false
    }
    if (-not $cfg.apps) {
        return $false
    }
    return ($cfg.apps.PSObject.Properties.Name -contains $Id)
}

# The whole registry step is wrapped in try/catch: HKLM:\SOFTWARE\Adobe
# does not exist at all when this runs under pwsh on Linux (the test
# suite), and that must be harmless, not a fatal error.
function Find-Photoshop {
    if ($cfg.apps.photoshop.exe -and (Test-Path -LiteralPath $cfg.apps.photoshop.exe -PathType Leaf)) {
        return $cfg.apps.photoshop.exe
    }

    try {
        $versionKeys = Get-ChildItem 'HKLM:\SOFTWARE\Adobe\Photoshop' -ErrorAction SilentlyContinue
        if ($versionKeys) {
            $best = $versionKeys | Sort-Object { [double]$_.PSChildName } -Descending | Select-Object -First 1
            if ($best) {
                $appPath = (Get-ItemProperty -LiteralPath $best.PSPath -ErrorAction SilentlyContinue).ApplicationPath
                if ($appPath) {
                    $exe = Join-Path $appPath 'Photoshop.exe'
                    if (Test-Path -LiteralPath $exe -PathType Leaf) {
                        return $exe
                    }
                }
            }
        }
    } catch {
        # No HKLM:\SOFTWARE\Adobe\Photoshop drive/key on this machine - ignore.
    }

    $glob = Get-ChildItem 'C:\Program Files\Adobe\Adobe Photoshop *\Photoshop.exe' -ErrorAction SilentlyContinue |
        Sort-Object Name -Descending | Select-Object -First 1
    if ($glob) {
        return $glob.FullName
    }

    return $null
}

function Resolve-AppExe([string]$Id) {
    $override = $cfg.apps.$Id.exe
    if ($override -and (Test-Path -LiteralPath $override -PathType Leaf)) {
        return $override
    }
    switch ($Id) {
        'photoshop' { return Find-Photoshop }
        default { return $null }
    }
}

function Invoke-Test {
    $lines = @()

    $root = $cfg.root
    if ($root -and (Test-Path -LiteralPath $root -PathType Container)) {
        $lines += "Root: $root OK"
    } else {
        $lines += "Root: $root not reachable (share not connected?)"
    }

    foreach ($id in $Apps.Keys) {
        if ($cfg.apps -and ($cfg.apps.PSObject.Properties.Name -contains $id)) {
            $label = $Apps[$id].Label
            $exe = Resolve-AppExe $id
            if ($exe) {
                $lines += "${label}: $exe OK"
            } else {
                $lines += "${label}: not found"
            }
        }
    }

    Show-Message ($lines -join "`n") $false
    exit 0
}

function Invoke-Open([string]$App, [string]$Path) {
    if (-not $App -or -not $Path) {
        Fail "app and path are required"
    }

    if (-not (Test-AppAllowed $App)) {
        Fail "Unknown app: $App"
    }

    if ($Path -match '^[\\/]' -or $Path -match '^[A-Za-z]:' -or $Path.Contains('\')) {
        Fail "Invalid path: $Path"
    }

    $segments = $Path -split '/'
    foreach ($seg in $segments) {
        if (-not $seg -or $seg -eq '..') {
            Fail "Invalid path: $Path"
        }
    }

    $lastSegment = $segments[$segments.Length - 1]
    if ($lastSegment.Contains('.')) {
        $ext = $lastSegment.Substring($lastSegment.LastIndexOf('.') + 1).ToLowerInvariant()
    } else {
        $ext = ''
    }

    $label = $Apps[$App].Label
    if ($Apps[$App].Extensions -notcontains $ext) {
        Fail "File type .$ext is not supported by $label"
    }

    $root = $cfg.root
    if (-not $root) {
        Fail "No root configured"
    }

    $full = Join-Path $root ($Path -replace '/', [string][IO.Path]::DirectorySeparatorChar)

    if (-not (Test-Path -LiteralPath $root -PathType Container)) {
        Fail "Share not reachable: $root"
    }
    if (-not (Test-Path -LiteralPath $full -PathType Leaf)) {
        Fail "File not found: $full"
    }

    $exe = Resolve-AppExe $App
    if (-not $exe) {
        Fail "$label not found. Set apps.$App.exe in $Config."
    }

    Start-Process -FilePath $exe -ArgumentList ('"' + $full + '"')
}

$withoutScheme = $Url -replace '^footage-archive://', ''
$queryIndex = $withoutScheme.IndexOf('?')
if ($queryIndex -ge 0) {
    $actionPart = $withoutScheme.Substring(0, $queryIndex)
    $queryPart = $withoutScheme.Substring($queryIndex + 1)
} else {
    $actionPart = $withoutScheme
    $queryPart = ''
}
$action = $actionPart.TrimEnd('/')

$query = @{}
if ($queryPart) {
    foreach ($pair in $queryPart -split '&') {
        if (-not $pair) {
            continue
        }
        $eqIndex = $pair.IndexOf('=')
        if ($eqIndex -ge 0) {
            $key = $pair.Substring(0, $eqIndex)
            $val = $pair.Substring($eqIndex + 1)
        } else {
            $key = $pair
            $val = ''
        }
        $query[$key] = [uri]::UnescapeDataString($val)
    }
}

switch ($action) {
    'test' { Invoke-Test }
    'open' { Invoke-Open $query['app'] $query['path'] }
    default { Fail "Unknown action: $action" }
}
'@

# --- launch.vbs: hides the console window that powershell.exe would ---
# --- otherwise flash when the scheme handler runs. ---
$LaunchSource = @'
Set sh = CreateObject("WScript.Shell")
dir = Left(WScript.ScriptFullName, InStrRev(WScript.ScriptFullName, "\"))
sh.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & dir & "opener.ps1"" """ & WScript.Arguments(0) & """", 0, False
'@

Set-Content -LiteralPath "$Dir\opener.ps1" -Value $OpenerSource -Encoding UTF8
Set-Content -LiteralPath "$Dir\launch.vbs" -Value $LaunchSource -Encoding ASCII

if ((-not (Test-Path -LiteralPath $Config)) -or $RootExplicitlyPassed) {
    $configObj = [ordered]@{
        root = $Root
        apps = [ordered]@{ photoshop = @{} }
    }
    $configJson = $configObj | ConvertTo-Json -Depth 3
    Set-Content -LiteralPath $Config -Value $configJson -Encoding UTF8
}

New-Item -Path $ClassKey -Value 'URL:Footage Archive' -Force | Out-Null
New-ItemProperty -Path $ClassKey -Name 'URL Protocol' -Value '' -PropertyType String -Force | Out-Null
New-Item -Path "$ClassKey\shell\open\command" -Value ('wscript.exe "' + $Dir + '\launch.vbs" "%1"') -Force | Out-Null

if ($Origin) {
    New-Item -Path $PolicyKey -Force | Out-Null
    $policyValue = '[{"protocol":"footage-archive","allowed_origins":["' + $Origin + '"]}]'
    New-ItemProperty -Path $PolicyKey -Name '1' -Value $policyValue -PropertyType String -Force | Out-Null
    Write-Output "Chrome policy set for origin $Origin."
    Write-Output "Quit Chrome completely and reopen it, then check chrome://policy."
}

$currentRoot = $Root
try {
    $cfgRead = Get-Content -LiteralPath $Config -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($cfgRead.root) {
        $currentRoot = $cfgRead.root
    }
} catch {
    # Leave $currentRoot at $Root if the config can't be re-read for some reason.
}

$Arrow = [char]0x2192

Write-Output ""
Write-Output "Installed:"
Write-Output "  Scheme: $ClassKey"
Write-Output "  Files:  $Dir"
Write-Output "  Config: $Config (root: $currentRoot)"
Write-Output ""
Write-Output "Now enable ""Opener is installed on this device"" in Settings $Arrow Open in and press Test."
