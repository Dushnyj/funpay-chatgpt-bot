[CmdletBinding()]
param()

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$failures = New-Object Collections.Generic.List[string]

function Assert-RelayAsset {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { $script:failures.Add($Message) }
}

$scripts = Get-ChildItem -LiteralPath $root -Filter "*.ps1" -File
foreach ($scriptFile in $scripts) {
    $tokens = $null
    $errors = $null
    [void][Management.Automation.Language.Parser]::ParseFile($scriptFile.FullName, [ref]$tokens, [ref]$errors)
    Assert-RelayAsset ($errors.Count -eq 0) ("PowerShell parse errors in {0}: {1}" -f $scriptFile.Name, (($errors | ForEach-Object Message) -join "; "))
}

$runner = Get-Content -LiteralPath (Join-Path $root "Relay.ps1") -Raw
$installer = Get-Content -LiteralPath (Join-Path $root "Install.ps1") -Raw
$common = Get-Content -LiteralPath (Join-Path $root "Common.ps1") -Raw
$start = Get-Content -LiteralPath (Join-Path $root "Start.ps1") -Raw
$stop = Get-Content -LiteralPath (Join-Path $root "Stop.ps1") -Raw
$status = Get-Content -LiteralPath (Join-Path $root "Status.ps1") -Raw
$uninstall = Get-Content -LiteralPath (Join-Path $root "Uninstall.ps1") -Raw
$readme = Get-Content -LiteralPath (Join-Path $root "README.md") -Raw
$runbook = Get-Content -LiteralPath (Join-Path $root "..\..\docs\windows-home-relay.md") -Raw

