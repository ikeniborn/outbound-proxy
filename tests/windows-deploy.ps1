#requires -Version 5.1
# Run with Windows PowerShell or PowerShell 7. The Docker commands use recorded responses.
$ErrorActionPreference = 'Stop'
. (Join-Path (Split-Path $PSScriptRoot) 'scripts/docker-proxy.ps1') status
function Assert($Condition, [string]$Message) { if (!$Condition) { throw $Message } }
function Expect-Failure([scriptblock]$Operation, [string]$Pattern) {
    try { & $Operation | Out-Null } catch {
        Assert ($_.Exception.Message -match $Pattern) "Unexpected failure: $_"
        return
    }
    throw "Expected failure matching $Pattern"
}

$repoRef = "$repository@sha256:" + ('a' * 64)
$localId = 'sha256:' + ('b' * 64)
$tempRoot = Join-Path ([IO.Path]::GetTempPath()) ([IO.Path]::GetRandomFileName())
[void][IO.Directory]::CreateDirectory($tempRoot)

try {
    & {
        $script:contextEndpoint = 'npipe:////./pipe/dockerDesktopLinuxEngine'
        $script:engineOS = 'linux'
        $script:pulls = 0
        function Invoke-Docker($Arguments) {
            switch ($Arguments[0]) {
                'context' {
                    if ($Arguments[1] -eq 'show') { return 'desktop-linux' }
                    return (ConvertTo-Json @(@{Endpoints=@{docker=@{Host=$script:contextEndpoint}}}) -Depth 5)
                }
                'info' { return (ConvertTo-Json @{OSType=$script:engineOS; OperatingSystem='Docker Desktop'; ServerVersion='29.0.0'}) }
                'compose' { return '2.40.0' }
                'pull' { $script:pulls++; return '' }
                'image' {
                    if ($Arguments -contains '--format') { return $localId }
                    return (ConvertTo-Json @(@{RepoDigests=@($repoRef)}) -Depth 4)
                }
            }
            throw "Unexpected call $Arguments"
        }
        $originalContext = $env:DOCKER_CONTEXT
        try {
            $env:DOCKER_CONTEXT = 'desktop-linux'
            Assert-Desktop
            $script:contextEndpoint = 'npipe:////remote/pipe/docker'
            Expect-Failure { Assert-Desktop } 'local.*pipe'
            $script:contextEndpoint = 'npipe:////./pipe/docker_engine'
            $script:engineOS = 'windows'
            Expect-Failure { Assert-Desktop } 'Linux containers'
        } finally { $env:DOCKER_CONTEXT = $originalContext }
        Assert ((Resolve-Image $localId) -ceq $localId) 'Local ID changed'
        Assert ($script:pulls -eq 0) 'Local ID triggered a registry pull'
        Assert ((Resolve-Image "${repository}:test") -ceq $repoRef) 'Tag did not resolve'
        Assert ((Resolve-Image $repoRef) -ceq $repoRef) 'Digest did not resolve'
        Expect-Failure { Resolve-Image 'other/image:latest' } 'reviewed tag'
    }

    & {
        $script:calls = @()
        function Invoke-Docker($Arguments, $InputText) {
            $script:calls += [pscustomobject]@{args=$Arguments -join ' '; input=$InputText}
            if ($Arguments[0] -eq 'port') { return '127.0.0.1:49155' }
            if ($Arguments -contains '-d') { return 'canary' }
            return ''
        }
        function Wait-Page($Number,$Marker) { Assert ($Number -eq 49155) 'Wrong canary port' }
        $config = [pscustomobject]@{image=$localId; port=18088; clients=$true; inventory=$true}
        Test-Isolation $config
        Assert ($script:calls.Count -eq 5) 'Unexpected canary call count'
        foreach ($i in 2..3) {
            Assert ($script:calls[$i].args -match 'engine-gateway host.docker.internal$') 'Missing Desktop gateway probe'
            Assert ($script:calls[$i].input -match 'socket.gaierror') 'Probe does not verify DNS'
        }
        Assert ($script:calls[3].args -match '--network outbound-proxy-clients') 'Missing client network probe'
        Assert ($script:calls[4].args -eq 'rm -f canary') 'Canary cleanup missing'
        Assert (!(($script:calls.args -join ' ') -match 'catalog|docker.sock')) 'Canary had deployment mounts'

        $null = Invoke-Compose $config owner @('config','--quiet')
        $override = ConvertFrom-Json $script:calls[-1].input
        Assert ($override.services.proxy.environment.OP_CLIENT_IP -eq '172.29.241.2') 'Missing client interface'
        Assert ($override.services.proxy.volumes[0].read_only) 'Inventory socket is writable'
        Assert ($override.volumes.catalog.labels.$ownerLabel -eq 'owner') 'Missing catalog owner'
        $config.clients=$false; $config.inventory=$false
        $null = Invoke-Compose $config owner @('config','--quiet')
        $override = ConvertFrom-Json $script:calls[-1].input
        Assert (!$override.services.proxy.volumes -and !$override.networks.clients) 'Optional daemon access or clients enabled'
    }

    & {
        function Invoke-Docker($Arguments) {
            if ($Arguments[0] -eq 'volume' -and $Arguments[1] -eq 'ls') { return 'outbound-proxy-catalog' }
            if ($Arguments[0] -eq 'volume') { return '[{"Labels":{}}]' }
            return ''
        }
        Expect-Failure { Assert-Ownership 'ours' } 'Conflicting volume'
    }


    & {
        $config = [pscustomobject]@{image=$localId; port=18888; lan='192.0.2.10'; subnet='192.0.2.64/26'}
        $script:testState = @{enabled=$false; running=$false; boot_enabled=$false; recovery_pending=$false;
            deployment=@{image=$localId}}
        $script:forwardChecks = 0
        function Invoke-Docker($Arguments) { return (ConvertTo-Json $script:testState -Depth 4) }
        function Test-Forwarding($Config) { $script:forwardChecks++ }
        Test-Operator $config 'container' 1
        Assert ($script:forwardChecks -eq 0) 'Stopped service generated forwarding traffic'
        $script:testState.enabled=$true; $script:testState.boot_enabled=$true
        Expect-Failure { Test-Operator $config 'container' 1 } 'inconsistent'
        $script:testState.running=$true
        Test-Operator $config 'container' 1
        Assert ($script:forwardChecks -eq 1) 'Enabled service skipped the fresh HTTPS check'
        $script:testState.recovery_pending=$true
        Expect-Failure { Test-Operator $config 'container' 1 } 'pending recovery'
        $script:testState.Remove('running')
        Expect-Failure { Test-Operator $config 'container' 1 } 'incomplete runtime status'
    }

    & {
        $config = [pscustomobject]@{port=18888; lan='192.0.2.10'; subnet='192.0.2.64/26'}
        function Get-NetIPAddress { [pscustomobject]@{AddressState='Preferred'; InterfaceIndex=5; PrefixLength=24} }
        function Get-NetAdapter { [pscustomobject]@{Name='Ethernet'; Status='Up'; HardwareInterface=$true} }
        $scope = Get-LanScope $config
        Assert (($scope.block -join ',') -eq '0.0.0.0-192.0.2.63,192.0.2.128-255.255.255.255') 'IPv4 complement is incomplete'
        Assert ((Get-RangeKeys '192.0.2.64/255.255.255.192') -eq (Get-RangeKeys '192.0.2.64/26')) 'Mask normalization differs'
        $config.subnet='192.0.2.1/24'
        Expect-Failure { Get-LanScope $config } 'network address'
        $config.subnet='192.0.0.0/16'
        Expect-Failure { Get-LanScope $config } 'fit within'
        $config.subnet='192.0.2.64/26'
        $script:rules = @{}
        $script:profileEnabled = 'True'
        $script:wrongScope = $false
        $script:enforcement = 'Full'
        function Get-CimInstance { foreach ($name in @('BFE','MpsSvc')) { [pscustomobject]@{Name=$name; State='Running'; StartMode='Auto'} } }
        function Get-NetFirewallProfile {
            foreach ($name in @('Domain','Private','Public')) {
                [pscustomobject]@{Name=$name; Enabled=$script:profileEnabled; AllowLocalFirewallRules='True'; DisabledInterfaceAliases=@()}
            }
        }
        function Get-NetFirewallSecurityFilter { return }
        function New-NetFirewallRule($Name,$Group,$Enabled,$Direction,$Profile,$Action,$RemoteAddress,$InterfaceAlias,$LocalAddress,$LocalPort,$Protocol,$PolicyStore) {
            Assert ($PolicyStore -eq 'PersistentStore') 'Rules must persist across Windows restarts'
            $script:rules[$Name]=[pscustomobject]@{Name=$Name; Group=$Group; Enabled=$Enabled; Direction=$Direction; Profile=$Profile; Action=$Action;
                PolicyStoreSourceType='Local'; PrimaryStatus='OK'; EnforcementStatus='Full'; LocalAddress=$LocalAddress; RemoteAddress=$RemoteAddress;
                LocalPort=$LocalPort; Protocol=$Protocol; InterfaceAlias=$(if ($InterfaceAlias) {$InterfaceAlias} else {'Any'})}
        }
        function Get-NetFirewallRule($Name,$PolicyStore) {
            if ($PolicyStore -eq 'PersistentStore') { return $script:rules.Values }
            $rule = $script:rules[$Name]
            $rule.EnforcementStatus=$script:enforcement
            return $rule
        }
        function Get-NetFirewallAddressFilter { process {
            [pscustomobject]@{LocalAddress=$_.LocalAddress; RemoteAddress=$(if ($script:wrongScope) {'0.0.0.0/0'} else {$_.RemoteAddress})}
        } }
        function Get-NetFirewallPortFilter { process { [pscustomobject]@{LocalPort=$_.LocalPort; RemotePort='Any'; Protocol=$_.Protocol} } }
        function Get-NetFirewallApplicationFilter { process { [pscustomobject]@{Program='Any'} } }
        function Get-NetFirewallServiceFilter { process { [pscustomobject]@{Service='Any'} } }
        function Get-NetFirewallInterfaceFilter { process { [pscustomobject]@{InterfaceAlias=$_.InterfaceAlias} } }
        function Remove-NetFirewallRule { process { $script:rules.Remove($_.Name) } }
        Install-LanPolicy $config 'test'
        Assert ($script:rules.Count -eq 2) 'Expected two narrow persistent rules'
        Assert ($script:rules['outbound-proxy-test-allow'].InterfaceAlias -eq 'Ethernet') 'Allow rule bound wrong adapter'
        $script:profileEnabled='False'
        Expect-Failure { Assert-LanPolicy $config 'test' } 'cannot enforce'
        $script:profileEnabled='True'; $script:wrongScope=$true
        Expect-Failure { Assert-LanPolicy $config 'test' } 'scope differs'
        $script:wrongScope=$false; $script:enforcement='LocalFirewallRulesDisallowed'
        Expect-Failure { Assert-LanPolicy $config 'test' } 'does not enforce'
        $script:rules['outbound-proxy-test-allow'].Group='foreign'
        Expect-Failure { Remove-LanPolicy 'test' } 'ownership conflict'
        Assert ($script:rules.Count -eq 2) 'Conflicting rule caused partial cleanup'
        $script:rules['outbound-proxy-test-allow'].Group='outbound-proxy-test'
        Remove-LanPolicy 'test'
        Assert ($script:rules.Count -eq 0) 'Owned rules survived removal'
    }

    & {
        $StateDir = $tempRoot
        $state = Join-Path $StateDir 'deployment.json'
        $script:events = @()
        $script:badIsolation = $false
        $script:badPort = 0
        $script:rejectLanCanary = $false
        function Assert-LanAdministrator {}
        function Get-LanScope($Config) { return @{} }
        function Assert-FirewallProfiles($Config,$Scope) {}
        function Remove-LanPolicy($Owner) { $script:events += 'firewall:remove' }
        function Install-LanPolicy($Config,$Owner) { $script:events += 'firewall:install' }
        function Test-LanListener($Config) {
            $script:events += 'lan:canary'
            if ($script:rejectLanCanary) { throw 'unverified LAN backend' }
        }
        function Assert-Desktop {}
        function Assert-Ownership($Owner) { Assert (![string]::IsNullOrEmpty($Owner)) 'Missing owner' }
        function Assert-FreePort($Number) { if ($Number -eq 19997) { throw 'occupied' } }
        function Resolve-Image($Reference) { if ($Reference -eq 'missing') { throw 'pull failed' }; return $localId }
        function Invoke-Compose($Config,$Owner,$Commands) {
            $script:events += "$($Commands[0]):$($Config.port)"
            return ''
        }
        function Test-Isolation($Config) { if ($script:badIsolation) { throw 'reachable through host gateway' } }
        function Test-Service($Config,$Owner) { if ($script:badPort -eq $Config.port) { throw 'failed verification' } }

        $Action='install'; $Image=$localId; $Port=18888
        $null = Invoke-Deployment
        $initial = [IO.File]::ReadAllText($state)
        $Action='reconfigure'; $Image=$null; $Port=19997
        Expect-Failure { Invoke-Deployment } 'occupied'
        $Port=19999; $Image='missing'
        Expect-Failure { Invoke-Deployment } 'pull failed'
        $Image=$null; $script:badIsolation=$true
        $before = $script:events.Count
        Expect-Failure { Invoke-Deployment } 'reachable through host gateway'
        Assert ($script:events.Count -eq $before+1 -and $script:events[-1] -eq 'config:19999') 'Canary failure changed container'
        Assert ([IO.File]::ReadAllText($state) -ceq $initial) 'Preflight changed committed state'
        $script:badIsolation=$false; $script:badPort=19999
        Expect-Failure { Invoke-Deployment } 'failed verification'
        $saved = ConvertFrom-Json ([IO.File]::ReadAllText($state))
        Assert ($saved.active.port -eq 18888 -and !$saved.pending) 'Failed update did not restore previous state'
        Assert ($script:events[-1] -eq 'up:18888') 'Previous service was not restored'
        $script:badPort=0
        $null = Invoke-Deployment
        $Port=0; $Action='rollback'
        $null = Invoke-Deployment
        $Action='status'
        $status = ConvertFrom-Json (Invoke-Deployment)
        Assert ($status.settings.port -eq 18888 -and !$status.recovery_required) 'Explicit rollback failed'
        $saved = ConvertFrom-Json ([IO.File]::ReadAllText($state))
        $saved.pending = $saved.previous
        Save-Record $saved $state
        $status = ConvertFrom-Json (Invoke-Deployment)
        Assert ($status.recovery_required) 'Interrupted update not reported'
        $Action='reconfigure'
        $null = Invoke-Deployment
        Assert (!(ConvertFrom-Json ([IO.File]::ReadAllText($state))).pending) 'Interrupted update not recovered'
        $Lan='192.0.2.10'; $Subnet='192.0.2.64/26'; $Action='reconfigure'; $script:rejectLanCanary=$true
        Expect-Failure { Invoke-Deployment } 'unverified LAN backend'
        Assert (!(ConvertFrom-Json ([IO.File]::ReadAllText($state))).active.lan) 'Failed canary committed LAN settings'
        $script:rejectLanCanary=$false
        $start=$script:events.Count
        $null = Invoke-Deployment
        $changes = $script:events[$start..($script:events.Count-1)] -join ','
        Assert ($changes -eq 'config:18888,down:18888,firewall:remove,firewall:install,lan:canary,up:18888') 'Catalog started before the firewall and LAN canary'
        $Action='remove'; $Lan=$null; $Subnet=$null
        $null = Invoke-Deployment
        Assert ($script:events[-2] -eq 'down:18888' -and $script:events[-1] -eq 'firewall:remove') 'Firewall removed while port was still published'
        $last = $script:events.Count
        $null = Invoke-Deployment
        Assert ($script:events.Count -eq $last) 'Repeated removal changed Docker state'
        $Lan='192.0.2.1'; $Action='install'
        Expect-Failure { Invoke-Deployment } 'Specify both -Lan and -Subnet'
    }
    'Windows deployment checks passed.'
} finally { [IO.Directory]::Delete($tempRoot,$true) }
