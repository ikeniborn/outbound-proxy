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
        $StateDir = $tempRoot
        $state = Join-Path $StateDir 'deployment.json'
        $script:events = @()
        $script:badIsolation = $false
        $script:badPort = 0
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
        $Action='remove'
        $null = Invoke-Deployment
        $last = $script:events.Count
        $null = Invoke-Deployment
        Assert ($script:events.Count -eq $last) 'Repeated removal changed Docker state'
        $Lan='192.0.2.1'; $Action='install'
        Expect-Failure { Invoke-Deployment } 'LAN publication is unavailable'
    }
    'Windows deployment checks passed.'
} finally { [IO.Directory]::Delete($tempRoot,$true) }