Assert-RelayAsset ($runner.Contains('"-R", $forward')) "Runner must use an explicit remote dynamic forward."
Assert-RelayAsset ($runner.Contains('"StrictHostKeyChecking=yes"')) "Runtime SSH must require a pinned host key."
Assert-RelayAsset ($runner.Contains('"ExitOnForwardFailure=yes"')) "Runtime SSH must fail when forwarding cannot be established."
Assert-RelayAsset ($runner.Contains('"ServerAliveInterval=30"')) "Runtime SSH needs keepalives."
Assert-RelayAsset (-not $runner.Contains("StrictHostKeyChecking=no")) "Runtime must never disable host-key checking."
Assert-RelayAsset ($installer.Contains('"-t", "ed25519"')) "Installer must generate a local Ed25519 key."
Assert-RelayAsset ($installer.Contains('Authorization = "Bearer $Code"')) "Pairing must use the one-time bearer token."
Assert-RelayAsset (-not $installer.Contains("private_key")) "Pairing must never send or receive a private key."
Assert-RelayAsset ($installer.Contains('$reEnrollRequested')) "Existing installations must distinguish repair from a new enrollment."
Assert-RelayAsset ($installer.Contains('Get-RelayPublicKeyFromIdentity')) "Re-enrollment must reuse the locally held public identity."
Assert-RelayAsset ($installer.Contains('Get-OrCreateRelayIdentity')) "A partial first installation must reuse its valid local identity instead of invoking an overwrite prompt."
Assert-RelayAsset ($installer.Contains('does not match its private key. Refusing to overwrite either file')) "Identity recovery must reject a mismatched public key without overwriting it."
Assert-RelayAsset (-not $installer.Contains('& $keygenPath @keygenArguments')) "Installer must not rely on PowerShell native empty-argument forwarding for ssh-keygen."
Assert-RelayAsset ($installer.Contains('$keygenProcess = Start-Process -FilePath $keygenPath')) "Installer must launch ssh-keygen with an explicit, safely quoted argument line."
Assert-RelayAsset ($installer.Contains('[IO.File]::WriteAllBytes($configPath, $oldConfigBytes)')) "A failed re-enrollment must restore the last configuration."
$existingInstallIndex = $installer.IndexOf('if (Test-Path -LiteralPath $configPath -PathType Leaf)')
$reEnrollIndex = $installer.IndexOf('if ($reEnrollRequested)', $existingInstallIndex)
$reEnrollInvokeIndex = $installer.IndexOf('Invoke-RelayPairing -Url $PairingUrl', $reEnrollIndex)
$existingExitIndex = $installer.IndexOf('exit 0', $reEnrollIndex)
Assert-RelayAsset ($existingInstallIndex -ge 0 -and $reEnrollIndex -gt $existingInstallIndex -and $reEnrollInvokeIndex -gt $reEnrollIndex -and $existingExitIndex -gt $reEnrollInvokeIndex) "Existing-install branch must consume a new pairing before its early exit."
$enrollmentConfigStart = $installer.IndexOf('function Set-RelayEnrollmentConfiguration')
$enrollmentConfigEnd = $installer.IndexOf('function Test-RelayEnrollment', $enrollmentConfigStart)
$enrollmentConfigBody = $installer.Substring($enrollmentConfigStart, $enrollmentConfigEnd - $enrollmentConfigStart)
Assert-RelayAsset (-not $enrollmentConfigBody.Contains('PairingCode') -and -not $enrollmentConfigBody.Contains('Authorization')) "Enrollment configuration must never persist setup credentials."
Assert-RelayAsset ($installer.Contains("New-ScheduledTaskTrigger -AtStartup")) "Autostart must run at Windows startup, not only at logon."
Assert-RelayAsset ($installer.Contains('-UserId "SYSTEM"')) "The boot task must use the local SYSTEM service account."
Assert-RelayAsset ($installer.Contains('Get-RelayProgramDataPath')) "Boot runtime must use the OS-derived ProgramData path."
Assert-RelayAsset ($installer.Contains('$EnableAutoStart -and -not $isSystemInstall')) "Autostart must reject every non-canonical install root."
Assert-RelayAsset ($installer.Contains('Assert-RelayProtectedSystemRuntime')) "Repairs must validate protected child runtime files before copying or executing them."
Assert-RelayAsset ($installer.Contains('^FunPayHomeRelay-Staging-[a-f0-9]{32}$')) "Elevated installer must accept only the generated canonical staging ancestor."
Assert-RelayAsset ($installer.IndexOf('Assert-SetupBootstrapPath -Path (Join-Path $PSScriptRoot "Common.ps1")') -lt $installer.IndexOf('. (Join-Path $PSScriptRoot "Common.ps1")')) "Install.ps1 and Common.ps1 must be ACL-validated before Common is imported."
Assert-RelayAsset ($installer.Contains('$isManualUserInstall -and $isAdministrator')) "Manual mode must not execute user-writable runtime files elevated."
Assert-RelayAsset ($installer.Contains('[IO.Directory]::GetParent([Environment]::SystemDirectory).FullName') -and $installer.Contains('System32\WindowsPowerShell\v1.0\Modules')) "Elevated setup must use only the OS-derived Windows module root."
Assert-RelayAsset ($installer.Contains('CommonApplicationData is not owned by SYSTEM or Administrators')) "Elevated setup must reject a ProgramData parent controlled by an untrusted owner."
Assert-RelayAsset ($installer.IndexOf('$bootstrapAdministrator') -lt $installer.IndexOf('. (Join-Path $PSScriptRoot "Common.ps1")')) "Elevated source validation must happen before Common.ps1 is imported."
Assert-RelayAsset (-not $installer.Contains('Join-Path $localInstallRoot "Stop.ps1"')) "Elevated migration must never execute the user-writable old Stop.ps1."
Assert-RelayAsset (-not $installer.Contains('Remove-Item -LiteralPath $localInstallRoot -Recurse')) "Elevated migration must not recursively delete the user-writable old tree."
Assert-RelayAsset ($installer.Contains("Protect-RelayPrivateKey")) "The generated private key needs a dedicated ACL."
Assert-RelayAsset ($installer.Contains('Repair-RelaySystemPublicKeyProtection -Root $InstallRoot')) "A retry must safely repair the ssh-keygen-created public-key owner before validating the complete runtime."
Assert-RelayAsset ($installer.Contains('Protect-RelayIdentityFiles -IdentityPath $IdentityPath -SystemInstall:$SystemInstall')) "Both halves of every generated or reused relay identity must receive protected ACLs."
$newIdentityBranch = $installer.Substring($installer.IndexOf('$keygenProcess = Start-Process -FilePath $keygenPath'), $installer.IndexOf('function Set-RelayEnrollmentConfiguration') - $installer.IndexOf('$keygenProcess = Start-Process -FilePath $keygenPath'))
Assert-RelayAsset ($newIdentityBranch.IndexOf('Protect-RelayIdentityFiles -IdentityPath $IdentityPath') -lt $newIdentityBranch.IndexOf('Get-RelayPublicKeyFromIdentity -IdentityPath $IdentityPath')) "A newly generated private key must be ACL-protected before it is read or derived."
$protectGeneratedIdentityIndex = $newIdentityBranch.IndexOf('Protect-RelayIdentityFiles -IdentityPath $generationIdentityPath')
$deriveGeneratedIdentityIndex = $newIdentityBranch.IndexOf('Get-RelayPublicKeyFromIdentity -IdentityPath $generationIdentityPath')
Assert-RelayAsset ($protectGeneratedIdentityIndex -ge 0 -and $deriveGeneratedIdentityIndex -gt $protectGeneratedIdentityIndex) "The protected-staging identity must receive its ACL before ssh-keygen output is read."
Assert-RelayAsset ($installer.Contains("RelayIdentity-{0}") -and $installer.Contains("O:BAG:BAD:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)")) "System identities must be generated in a random installer child that only SYSTEM and Administrators can access."
Assert-RelayAsset ($installer.Contains('Move-Item -LiteralPath $generationIdentityPath -Destination $IdentityPath')) "Only the already protected private key may be moved into the runtime root."
Assert-RelayAsset ($common.Contains('function Protect-RelayFile')) "All relay key files need a reparse-safe ACL helper."
Assert-RelayAsset ($common.Contains('A protected relay file must be a regular file without reparse points')) "The key-file ACL helper must reject reparse points before Set-Acl."
Assert-RelayAsset (-not $installer.Contains("-Verb RunAs")) "The downloaded installer must never self-elevate from a user-writable directory."
Assert-RelayAsset ($installer.Contains("will not self-elevate")) "The installer must clearly explain safe Administrator PowerShell startup."
Assert-RelayAsset (-not $installer.Contains("New-ScheduledTaskTrigger -AtLogOn")) "Boot mode must not be mislabeled logon autostart."
Assert-RelayAsset ($common.Contains('$safe = $safe -replace')) "Logs must pass through secret redaction."
Assert-RelayAsset ($common.Contains('S-1-5-18') -and $common.Contains('S-1-5-32-544')) "System install ACL must name SYSTEM and Administrators explicitly."
Assert-RelayAsset ($common.Contains('[Environment+SpecialFolder]::CommonApplicationData') -and $common.Contains('[Environment]::SystemDirectory')) "Trusted Windows roots must come from OS APIs, not mutable environment variables."
Assert-RelayAsset (-not $common.Contains('$env:ProgramData') -and -not $common.Contains('$env:WINDIR') -and -not $common.Contains('$env:LOCALAPPDATA')) "Trusted runtime roots must not use mutable environment variables."
Assert-RelayAsset ($common.Contains('function Set-RelayTrustedModulePath')) "Elevated PowerShell 7 controls need a trusted machine-only module path."
Assert-RelayAsset ($common.Contains('System32\WindowsPowerShell\v1.0\Modules')) "Trusted module path must retain built-in Windows modules such as ScheduledTasks."
Assert-RelayAsset (-not $common.Contains('$PSHOME')) "High-integrity controls must not trust a caller-selected PowerShell home."
foreach ($controlScript in @($start, $stop, $status, $uninstall)) {
    Assert-RelayAsset ($controlScript.Contains('Set-RelayTrustedModulePath')) "Every elevated control must support built-in Windows modules from PowerShell 7."
    Assert-RelayAsset (-not $controlScript.Contains('$env:PSModulePath = Join-Path $PSHOME "Modules"')) "Controls must not hide Windows modules when launched from PowerShell 7."
    Assert-RelayAsset ($controlScript.IndexOf('Set-RelayTrustedModulePath') -lt $controlScript.IndexOf('Get-ScheduledTask')) "Trusted module roots must be set before any task-scheduler command resolves."
}
Assert-RelayAsset (-not $common.Contains('Get-Command ssh.exe') -and -not $common.Contains('Get-Command ssh-keygen.exe')) "Runtime SSH binaries must resolve only to the built-in Windows OpenSSH path."
Assert-RelayAsset ($common.Contains('Assert-RelayTreeHasNoReparsePoints')) "Recursive removal must reject every reparse point."
foreach ($controlScript in @($start, $stop, $status, $uninstall, $runner)) {
    Assert-RelayAsset ($controlScript.IndexOf('$canonicalScript') -lt $controlScript.IndexOf('. (Join-Path $requestedRoot "Common.ps1")')) "Control scripts must validate their canonical path before importing Common.ps1."
    Assert-RelayAsset ($controlScript.IndexOf('Assert-BootstrapProtectedPath -Path (Join-Path $requestedRoot "Common.ps1")') -lt $controlScript.IndexOf('. (Join-Path $requestedRoot "Common.ps1")')) "Control scripts must inline-validate root/script/Common before dot-source."
    Assert-RelayAsset ($controlScript.Contains('DeleteSubdirectoriesAndFiles')) "Bootstrap ACL checks must reject parent/child replacement rights."
    Assert-RelayAsset (-not $controlScript.Contains('[Security.AccessControl.FileSystemRights]::Modify')) "Bootstrap ACL checks must not put the composite Modify right into a write bitmask."
}
Assert-RelayAsset (-not $common.Contains('[Security.AccessControl.FileSystemRights]::Modify')) "The protected-path ACL check must not put the composite Modify right into a write bitmask."
Assert-RelayAsset (-not $installer.Contains('[Security.AccessControl.FileSystemRights]::Modify')) "Installer bootstrap ACL checks must not put the composite Modify right into a write bitmask."
Assert-RelayAsset ($uninstall.Contains('Assert-RelayTreeHasNoReparsePoints')) "Uninstall must reject reparse points before recursive deletion."
Assert-RelayAsset ($uninstall.Contains('$task = if ($isSystemRoot)')) "Manual uninstall must never manage the global SYSTEM task."
Assert-RelayAsset (-not $uninstall.Contains('Test-RelayPathInsideRoot -Path (Join-Path $InstallRoot "relay_ed25519")')) "Uninstall must not rely on a tautological caller-root check."
Assert-RelayAsset (-not $uninstall.Contains('Remove-Item -LiteralPath $startMenuFolder -Recurse')) "Uninstall must not recursively delete a user-controlled Start Menu tree."
Assert-RelayAsset ($installer.Contains('if ($SystemInstall)') -and $installer.Contains('no elevated writes are made to user-writable Start Menu')) "Boot install must not write shortcuts into user-writable shell folders."
Assert-RelayAsset ($readme.Contains("1080") -and $readme.Contains("никогда не публикуется")) "README must forbid publishing SOCKS 1080."
Assert-RelayAsset ($runbook.Contains('socks5://home-relay:1080')) "Runbook must identify the backend-only SOCKS endpoint."
Assert-RelayAsset ($runbook.Contains("fail closed")) "Runbook must require fail-closed routing."

