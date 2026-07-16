Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"

$script:RelayTaskName = "FunPay Home Relay"
$script:RelayProductName = "FunPay Home Relay"

function Get-DefaultRelayRoot {
    Join-Path (Get-RelayLocalAppDataPath) "FunPayHomeRelay"
}

function Get-RelayLocalAppDataPath {
    [Environment]::GetFolderPath([Environment+SpecialFolder]::LocalApplicationData)
}

function Get-RelayProgramDataPath {
    [Environment]::GetFolderPath([Environment+SpecialFolder]::CommonApplicationData)
}

function Get-RelayWindowsPath {
    [IO.Directory]::GetParent([Environment]::SystemDirectory).FullName
}

function Assert-RelayWindows {
    if ($env:OS -ne "Windows_NT") {
        throw "$script:RelayProductName supports Windows only."
    }
}

function Get-RelayPowerShellPath {
    $builtIn = Join-Path (Get-RelayWindowsPath) "System32\WindowsPowerShell\v1.0\powershell.exe"
    if (-not (Test-Path -LiteralPath $builtIn -PathType Leaf)) {
        throw "The built-in Windows PowerShell 5.1 executable is missing."
    }
    $builtIn
}

function Set-RelayTrustedModulePath {
    # High-integrity controls may be launched from PowerShell 7 even though
    # ScheduledTasks is a built-in Windows PowerShell module. Resolve its only
    # allowed root from the OS itself; a caller-supplied host installation
    # directory is not a high-integrity trust boundary.
    $trusted = Join-Path (Get-RelayWindowsPath) "System32\WindowsPowerShell\v1.0\Modules"
    if (-not (Test-Path -LiteralPath $trusted -PathType Container)) {
        throw "Built-in PowerShell module directories are missing."
    }
    $env:PSModulePath = $trusted
}

function Get-RelaySshPath {
    $systemSsh = Join-Path (Get-RelayWindowsPath) "System32\OpenSSH\ssh.exe"
    if (-not (Test-Path -LiteralPath $systemSsh -PathType Leaf)) {
        throw "Windows OpenSSH Client is not installed. Install the optional capability 'OpenSSH Client' and retry."
    }
    $systemSsh
}

function Get-RelaySshKeygenPath {
    $systemKeygen = Join-Path (Get-RelayWindowsPath) "System32\OpenSSH\ssh-keygen.exe"
    if (-not (Test-Path -LiteralPath $systemKeygen -PathType Leaf)) {
        throw "ssh-keygen.exe is missing. Install the Windows OpenSSH Client optional capability and retry."
    }
    $systemKeygen
}

