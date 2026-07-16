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
$canonicalScript = Join-Path $requestedRoot "Relay.ps1"
if (-not [IO.Path]::GetFullPath($PSCommandPath).Equals([IO.Path]::GetFullPath($canonicalScript), [StringComparison]::OrdinalIgnoreCase)) {
    throw "Run the canonical Relay.ps1 from the installed relay directory."
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
$config = Read-RelayConfig -InstallRoot $InstallRoot
$sshPath = Get-RelaySshPath
$emptyConfig = Join-Path $InstallRoot "ssh_config"
$runnerPidPath = Join-Path $InstallRoot "runner.pid"
$sshPidPath = Join-Path $InstallRoot "ssh.pid"
$attemptErrorPath = Join-Path (Join-Path $InstallRoot "logs") "ssh-attempt.err"
$lockStream = $null
$sshProcess = $null

try {
    try {
        $lockStream = [IO.File]::Open(
            (Join-Path $InstallRoot "relay.lock"),
            [IO.FileMode]::OpenOrCreate,
            [IO.FileAccess]::ReadWrite,
            [IO.FileShare]::None
        )
    } catch [IO.IOException] {
        Write-RelayLog -InstallRoot $InstallRoot -Message "A relay process is already running."
        exit 0
    }

    Set-Content -LiteralPath $runnerPidPath -Value $PID -Encoding ASCII
    Write-RelayState -InstallRoot $InstallRoot -Status "starting" -Detail "Starting protected SSH relay."
    $failureCount = 0

    while ($true) {
        $config = Read-RelayConfig -InstallRoot $InstallRoot
        Remove-Item -LiteralPath $attemptErrorPath -Force -ErrorAction SilentlyContinue
        $forward = "{0}:{1}" -f $config.remoteSocksBind, [int]$config.remoteSocksPort
        $target = "{0}@{1}" -f $config.sshUser, $config.sshHost
        $arguments = @(
            "-N", "-T", "-F", $emptyConfig,
            "-i", [string]$config.identityFile,
            "-p", [string]$config.sshPort,
            "-o", "BatchMode=yes",
            "-o", "PasswordAuthentication=no",
            "-o", "KbdInteractiveAuthentication=no",
            "-o", "PubkeyAuthentication=yes",
            "-o", "IdentitiesOnly=yes",
            "-o", "StrictHostKeyChecking=yes",
            "-o", ("UserKnownHostsFile={0}" -f $config.knownHostsFile),
            "-o", "UpdateHostKeys=no",
            "-o", "ExitOnForwardFailure=yes",
            "-o", "ServerAliveInterval=30",
            "-o", "ServerAliveCountMax=3",
            "-o", "TCPKeepAlive=yes",
            "-o", "PermitLocalCommand=no",
            "-o", "ControlMaster=no",
            "-o", "LogLevel=ERROR",
            "-R", $forward,
            $target
        )
        $argumentLine = (($arguments | ForEach-Object { Quote-RelayProcessArgument -Value ([string]$_) }) -join " ")
        $startedAt = [DateTime]::UtcNow

        try {
            $sshProcess = Start-Process -FilePath $sshPath -ArgumentList $argumentLine -PassThru -WindowStyle Hidden -RedirectStandardError $attemptErrorPath
            Set-Content -LiteralPath $sshPidPath -Value $sshProcess.Id -Encoding ASCII
            Write-RelayState -InstallRoot $InstallRoot -Status "starting" -Detail "SSH started; waiting for the forward." -SshPid $sshProcess.Id

            $markedConnected = $false
            while (-not $sshProcess.WaitForExit(1000)) {
                if (-not $markedConnected -and ([DateTime]::UtcNow - $startedAt).TotalSeconds -ge 3) {
                    $markedConnected = $true
                    $failureCount = 0
                    Write-RelayState -InstallRoot $InstallRoot -Status "connected" -Detail "Home route is connected." -SshPid $sshProcess.Id
                    Write-RelayLog -InstallRoot $InstallRoot -Message "Relay connected."
                }
            }
        } catch {
            Write-RelayLog -InstallRoot $InstallRoot -Message $_.Exception.Message -Level "ERROR"
        } finally {
            Remove-Item -LiteralPath $sshPidPath -Force -ErrorAction SilentlyContinue
        }

        $failureCount++
        $detail = "SSH exited; retrying."
        if (Test-Path -LiteralPath $attemptErrorPath -PathType Leaf) {
            $sshError = (Get-Content -LiteralPath $attemptErrorPath -Raw -ErrorAction SilentlyContinue)
            if ($sshError) {
                $detail = ConvertTo-RelaySafeMessage -Message $sshError
                Write-RelayLog -InstallRoot $InstallRoot -Message $detail -Level "WARN"
            }
            Remove-Item -LiteralPath $attemptErrorPath -Force -ErrorAction SilentlyContinue
        }
        $delaySeconds = [Math]::Min(60, [Math]::Pow(2, [Math]::Min($failureCount, 5)))
        Write-RelayState -InstallRoot $InstallRoot -Status "retrying" -Detail $detail
        Start-Sleep -Seconds ([int]$delaySeconds)
    }
} finally {
    if ($sshProcess -and -not $sshProcess.HasExited) {
        Microsoft.PowerShell.Management\Stop-Process -Id $sshProcess.Id -Force -ErrorAction SilentlyContinue
    }
    Remove-Item -LiteralPath $runnerPidPath -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $sshPidPath -Force -ErrorAction SilentlyContinue
    Write-RelayState -InstallRoot $InstallRoot -Status "stopped" -Detail "Relay stopped."
    if ($lockStream) { $lockStream.Dispose() }
}
