[CmdletBinding()]
param([string]$InstallRoot = "")

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"
if ([string]::IsNullOrWhiteSpace($InstallRoot)) { $InstallRoot = $PSScriptRoot }

$systemRoot = Join-Path ([Environment]::GetFolderPath([Environment+SpecialFolder]::CommonApplicationData)) "FunPayHomeRelay"
$manualRoot = Join-Path ([Environment]::GetFolderPath([Environment+SpecialFolder]::LocalApplicationData)) "FunPayHomeRelay"
$requestedRoot = [IO.Path]::GetFullPath($InstallRoot).TrimEnd('\')
$isSystemRoot = $requestedRoot.Equals([IO.Path]::GetFullPath($systemRoot).TrimEnd('\'), [StringComparison]::OrdinalIgnoreCase)
$isManualRoot = $requestedRoot.Equals([IO.Path]::GetFullPath($manualRoot).TrimEnd('\'), [StringComparison]::OrdinalIgnoreCase)
if (-not $isSystemRoot -and -not $isManualRoot) { throw "Unsupported relay installation path." }
$canonicalScript = Join-Path $requestedRoot "Stop.ps1"
if (-not [IO.Path]::GetFullPath($PSCommandPath).Equals([IO.Path]::GetFullPath($canonicalScript), [StringComparison]::OrdinalIgnoreCase)) {
    throw "Run the canonical Stop.ps1 from the installed relay directory."
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
    $elevated = Start-Process -FilePath (Get-RelayPowerShellPath) -ArgumentList $arguments -Verb RunAs -Wait -PassThru
    exit $elevated.ExitCode
}
$task = if ($isSystemRoot) { ScheduledTasks\Get-ScheduledTask -TaskName $script:RelayTaskName -ErrorAction SilentlyContinue } else { $null }
if ($task -and $task.State -eq "Running") {
    ScheduledTasks\Stop-ScheduledTask -TaskName $script:RelayTaskName -ErrorAction SilentlyContinue
}

$identityPath = Join-Path $InstallRoot "relay_ed25519"
$sshProcess = Get-RelayProcessFromPidFile -PidPath (Join-Path $InstallRoot "ssh.pid") -RequiredCommandFragment $identityPath
if ($sshProcess) {
    Microsoft.PowerShell.Management\Stop-Process -Id $sshProcess.ProcessId -Force -ErrorAction SilentlyContinue
}

$runnerPath = Join-Path $InstallRoot "Relay.ps1"
$runner = Get-RelayProcessFromPidFile -PidPath (Join-Path $InstallRoot "runner.pid") -RequiredCommandFragment $runnerPath
if ($runner) {
    Microsoft.PowerShell.Management\Stop-Process -Id $runner.ProcessId -Force -ErrorAction SilentlyContinue
}

Remove-Item -LiteralPath (Join-Path $InstallRoot "runner.pid") -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath (Join-Path $InstallRoot "ssh.pid") -Force -ErrorAction SilentlyContinue
Write-RelayState -InstallRoot $InstallRoot -Status "stopped" -Detail "Stopped by the local operator."
Write-RelayLog -InstallRoot $InstallRoot -Message "Relay stopped by the local operator."
Write-Host "FunPay Home Relay stopped."