function Test-RelaySystemInstallRoot {
    param([Parameter(Mandatory = $true)][string]$Path)

    $programDataRoot = [IO.Path]::GetFullPath((Join-Path (Get-RelayProgramDataPath) "FunPayHomeRelay")).TrimEnd('\')
    $candidate = [IO.Path]::GetFullPath($Path).TrimEnd('\')
    $candidate.Equals($programDataRoot, [StringComparison]::OrdinalIgnoreCase)
}

function Test-RelayManualInstallRoot {
    param([Parameter(Mandatory = $true)][string]$Path)

    $localRoot = [IO.Path]::GetFullPath((Get-DefaultRelayRoot)).TrimEnd('\')
    $candidate = [IO.Path]::GetFullPath($Path).TrimEnd('\')
    $candidate.Equals($localRoot, [StringComparison]::OrdinalIgnoreCase)
}

function New-RelayDirectorySecurity {
    param([switch]$SystemInstall)

    $systemSid = New-Object Security.Principal.SecurityIdentifier("S-1-5-18")
    $administratorsSid = New-Object Security.Principal.SecurityIdentifier("S-1-5-32-544")
    $ownerSid = if ($SystemInstall) { $administratorsSid } else { [Security.Principal.WindowsIdentity]::GetCurrent().User }
    $acl = New-Object Security.AccessControl.DirectorySecurity
    $acl.SetAccessRuleProtection($true, $false)
    $acl.SetOwner($ownerSid)
    foreach ($fullControlSid in @($ownerSid, $systemSid)) {
        $rule = New-Object Security.AccessControl.FileSystemAccessRule(
            $fullControlSid,
            [Security.AccessControl.FileSystemRights]::FullControl,
            ([Security.AccessControl.InheritanceFlags]::ContainerInherit -bor [Security.AccessControl.InheritanceFlags]::ObjectInherit),
            [Security.AccessControl.PropagationFlags]::None,
            [Security.AccessControl.AccessControlType]::Allow
        )
        [void]$acl.AddAccessRule($rule)
    }
    if ($SystemInstall) {
        $usersSid = New-Object Security.Principal.SecurityIdentifier("S-1-5-32-545")
        $readRule = New-Object Security.AccessControl.FileSystemAccessRule(
            $usersSid,
            [Security.AccessControl.FileSystemRights]::ReadAndExecute,
            ([Security.AccessControl.InheritanceFlags]::ContainerInherit -bor [Security.AccessControl.InheritanceFlags]::ObjectInherit),
            [Security.AccessControl.PropagationFlags]::None,
            [Security.AccessControl.AccessControlType]::Allow
        )
        [void]$acl.AddAccessRule($readRule)
    }
    $acl
}

function Assert-RelayProtectedPath {
    param([Parameter(Mandatory = $true)][string]$Path)

    $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw "A protected relay path must not be a junction, symbolic link, or other reparse point: $Path"
    }

    $acl = Get-Acl -LiteralPath $Path
    try {
        $ownerSid = (New-Object Security.Principal.NTAccount($acl.Owner)).Translate([Security.Principal.SecurityIdentifier]).Value
    } catch {
        throw "The protected system installation directory has an untrusted owner."
    }
    if ($ownerSid -notin @("S-1-5-18", "S-1-5-32-544")) {
        throw "The protected system installation directory is not owned by SYSTEM or Administrators. Remove it after inspection and retry."
    }

    $trustedWriters = @("S-1-5-18", "S-1-5-32-544")
    $writeMask = (
        [Security.AccessControl.FileSystemRights]::Write -bor
        [Security.AccessControl.FileSystemRights]::Delete -bor
        [Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles -bor
        [Security.AccessControl.FileSystemRights]::ChangePermissions -bor
        [Security.AccessControl.FileSystemRights]::TakeOwnership
    )
    foreach ($rule in $acl.Access) {
        if ($rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow) {
            continue
        }
        try {
            $sid = $rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
        } catch {
            throw "The protected system installation directory contains an unresolvable ACL principal."
        }
        if ($sid -notin $trustedWriters -and ($rule.FileSystemRights -band $writeMask)) {
            throw "The protected system installation directory grants write access to an untrusted principal. Remove it after inspection and retry."
        }
    }
}

function Assert-RelayProtectedDirectory {
    param([Parameter(Mandatory = $true)][string]$Path)

    Assert-RelayProtectedPath -Path $Path
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    if (-not $item.PSIsContainer) {
        throw "Expected a protected directory: $Path"
    }
}

function Assert-RelayProtectedSystemDirectory {
    param([Parameter(Mandatory = $true)][string]$Path)

    if (-not (Test-RelaySystemInstallRoot -Path $Path)) {
        throw "Refusing to validate an unexpected system installation path."
    }
    Assert-RelayProtectedDirectory -Path $Path
}

function Assert-RelayProtectedSystemRuntime {
    param([Parameter(Mandatory = $true)][string]$Root)

    Assert-RelayProtectedSystemDirectory -Path $Root
    $pending = New-Object 'System.Collections.Generic.Queue[string]'
    $pending.Enqueue([IO.Path]::GetFullPath($Root))
    while ($pending.Count -gt 0) {
        $directory = $pending.Dequeue()
        foreach ($child in Get-ChildItem -LiteralPath $directory -Force -ErrorAction Stop) {
            Assert-RelayProtectedPath -Path $child.FullName
            if ($child.PSIsContainer) {
                $pending.Enqueue($child.FullName)
            }
        }
    }
}

function Assert-RelayTreeHasNoReparsePoints {
    param([Parameter(Mandatory = $true)][string]$Root)

    $rootItem = Get-Item -LiteralPath $Root -Force -ErrorAction Stop
    if (-not $rootItem.PSIsContainer -or ($rootItem.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw "Relay removal requires a real directory without reparse points."
    }
    $pending = New-Object 'System.Collections.Generic.Queue[string]'
    $pending.Enqueue($rootItem.FullName)
    while ($pending.Count -gt 0) {
        $directory = $pending.Dequeue()
        foreach ($child in Get-ChildItem -LiteralPath $directory -Force -ErrorAction Stop) {
            if ($child.Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw "Refusing recursive removal because the relay tree contains a reparse point: $($child.FullName)"
            }
            if ($child.PSIsContainer) {
                $pending.Enqueue($child.FullName)
            }
        }
    }
}

function Initialize-RelayDirectory {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [switch]$SystemInstall
    )

    if (-not $SystemInstall) {
        New-Item -ItemType Directory -Path $Path -Force | Out-Null
        Protect-RelayDirectory -Path $Path
        return
    }
    if ($PSVersionTable.PSEdition -ne "Desktop") {
        throw "Protected boot installation must run in Windows PowerShell 5.1. Use the command generated by the admin panel."
    }

    if (-not (Test-Path -LiteralPath $Path)) {
        $directorySecurity = New-RelayDirectorySecurity -SystemInstall
        # Windows PowerShell 5.1 exposes the DirectorySecurity overload.  It
        # applies the non-user-writable DACL as part of directory creation, so
        # there is no New-Item -> Set-Acl substitution window.
        [void][IO.Directory]::CreateDirectory($Path, $directorySecurity)
    }
    Assert-RelayProtectedSystemDirectory -Path $Path
    Protect-RelayDirectory -Path $Path -SystemInstall
}

function Protect-RelayDirectory {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [switch]$SystemInstall
    )

    Assert-RelayWindows
    $item = Get-Item -LiteralPath $Path -ErrorAction Stop
    if (-not $item.PSIsContainer) {
        throw "Expected a directory: $Path"
    }

    $acl = New-RelayDirectorySecurity -SystemInstall:$SystemInstall
    Set-Acl -LiteralPath $Path -AclObject $acl
}

function Protect-RelayFile {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [switch]$SystemInstall
    )

    $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    if ($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw "A protected relay file must be a regular file without reparse points: $Path"
    }

    $systemSid = New-Object Security.Principal.SecurityIdentifier("S-1-5-18")
    $administratorsSid = New-Object Security.Principal.SecurityIdentifier("S-1-5-32-544")
    $ownerSid = if ($SystemInstall) { $administratorsSid } else { [Security.Principal.WindowsIdentity]::GetCurrent().User }
    $acl = New-Object Security.AccessControl.FileSecurity
    $acl.SetAccessRuleProtection($true, $false)
    $acl.SetOwner($ownerSid)
    foreach ($fullControlSid in @($ownerSid, $systemSid)) {
        $rule = New-Object Security.AccessControl.FileSystemAccessRule(
            $fullControlSid,
            [Security.AccessControl.FileSystemRights]::FullControl,
            [Security.AccessControl.AccessControlType]::Allow
        )
        [void]$acl.AddAccessRule($rule)
    }
    Set-Acl -LiteralPath $Path -AclObject $acl
}

function Protect-RelayPrivateKey {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [switch]$SystemInstall
    )

    Protect-RelayFile -Path $Path -SystemInstall:$SystemInstall
}

function Test-RelayAdministrator {
    $principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
    $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function ConvertTo-RelaySafeMessage {
    param([AllowEmptyString()][string]$Message)

    if ($null -eq $Message) {
        return ""
    }

    $safe = $Message -replace '(?i)Bearer\s+[^\s]+', 'Bearer [redacted]'
    $safe = $safe -replace '(?i)((pairing[_ -]?code|token|password|authorization)\s*[:=]\s*)[^\s,;]+', '$1[redacted]'
    $safe.Trim()
}

function Write-RelayLog {
    param(
        [Parameter(Mandatory = $true)][string]$InstallRoot,
        [Parameter(Mandatory = $true)][string]$Message,
        [ValidateSet("INFO", "WARN", "ERROR")][string]$Level = "INFO"
    )

    $logDir = Join-Path $InstallRoot "logs"
    if (-not (Test-Path -LiteralPath $logDir)) {
        New-Item -ItemType Directory -Path $logDir -Force | Out-Null
    }
    $logPath = Join-Path $logDir "relay.log"
    if ((Test-Path -LiteralPath $logPath) -and (Get-Item -LiteralPath $logPath).Length -gt 1048576) {
        $oldPath = "$logPath.1"
        Remove-Item -LiteralPath $oldPath -Force -ErrorAction SilentlyContinue
        Move-Item -LiteralPath $logPath -Destination $oldPath -Force
    }

    $safe = ConvertTo-RelaySafeMessage -Message $Message
    Add-Content -LiteralPath $logPath -Value ("{0} [{1}] {2}" -f ([DateTime]::UtcNow.ToString("o")), $Level, $safe) -Encoding UTF8
}

function Write-RelayJsonAtomic {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)]$Value
    )

    $temporary = "$Path.$PID.tmp"
    $Value | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $temporary -Encoding UTF8
    Move-Item -LiteralPath $temporary -Destination $Path -Force
}