if ($env:OS -eq "Windows_NT") {
    $previousModulePath = $env:PSModulePath
    try {
        . (Join-Path $root "Common.ps1")
        Set-RelayTrustedModulePath
        $expectedModulePath = Join-Path ([IO.Directory]::GetParent([Environment]::SystemDirectory).FullName) "System32\WindowsPowerShell\v1.0\Modules"
        Assert-RelayAsset ($env:PSModulePath -eq $expectedModulePath) "Trusted module path must contain exactly the OS-derived Windows module root."
        Assert-RelayAsset ($null -ne (Get-Command Get-ScheduledTask -ErrorAction SilentlyContinue)) "ScheduledTasks must resolve after trusted-path hardening."
        Assert-RelayAsset ($null -ne (Get-Command Get-CimInstance -ErrorAction SilentlyContinue)) "CimCmdlets must resolve after trusted-path hardening."
    } finally {
        $env:PSModulePath = $previousModulePath
    }
}

# FileSystemRights.Modify is a composite value that contains read/execute bits.
# Adding it to a bitmask therefore misclassifies the intended Users
# ReadAndExecute rule as writable. The mask must instead name only the concrete
# mutation rights while still matching the real Modify permission.
$aclWriteMask = (
    [Security.AccessControl.FileSystemRights]::Write -bor
    [Security.AccessControl.FileSystemRights]::Delete -bor
    [Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles -bor
    [Security.AccessControl.FileSystemRights]::ChangePermissions -bor
    [Security.AccessControl.FileSystemRights]::TakeOwnership
)
Assert-RelayAsset (([Security.AccessControl.FileSystemRights]::ReadAndExecute -band $aclWriteMask) -eq 0) "Users ReadAndExecute must not be classified as relay write access."
Assert-RelayAsset (([Security.AccessControl.FileSystemRights]::Modify -band $aclWriteMask) -ne 0) "A real Modify rule must still be classified as relay write access."
$readOnlyRights = @(
    [Security.AccessControl.FileSystemRights]::ReadData,
    [Security.AccessControl.FileSystemRights]::ReadExtendedAttributes,
    [Security.AccessControl.FileSystemRights]::ReadAttributes,
    [Security.AccessControl.FileSystemRights]::ReadPermissions,
    [Security.AccessControl.FileSystemRights]::ExecuteFile,
    [Security.AccessControl.FileSystemRights]::Synchronize,
    [Security.AccessControl.FileSystemRights]::Read,
    [Security.AccessControl.FileSystemRights]::ReadAndExecute
)
foreach ($right in $readOnlyRights) {
    Assert-RelayAsset (($right -band $aclWriteMask) -eq 0) "A read-only ACL right must not be classified as relay write access: $right"
}
$mutationRights = @(
    [Security.AccessControl.FileSystemRights]::WriteData,
    [Security.AccessControl.FileSystemRights]::AppendData,
    [Security.AccessControl.FileSystemRights]::WriteExtendedAttributes,
    [Security.AccessControl.FileSystemRights]::WriteAttributes,
    [Security.AccessControl.FileSystemRights]::Delete,
    [Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles,
    [Security.AccessControl.FileSystemRights]::ChangePermissions,
    [Security.AccessControl.FileSystemRights]::TakeOwnership,
    [Security.AccessControl.FileSystemRights]::Modify,
    [Security.AccessControl.FileSystemRights]::FullControl
)
foreach ($right in $mutationRights) {
    Assert-RelayAsset (($right -band $aclWriteMask) -ne 0) "A mutating ACL right must be classified as relay write access: $right"
}

# Exercise the enrollment-config writer independently from network/UAC actions.
# This verifies that an already present relay.json can be replaced by a new
# relay_id and pinned host key without putting setup credentials into the file.
. (Join-Path $root "Common.ps1")
$installTokens = $null
$installErrors = $null
$installAst = [Management.Automation.Language.Parser]::ParseFile((Join-Path $root "Install.ps1"), [ref]$installTokens, [ref]$installErrors)
$neededFunctions = @(
    "Get-RelayPublicKeyFromIdentity",
    "Get-OrCreateRelayIdentity",
    "Get-RequiredRelayProperty",
    "Protect-RelayIdentityFiles",
    "Set-RelayEnrollmentConfiguration"
)
foreach ($functionName in $neededFunctions) {
    $definition = $installAst.FindAll({
        param($node)
        $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $functionName
    }, $true) | Select-Object -First 1
    Assert-RelayAsset ($null -ne $definition) "Missing enrollment helper: $functionName"
    if ($definition) { . ([scriptblock]::Create($definition.Extent.Text)) }
}

$retryRoot = Join-Path $env:TEMP ("funpay-relay-retry-validator-{0}" -f [Guid]::NewGuid().ToString("N"))
try {
    New-Item -ItemType Directory -Path $retryRoot | Out-Null
    $retryIdentity = Join-Path $retryRoot "relay_ed25519"
    $firstRetryPublicKey = Get-OrCreateRelayIdentity -Root $retryRoot -IdentityPath $retryIdentity
    $firstRetryPrivateHash = (Get-FileHash -LiteralPath $retryIdentity -Algorithm SHA256).Hash

    # This is the state left when enrollment fails after ssh-keygen but before
    # relay.json is written. A retry must preserve the private key, including
    # when a crash left only the private half of the pair.
    Remove-Item -LiteralPath "$retryIdentity.pub" -Force
    $secondRetryPublicKey = Get-OrCreateRelayIdentity -Root $retryRoot -IdentityPath $retryIdentity
    $secondRetryPrivateHash = (Get-FileHash -LiteralPath $retryIdentity -Algorithm SHA256).Hash
    Assert-RelayAsset ($secondRetryPublicKey -eq $firstRetryPublicKey) "Retry must derive the same public key from the partial private identity."
    Assert-RelayAsset ($secondRetryPrivateHash -eq $firstRetryPrivateHash) "Retry must not overwrite the partial private identity."

    $mismatchedPublicKey = "ssh-ed25519 AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
    Set-Content -LiteralPath "$retryIdentity.pub" -Value $mismatchedPublicKey -Encoding ASCII
    $privateHashBeforeMismatch = (Get-FileHash -LiteralPath $retryIdentity -Algorithm SHA256).Hash
    $publicHashBeforeMismatch = (Get-FileHash -LiteralPath "$retryIdentity.pub" -Algorithm SHA256).Hash
    $mismatchRejected = $false
    try {
        [void](Get-OrCreateRelayIdentity -Root $retryRoot -IdentityPath $retryIdentity)
    } catch {
        $mismatchRejected = $_.Exception.Message -like "*does not match its private key*"
    }
    Assert-RelayAsset $mismatchRejected "Retry must reject a public key that does not match the private identity."
    Assert-RelayAsset ((Get-FileHash -LiteralPath $retryIdentity -Algorithm SHA256).Hash -eq $privateHashBeforeMismatch) "Rejected recovery must not overwrite the private identity."
    Assert-RelayAsset ((Get-FileHash -LiteralPath "$retryIdentity.pub" -Algorithm SHA256).Hash -eq $publicHashBeforeMismatch) "Rejected recovery must not overwrite the public identity."
} catch {
    $failures.Add("Partial-identity retry exercise failed: $($_.Exception.Message)")
} finally {
    Remove-Item -LiteralPath $retryRoot -Recurse -Force -ErrorAction SilentlyContinue
}

$testRoot = Join-Path $env:TEMP ("funpay-relay-validator-{0}" -f [Guid]::NewGuid().ToString("N"))
try {
    New-Item -ItemType Directory -Path $testRoot | Out-Null
    $testIdentity = Join-Path $testRoot "relay_ed25519"
    New-Item -ItemType File -Path $testIdentity | Out-Null
    $firstPairing = [pscustomobject]@{
        schema_version = 1
        relay_id = "relay-1-aaaaaaaaaaaa"
        display_name = "Home"
        ssh_host = "funpay-bot.duckdns.org"
        ssh_port = 2222
        ssh_user = "relay"
        remote_socks_bind = "0.0.0.0"
        remote_socks_port = 1080
        host_key = [pscustomobject]@{ type = "ssh-ed25519"; data = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=" }
    }
    [void](Set-RelayEnrollmentConfiguration -Pairing $firstPairing -Root $testRoot -IdentityPath $testIdentity -CreatedAtUtc "2026-01-01T00:00:00Z")
    $secondPairing = $firstPairing.PSObject.Copy()
    $secondPairing.relay_id = "relay-2-bbbbbbbbbbbb"
    $secondPairing.host_key = [pscustomobject]@{ type = "ssh-ed25519"; data = "BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB=" }
    [void](Set-RelayEnrollmentConfiguration -Pairing $secondPairing -Root $testRoot -IdentityPath $testIdentity -CreatedAtUtc "2026-01-01T00:00:00Z")
    $updatedConfigText = Get-Content -LiteralPath (Join-Path $testRoot "relay.json") -Raw
    $updatedConfig = $updatedConfigText | ConvertFrom-Json
    $updatedKnownHost = Get-Content -LiteralPath (Join-Path $testRoot "known_hosts") -Raw
    Assert-RelayAsset ($updatedConfig.relayId -eq "relay-2-bbbbbbbbbbbb") "Re-enrollment must replace relay_id."
    Assert-RelayAsset ($updatedKnownHost.Contains("BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB=")) "Re-enrollment must replace the pinned host key."
    Assert-RelayAsset (-not $updatedConfigText.Contains("setup-token")) "relay.json must not contain setup credentials."
} catch {
    $failures.Add("Enrollment configuration exercise failed: $($_.Exception.Message)")
} finally {
    Remove-Item -LiteralPath $testRoot -Recurse -Force -ErrorAction SilentlyContinue
}

if ($failures.Count -gt 0) {
    $failures | ForEach-Object { Write-Error $_ }
    exit 1
}
Write-Host ("Validated {0} PowerShell scripts and relay security invariants." -f $scripts.Count)
