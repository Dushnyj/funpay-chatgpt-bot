[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = "High")]
param(
    [string]$InstallRoot = "",
    [switch]$KeepLogs,
    [switch]$Force
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"
if ([string]::IsNullOrWhiteSpace($InstallRoot)) { $InstallRoot = $PSScriptRoot }

$systemRoot = Join-Path ([Environment]::GetFolderPath([Environment+SpecialFolder]::CommonApplicationData)) "FunPayHomeRelay"
$manualRoot = Join-Path ([Environment]::GetFolderPath([Environment+SpecialFolder]::LocalApplicationData)) "FunPayHomeRelay"
$requestedRoot = [IO.Path]::GetFullPath($InstallRoot).TrimEnd('\')
$isSystemRoot = $requestedRoot.Equals([IO.Path]::GetFullPath($systemRoot).TrimEnd('\'), [StringComparison]::OrdinalIgnoreCase)
$isManualRoot = $requestedRoot.Equals([IO.Path]::GetFullPath($manualRoot).TrimEnd('\'), [StringComparison]::OrdinalIgnoreCase)
if (-not $isSystemRoot -and -not $isManualRoot) { throw "Unsupported relay installation path." }
$canonicalScript = Join-Path $requestedRoot "Uninstall.ps1"
if (-not [IO.Path]::GetFullPath($PSCommandPath).Equals([IO.Path]::GetFullPath($canonicalScript), [StringComparison]::OrdinalIgnoreCase)) {
    throw "Run the canonical Uninstall.ps1 from the installed relay directory."
}
function Assert-BootstrapProtectedPath {
    param([string]$Path, [switch]$Directory)
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -or ($Directory -and -not $item.PSIsContainer)) { throw "Protected relay bootstrap path is unsafe: $Path" }
    $acl = Get-Acl -LiteralPath $Path
    $owner = (New-Object Security.Principal.NTAccount($acl.Owner)).Translate([Security.Principal.SecurityIdentifier]).Value
    if ($owner -notin @("S-1-5-18", "S-1-5-32-544")) { throw "Protected relay bootstrap owner is untrusted: $Path" }
    $writeMask = [Security.AccessControl.FileSystemRights]::Write -bor [Security.AccessControl.FileSystemRights]::Delete -bor [Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles -bor [Security.AccessControl.FileSystemRights]::ChangePermissions -bor [Security.AccessControl.FileSystemRights]::TakeOwnership
    foreach ($rule in $acl.Access) {
        if ($rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow) { continue }
        $sid = $rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
        if ($sid -notin @("S-1-5-18", "S-1-5-32-544") -and ($rule.FileSystemRights -band $writeMask)) { throw "Protected relay bootstrap path is writable by an untrusted principal: $Path" }
    }
}
if ($isSystemRoot) {
    $parentItem = Get-Item -LiteralPath (Split-Path -Parent $requestedRoot) -Force -ErrorAction Stop
    if (-not $parentItem.PSIsContainer -or ($parentItem.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw "CommonApplicationData parent is unsafe." }
    foreach ($rule in (Get-Acl -LiteralPath $parentItem.FullName).Access) {
        if ($rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow -or ($rule.PropagationFlags -band [Security.AccessControl.PropagationFlags]::InheritOnly)) { continue }
        $sid = $rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
        if ($sid -notin @("S-1-5-18", "S-1-5-32-544") -and ($rule.FileSystemRights -band [Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles)) { throw "CommonApplicationData allows an untrusted principal to replace the relay root." }
    }
    Assert-BootstrapProtectedPath -Path $requestedRoot -Directory
    Assert-BootstrapProtectedPath -Path $canonicalScript
    Assert-BootstrapProtectedPath -Path (Join-Path $requestedRoot "Common.ps1")
} else {
    $bootstrapPrincipal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    if ($bootstrapPrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw "Manual relay control must run without administrator rights." }
}
. (Join-Path $requestedRoot "Common.ps1")

Assert-RelayWindows
if ($isSystemRoot) { Assert-RelayProtectedSystemRuntime -Root $requestedRoot }
Assert-RelayTreeHasNoReparsePoints -Root $requestedRoot
$isAdministrator = Test-RelayAdministrator
if ($isManualRoot -and $isAdministrator) {
    throw "Manual relay removal must run in a normal, non-administrator PowerShell window."
}
if ($isSystemRoot -and $KeepLogs) {
    throw "Copy logs from the protected relay directory before system uninstall; elevated uninstall will not write into a user-writable profile."
}
if ($isSystemRoot -and -not $isAdministrator) {
    $elevatedArguments = @(
        "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", $canonicalScript,
        "-InstallRoot", $requestedRoot
    )
    if ($KeepLogs) { $elevatedArguments += "-KeepLogs" }
    if ($Force) { $elevatedArguments += "-Force" }
    $elevatedLine = (($elevatedArguments | ForEach-Object { Quote-RelayProcessArgument -Value ([string]$_) }) -join " ")
    $elevated = Start-Process -FilePath (Get-RelayPowerShellPath) -ArgumentList $elevatedLine -Verb RunAs -Wait -PassThru
    exit $elevated.ExitCode
}
if ($isSystemRoot) { Set-RelayTrustedModulePath }
$savedConfirmPreference = $ConfirmPreference
try {
    if ($Force) { $ConfirmPreference = "None" }
    $shouldRemove = $PSCmdlet.ShouldProcess($InstallRoot, "Stop and remove FunPay Home Relay")
} finally {
    $ConfirmPreference = $savedConfirmPreference
}
if (-not $shouldRemove) {
    exit 0
}

& (Join-Path $PSScriptRoot "Stop.ps1") -InstallRoot $InstallRoot
$task = if ($isSystemRoot) { ScheduledTasks\Get-ScheduledTask -TaskName $script:RelayTaskName -ErrorAction SilentlyContinue } else { $null }
if ($task) {
    ScheduledTasks\Stop-ScheduledTask -TaskName $script:RelayTaskName -ErrorAction SilentlyContinue
    ScheduledTasks\Unregister-ScheduledTask -TaskName $script:RelayTaskName -Confirm:$false
}

if ($isManualRoot) {
    $startMenuFolder = Join-Path ([Environment]::GetFolderPath([Environment+SpecialFolder]::Programs)) "FunPay Home Relay"
    $desktopPath = [Environment]::GetFolderPath([Environment+SpecialFolder]::DesktopDirectory)
    foreach ($action in @("Start", "Stop", "Status")) {
        foreach ($shortcutPath in @(
            (Join-Path $startMenuFolder ("{0}.lnk" -f $action)),
            (Join-Path $desktopPath ("FunPay Relay - {0}.lnk" -f $action))
        )) {
            if (Test-Path -LiteralPath $shortcutPath) {
                $shortcut = Get-Item -LiteralPath $shortcutPath -Force
                if ($shortcut.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Refusing to remove a shortcut reparse point: $shortcutPath" }
                Remove-Item -LiteralPath $shortcutPath -Force
            }
        }
    }
    if (Test-Path -LiteralPath $startMenuFolder -PathType Container) {
        $folder = Get-Item -LiteralPath $startMenuFolder -Force
        if ($folder.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Refusing to remove a Start Menu reparse point." }
        if (-not (Get-ChildItem -LiteralPath $startMenuFolder -Force)) {
            Remove-Item -LiteralPath $startMenuFolder -Force
        }
    }
}

if ($KeepLogs) {
    $logBackup = Join-Path (Get-RelayLocalAppDataPath) ("FunPayHomeRelay-logs-{0}" -f (Get-Date -Format "yyyyMMddHHmmss"))
    if (Test-Path -LiteralPath (Join-Path $InstallRoot "logs")) {
        Copy-Item -LiteralPath (Join-Path $InstallRoot "logs") -Destination $logBackup -Recurse -Force
        Write-Host "Logs preserved at $logBackup"
    }
}

Assert-RelayTreeHasNoReparsePoints -Root $requestedRoot
Remove-Item -LiteralPath $requestedRoot -Recurse -Force -ErrorAction Stop
Write-Host "FunPay Home Relay removed from this PC. Delete the route in the admin panel to revoke its registered public key."