function Test-RelayPathInsideRoot {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$InstallRoot
    )

    $rootFull = [IO.Path]::GetFullPath($InstallRoot).TrimEnd('\') + '\'
    $pathFull = [IO.Path]::GetFullPath($Path)
    $pathFull.StartsWith($rootFull, [StringComparison]::OrdinalIgnoreCase)
}

function Read-RelayConfig {
    param([Parameter(Mandatory = $true)][string]$InstallRoot)

    $configPath = Join-Path $InstallRoot "relay.json"
    if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
        throw "Relay configuration is missing: $configPath"
    }

    $config = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
    if ([int]$config.schemaVersion -ne 1) {
        throw "Unsupported relay configuration version."
    }
    if ([string]$config.relayId -notmatch '^[A-Za-z0-9_-]{1,128}$') {
        throw "Invalid relay ID."
    }
    if ([string]$config.sshHost -notmatch '^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$') {
        throw "Invalid SSH host."
    }
    if ([int]$config.sshPort -lt 1 -or [int]$config.sshPort -gt 65535) {
        throw "Invalid SSH port."
    }
    if ([string]$config.sshUser -notmatch '^[a-z_][a-z0-9_-]{0,31}$') {
        throw "Invalid SSH user."
    }
    if ([string]$config.remoteSocksBind -ne "0.0.0.0") {
        throw "The relay may bind only to the isolated sidecar interface (0.0.0.0)."
    }
    if ([int]$config.remoteSocksPort -ne 1080) {
        throw "Unexpected remote SOCKS port."
    }
    if (-not (Test-RelayPathInsideRoot -Path ([string]$config.identityFile) -InstallRoot $InstallRoot)) {
        throw "Identity path escapes the installation directory."
    }
    if (-not (Test-RelayPathInsideRoot -Path ([string]$config.knownHostsFile) -InstallRoot $InstallRoot)) {
        throw "Known-hosts path escapes the installation directory."
    }
    if (-not (Test-Path -LiteralPath ([string]$config.identityFile) -PathType Leaf)) {
        throw "Relay identity is missing."
    }
    if (-not (Test-Path -LiteralPath ([string]$config.knownHostsFile) -PathType Leaf)) {
        throw "Pinned SSH host key is missing."
    }
    $config
}

