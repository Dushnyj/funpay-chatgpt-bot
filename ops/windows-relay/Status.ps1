[CmdletBinding()]
param(
    [string]$InstallRoot = "",
    [switch]$KeepOpen
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
$canonicalScript = Join-Path $requestedRoot "Status.ps1"
if (-not [IO.Path]::GetFullPath($PSCommandPath).Equals([IO.Path]::GetFullPath($canonicalScript), [StringComparison]::OrdinalIgnoreCase)) {
    throw "Run the canonical Status.ps1 from the installed relay directory."
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
$isAdministrator = Test-RelayAdministrator
if ($isSystemRoot -and $isAdministrator) { Set-RelayTrustedModulePath }
if ($isSystemRoot -and -not $isAdministrator) {
    $arguments = '-NoLogo -NoProfile -ExecutionPolicy Bypass -File "{0}" -InstallRoot "{1}"' -f $PSCommandPath, $InstallRoot
    if ($KeepOpen) { $arguments += " -KeepOpen" }
    $elevated = Start-Process -FilePath (Get-RelayPowerShellPath) -ArgumentList $arguments -Verb RunAs -Wait -PassThru
    exit $elevated.ExitCode
}
$config = Read-RelayConfig -InstallRoot $InstallRoot
$runnerPath = Join-Path $InstallRoot "Relay.ps1"
$runner = Get-RelayProcessFromPidFile -PidPath (Join-Path $InstallRoot "runner.pid") -RequiredCommandFragment $runnerPath
$statePath = Join-Path $InstallRoot "state.json"
$state = $null
if (Test-Path -LiteralPath $statePath -PathType Leaf) {
    try { $state = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json } catch { }
}
$task = if ($isSystemRoot) { ScheduledTasks\Get-ScheduledTask -TaskName $script:RelayTaskName -ErrorAction SilentlyContinue } else { $null }

[pscustomobject]@{
    Relay = $config.displayName
    Installed = $true
    Running = [bool]$runner
    Connection = if ($state) { $state.status } else { "unknown" }
    LastUpdateUtc = if ($state) { $state.updatedAtUtc } else { $null }
    AutoStart = [bool]$task
    TaskState = if ($task) { [string]$task.State } else { "NotInstalled" }
    Server = "{0}:{1}" -f $config.sshHost, $config.sshPort
} | Format-List

$statusCode = 0
if (-not $runner) { $statusCode = 1 }
elseif (-not $state -or $state.status -ne "connected") { $statusCode = 2 }
if ($KeepOpen) {
    [void](Read-Host "Press Enter to close")
    return
}
exit $statusCode