function Quote-RelayProcessArgument {
    param([Parameter(Mandatory = $true)][AllowEmptyString()][string]$Value)

    if ($Value.Length -eq 0) {
        return '""'
    }
    if ($Value -notmatch '[\s"]') {
        return $Value
    }

    $builder = New-Object Text.StringBuilder
    [void]$builder.Append('"')
    $backslashes = 0
    foreach ($character in $Value.ToCharArray()) {
        if ($character -eq '\') {
            $backslashes++
            continue
        }
        if ($character -eq '"') {
            [void]$builder.Append(('\' * (($backslashes * 2) + 1)))
            [void]$builder.Append('"')
            $backslashes = 0
            continue
        }
        if ($backslashes -gt 0) {
            [void]$builder.Append(('\' * $backslashes))
            $backslashes = 0
        }
        [void]$builder.Append($character)
    }
    if ($backslashes -gt 0) {
        [void]$builder.Append(('\' * ($backslashes * 2)))
    }
    [void]$builder.Append('"')
    $builder.ToString()
}

function Get-RelayProcessFromPidFile {
    param(
        [Parameter(Mandatory = $true)][string]$PidPath,
        [Parameter(Mandatory = $true)][string]$RequiredCommandFragment
    )

    if (-not (Test-Path -LiteralPath $PidPath -PathType Leaf)) {
        return $null
    }
    $raw = (Get-Content -LiteralPath $PidPath -Raw).Trim()
    if ($raw -notmatch '^\d+$') {
        return $null
    }

    $process = CimCmdlets\Get-CimInstance Win32_Process -Filter ("ProcessId = {0}" -f [int]$raw) -ErrorAction SilentlyContinue
    if (
        -not $process -or
        [string]$process.CommandLine.IndexOf(
            $RequiredCommandFragment,
            [StringComparison]::OrdinalIgnoreCase
        ) -lt 0
    ) {
        return $null
    }
    $process
}

function Write-RelayState {
    param(
        [Parameter(Mandatory = $true)][string]$InstallRoot,
        [Parameter(Mandatory = $true)][ValidateSet("starting", "connected", "retrying", "stopped")][string]$Status,
        [string]$Detail = "",
        [Nullable[int]]$SshPid = $null
    )

    $state = [ordered]@{
        status = $Status
        updatedAtUtc = [DateTime]::UtcNow.ToString("o")
        runnerPid = $PID
        sshPid = $SshPid
        detail = ConvertTo-RelaySafeMessage -Message $Detail
    }
    Write-RelayJsonAtomic -Path (Join-Path $InstallRoot "state.json") -Value $state
}
