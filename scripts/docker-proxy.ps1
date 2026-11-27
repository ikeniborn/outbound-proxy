#requires -Version 5.1
[CmdletBinding()]
param(
    [Parameter(Mandatory, Position=0)]
    [ValidateSet('install','reconfigure','status','rollback','remove')] [string]$Action,
    [string]$Image,
    [ValidateRange(1024,65535)] [int]$Port,
    [switch]$Clients, [switch]$NoClients, [switch]$Inventory, [switch]$NoInventory,
    [string]$Lan, [string]$Subnet, [switch]$LocalOnly,
    [string]$StateDir = (Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'outbound-proxy')
)

$ErrorActionPreference = 'Stop'
$project = 'outbound-proxy'
$repository = 'ghcr.io/ikeniborn/outbound-proxy'
$ownerLabel = 'io.github.ikeniborn.outbound-proxy.owner'
$clientNetwork = 'outbound-proxy-clients'
$composeFile = Join-Path (Split-Path $PSScriptRoot) 'compose.yaml'
. (Join-Path $PSScriptRoot 'windows-lan.ps1')

function Invoke-Docker([string[]]$Arguments, [string]$InputText) {
    if ($PSBoundParameters.ContainsKey('InputText')) {
        $output = $InputText | & docker @Arguments
    } else {
        $output = & docker @Arguments
    }
    if ($LASTEXITCODE -ne 0) { throw "Docker $($Arguments[0]) exited $LASTEXITCODE." }
    return ($output -join "`n").Trim()
}

function Get-Inspection([string[]]$Arguments) {
    $items = @(ConvertFrom-Json (Invoke-Docker $Arguments))
    if ($items.Count -ne 1) { throw 'Expected exactly one Docker object.' }
    return $items[0]
}

function Assert-Desktop {
    $context = Invoke-Docker @('context','show')
    $endpoint = (Get-Inspection @('context','inspect',$context)).Endpoints.docker.Host
    if ($env:DOCKER_HOST -and !$env:DOCKER_CONTEXT) { $endpoint = $env:DOCKER_HOST }
    if ($endpoint -notmatch '^npipe:////\./pipe/[\w.-]+$') {
        throw 'Select a local Docker Desktop Windows named pipe.'
    }
    $info = ConvertFrom-Json (Invoke-Docker @('info','--format','{{json .}}'))
    if ($info.OSType -ne 'linux' -or $info.OperatingSystem -notmatch 'Docker Desktop' -or
        [int]($info.ServerVersion.Split('.')[0]) -lt 28) {
        throw 'Select Docker Desktop with Linux containers and Engine 28 or newer.'
    }
    $version = Invoke-Docker @('compose','version','--short')
    if ($version -notmatch '^v?(\d+\.\d+\.\d+)' -or [version]$Matches[1] -lt [version]'2.33.1') {
        throw 'Install Docker Compose 2.33.1 or newer.'
    }
}

function Resolve-Image([string]$Reference) {
    $digest = 'sha256:[0-9a-f]{64}'
    if ($Reference -cmatch "^$digest$") {
        if ((Invoke-Docker @('image','inspect','--format','{{.Id}}',$Reference)) -cne $Reference) {
            throw 'Selected local image ID is unavailable.'
        }
        return $Reference
    }
    $prefix = [regex]::Escape($repository)
    if ($Reference -cnotmatch "^${prefix}(:[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}|@$digest)$") {
        throw "Select a reviewed tag or index digest from $repository, or a full local image ID."
    }
    $null = Invoke-Docker @('pull',$Reference)
    $inspection = Get-Inspection @('image','inspect',$Reference)
    $digests = @($inspection.RepoDigests | Where-Object { $_ -cmatch "^${prefix}@$digest$" })
    if ($Reference.Contains('@') -and $digests -ccontains $Reference) { return $Reference }
    if (!$Reference.Contains('@') -and $digests.Count -eq 1) { return $digests[0] }
    throw 'Cannot identify a unique image digest. Supply the immutable reference.'
}

function Assert-Ownership($Owner) {
    $ids = Invoke-Docker @('ps','-aq','--filter',"label=com.docker.compose.project=$project")
    foreach ($id in ($ids -split '\s+' | Where-Object { $_ })) {
        if ((Get-Inspection @('inspect',$id)).Config.Labels.$ownerLabel -ne $Owner) {
            throw 'The Compose project is owned by another deployment.'
        }
    }
    foreach ($entry in @(@('volume','outbound-proxy-catalog'), @('network','outbound-proxy-ingress'))) {
        $names = Invoke-Docker @($entry[0],'ls','--format','{{.Name}}')
        if (($names -split '\s+') -contains $entry[1]) {
            if ((Get-Inspection @($entry[0],'inspect',$entry[1])).Labels.$ownerLabel -ne $Owner) {
                throw "Conflicting $($entry[0]) $($entry[1]). Use its original deployment record."
            }
        }
    }
}

function Initialize-Clients {
    $names = Invoke-Docker @('network','ls','--format','{{.Name}}')
    if (($names -split '\s+') -notcontains $clientNetwork) {
        $null = Invoke-Docker @('network','create','--driver','bridge','--internal','--subnet','172.29.241.0/28',
            '--ip-range','172.29.241.8/29','--gateway','172.29.241.1','--label',"$ownerLabel=clients-v1",$clientNetwork)
    }
    $net = Get-Inspection @('network','inspect',$clientNetwork)
    $pools = @($net.IPAM.Config)
    if ($net.Driver -ne 'bridge' -or !$net.Internal -or $net.EnableIPv6 -or $net.Labels.$ownerLabel -ne 'clients-v1' -or
        $pools.Count -ne 1 -or $pools[0].Subnet -ne '172.29.241.0/28' -or $pools[0].Gateway -ne '172.29.241.1' -or
        $pools[0].IPRange -ne '172.29.241.8/29') {
        throw 'Existing Docker client network conflicts with this installation.'
    }
}

function Invoke-Compose($Config, $Owner, [string[]]$Commands) {
    $labels = @{$ownerLabel=$Owner}
    $service = @{labels=$labels; environment=@{}}
    $override = @{services=@{proxy=$service}; volumes=@{catalog=@{labels=$labels}}; networks=@{ingress=@{labels=$labels}}}
    if ($Config.lan) {
        $service.ports = @(@{target=18888; published=[string]$Config.port; host_ip=$Config.lan; protocol='tcp'})
    }
    if ($Config.clients) {
        $service.networks = @{clients=@{ipv4_address='172.29.241.2'}}
        $service.environment.OP_CLIENT_IP = '172.29.241.2'
        $service.environment.OP_CLIENT_SUBNET = '172.29.241.0/28'
        $override.networks.clients = @{external=$true; name=$clientNetwork}
    }
    if ($Config.inventory) {
        $service.user = '0:0'
        $service.cap_add = @('SETUID','SETGID')
        $service.volumes = @(@{type='bind'; source='/var/run/docker.sock'; target='/var/run/docker.sock'; read_only=$true; bind=@{create_host_path=$false}})
        $service.environment.OP_INVENTORY = '1'
    }
    $vars = @{OP_IMAGE=$Config.image; OP_HOST_PORT=[string]$Config.port; OP_LAN_IP=[string]$Config.lan;
        OP_FORWARD_SOURCES='172.29.240.1/32,127.0.0.1/32'; COMPOSE_DISABLE_ENV_FILE='1'}
    if ($Config.lan) { $vars.OP_FORWARD_SOURCES += ",$($Config.subnet)" }
    $old = @{}
    try {
        foreach ($key in $vars.Keys) {
            $old[$key] = [Environment]::GetEnvironmentVariable($key)
            [Environment]::SetEnvironmentVariable($key,$vars[$key])
        }
        return Invoke-Docker (@('compose','--project-name',$project,'-f',$composeFile,'-f','-') + $Commands) (ConvertTo-Json $override -Depth 10 -Compress)
    } finally {
        foreach ($key in $old.Keys) { [Environment]::SetEnvironmentVariable($key,$old[$key]) }
    }
}

function Assert-FreePort([int]$Number) {
    $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback,$Number)
    try { $listener.ExclusiveAddressUse = $true; $listener.Start() }
    finally { $listener.Stop() }
}

function Wait-Page([int]$Number, [string]$Marker, [string]$Address='127.0.0.1') {
    for ($i = 0; $i -lt 30; $i++) {
        $request = [Net.HttpWebRequest]::Create("http://${Address}:$Number/")
        $request.Proxy = $null
        $request.Timeout = 1000
        $request.ReadWriteTimeout = 1000
        $request.AllowAutoRedirect = $false
        try {
            $response = $request.GetResponse()
            try {
                $reader = [IO.StreamReader]::new($response.GetResponseStream())
                try {
                    if ([int]$response.StatusCode -eq 200 -and $reader.ReadToEnd() -match $Marker) { return }
                } finally { $reader.Dispose() }
            } finally { $response.Dispose() }
        } catch [Net.WebException] {}
        Start-Sleep -Milliseconds 400
    }
    throw "Local HTTP check failed on port $Number."
}

function Test-Denial($Config, [int]$Number, [switch]$Live, [switch]$LanEndpoint) {
    $code = @'
import errno, http.client, socket, sys
for target in sys.argv[2:]:
    conn = http.client.HTTPConnection(target, int(sys.argv[1]), timeout=2)
    try:
        conn.request('GET', '/', headers={'Host': '127.0.0.1:18890'})
        if conn.getresponse().status != 403:
            sys.exit('Management reachable via ' + target)
    except socket.gaierror:
        sys.exit('Cannot resolve ' + target)
    except (ConnectionError, TimeoutError, http.client.RemoteDisconnected):
        pass
    except OSError as error:
        if error.errno not in (errno.ENETUNREACH, errno.EHOSTUNREACH):
            raise
    finally:
        conn.close()
'@
    $networks = @('bridge')
    if ($Config.clients) { $networks += $clientNetwork }
    foreach ($network in $networks) {
        $targets = @('engine-gateway','host.docker.internal')
        if ($LanEndpoint) { $targets += $Config.lan }
        if ($Live) {
            $targets += '172.29.240.2'
            if ($Config.lan) { $targets += $Config.lan }
            if ($network -eq $clientNetwork) { $targets += '172.29.241.2' }
        }
        $null = Invoke-Docker (@('run','--rm','-i','--pull=never','--network',$network,'--user','10001:10001',
            '--cap-drop','ALL','--read-only','--add-host','engine-gateway:host-gateway','--entrypoint','python3',
            $Config.image,'-',[string]$Number) + $targets) $code
    }
}

function Test-Isolation($Config) {
    $listener = "import http.server,os,signal; signal.alarm(120); os.chdir('/tmp'); http.server.HTTPServer(('',18890),http.server.SimpleHTTPRequestHandler).serve_forever()"
    $id = Invoke-Docker @('run','--rm','-d','--pull=never','--cap-drop','ALL','--read-only','--user','10001:10001',
        '-p','127.0.0.1::18890','--entrypoint','python3',$Config.image,'-c',$listener)
    try {
        $published = Invoke-Docker @('port',$id,'18890/tcp')
        if ($published -notmatch '^127\.0\.0\.1:(\d+)$') { throw 'Unexpected canary port binding.' }
        $bound = [int]$Matches[1]
        Wait-Page $bound 'Directory listing for'
        Test-Denial $Config $bound
    } finally { $null = Invoke-Docker @('rm','-f',$id) }
}

function Test-Forwarding($Config) {
    $result = & curl.exe --disable --silent --show-error --noproxy unused.invalid --proxy "http://127.0.0.1:$($Config.port)" --max-time 20 --output NUL --write-out '%{http_connect} %{http_code}' https://example.com/
    if ($LASTEXITCODE -ne 0 -or $result -ne '200 200') { throw 'Fresh HTTPS forwarding through the host proxy failed.' }
}

function Test-Operator($Config, $Id, [int]$Attempts=30) {
    for ($i=1; $i -le $Attempts; $i++) {
        $state = ConvertFrom-Json (Invoke-Docker @('exec',$Id,'outbound-proxy','status'))
        foreach ($field in @('enabled','running','boot_enabled','recovery_pending')) {
            if ($state.$field -isnot [bool]) { throw 'The installed CLI returned incomplete runtime status.' }
        }
        if ($state.deployment.image -cne $Config.image) { throw 'The CLI reports another deployment image.' }
        if (!$state.recovery_pending -and $state.enabled -eq $state.running -and $state.enabled -eq $state.boot_enabled) {
            if ($state.enabled) { Test-Forwarding $Config }
            return
        }
        if ($i -lt $Attempts) { Start-Sleep -Milliseconds 500 }
    }
    throw 'The installed CLI reports an inconsistent deployment or pending recovery.'
}

function Test-Service($Config, $Owner) {
    $id = Invoke-Compose $Config $Owner @('ps','-q','proxy')
    if ($id -notmatch '^[0-9a-f]{64}$') { throw 'Expected one running service container.' }
    $inspection = Get-Inspection @('inspect',$id)
    if ($inspection.Image -cne (Invoke-Docker @('image','inspect','--format','{{.Id}}',$Config.image))) {
        throw 'The active image differs from the pinned image.'
    }
    $net = Get-Inspection @('network','inspect','outbound-proxy-ingress')
    $members = @($net.Containers.PSObject.Properties.Name)
    if ($members.Count -ne 1 -or $members[0] -ne $id) { throw 'The host ingress network has unexpected members.' }
    $bindings = $inspection.NetworkSettings.Ports
    $panelBindings = @($bindings.'18890/tcp' | ForEach-Object { "$($_.HostIp):$($_.HostPort)" })
    $proxyBindings = @($bindings.'18888/tcp' | ForEach-Object { "$($_.HostIp):$($_.HostPort)" } | Sort-Object)
    $expected = @("127.0.0.1:$($Config.port)")
    if ($Config.lan) { $expected += "$($Config.lan):$($Config.port)"; Assert-LanPolicy $Config $Owner }
    if (($panelBindings -join ',') -ne '127.0.0.1:18890' -or ($proxyBindings -join ',') -ne (($expected | Sort-Object) -join ',')) {
        throw 'Published host ports differ from the verified deployment settings.'
    }
    Wait-Page 18890 'Outbound Proxy'
    Test-Denial $Config 18890 -Live
    Test-Operator $Config $id
}

function Save-Record($Record, $Path) {
    $temp = [IO.Path]::Combine([IO.Path]::GetDirectoryName($Path), [IO.Path]::GetRandomFileName())
    $backup = "$temp.bak"
    try {
        [IO.File]::WriteAllText($temp, (ConvertTo-Json $Record -Depth 6), [Text.UTF8Encoding]::new($false))
        if ([IO.File]::Exists($Path)) { [IO.File]::Replace($temp,$Path,$backup) }
        else { [IO.File]::Move($temp,$Path) }
    } finally { [IO.File]::Delete($temp); [IO.File]::Delete($backup) }
}

function Restore-Record($Record) {
    $null = Invoke-Compose $Record.pending $Record.owner @('down')
    if ($Record.pending.lan -or $Record.active.lan) { Remove-LanPolicy $Record.owner }
    if ($Record.active) {
        if ($Record.active.clients) { Initialize-Clients }
        Test-Isolation $Record.active
        try {
            if ($Record.active.lan) {
                Install-LanPolicy $Record.active $Record.owner
                Test-LanListener $Record.active
            }
            $null = Invoke-Compose $Record.active $Record.owner @('up','-d','--pull','never')
            Test-Service $Record.active $Record.owner
        } catch {
            $null = Invoke-Compose $Record.active $Record.owner @('down')
            throw
        }
    }
}

function Invoke-Deployment {
    if ([bool]$Lan -ne [bool]$Subnet -or ($LocalOnly -and $Lan)) { throw 'Specify both -Lan and -Subnet, or use -LocalOnly.' }
    if (($Clients -and $NoClients) -or ($Inventory -and $NoInventory)) { throw 'Conflicting deployment options.' }
    if ($Action -notin @('install','reconfigure') -and ($Image -or $Port -or $Lan -or $Clients -or $NoClients -or $Inventory -or $NoInventory -or $LocalOnly)) {
        throw 'Image and network settings apply only to install and reconfigure.'
    }
    Assert-Desktop
    $directory = [IO.Directory]::CreateDirectory($StateDir).FullName
    $lock = [IO.File]::Open((Join-Path $directory 'lock'), 'OpenOrCreate', 'ReadWrite', 'None')
    try {
        $path = Join-Path $directory 'deployment.json'
        $record = if ([IO.File]::Exists($path)) { ConvertFrom-Json ([IO.File]::ReadAllText($path)) }
                  else { [pscustomobject]@{owner=[guid]::NewGuid().ToString(); active=$null; previous=$null; pending=$null} }
        if (!$record.owner) { throw 'Deployment state has no owner.' }
        if (!$record.PSObject.Properties['pending']) { Add-Member -InputObject $record -NotePropertyName pending -NotePropertyValue $null }
        Assert-Ownership $record.owner
        if ($Action -eq 'status') {
            if ($record.active.lan) { Assert-LanPolicy $record.active $record.owner }
            $containers = if ($record.active) { Invoke-Compose $record.active $record.owner @('ps') }
            return (ConvertTo-Json @{settings=$record.active; recovery_required=[bool]$record.pending; container=$containers} -Depth 5)
        }
        if ($Lan -or $record.active.lan -or $record.pending.lan -or ($Action -eq 'rollback' -and $record.previous.lan)) {
            Assert-LanAdministrator
        }
        if ($Action -eq 'remove') {
            $current = if ($record.pending) { $record.pending } else { $record.active }
            if ($current) {
                $null = Invoke-Compose $current $record.owner @('down')
                if ($record.active.lan -or $record.pending.lan) { Remove-LanPolicy $record.owner }
                $record.active = $null; $record.previous = $null; $record.pending = $null
                Save-Record $record $path
            }
            return 'Removed. The catalog volume is retained.'
        }
        if ($record.pending) {
            Restore-Record $record
            $record.pending = $null
            Save-Record $record $path
        }
        $active = $record.active
        if ($Action -eq 'rollback') {
            $candidate = $record.previous
            if (!$candidate) { throw 'No previous deployment is recorded.' }
            if ($active.port -ne $candidate.port) { Assert-FreePort $candidate.port }
        } else {
            if (!$active -and $Action -eq 'reconfigure') { throw 'Install before reconfiguration.' }
            $candidate = if ($active) { $active.PSObject.Copy() } else {
                [pscustomobject]@{image=$null; port=18888; clients=$false; inventory=$false}
            }
            if (!$candidate.PSObject.Properties['lan']) { Add-Member -InputObject $candidate -NotePropertyName lan -NotePropertyValue $null }
            if (!$candidate.PSObject.Properties['subnet']) { Add-Member -InputObject $candidate -NotePropertyName subnet -NotePropertyValue $null }
            if ($Lan -or $LocalOnly) { $candidate.lan=$Lan; $candidate.subnet=$Subnet }
            if ($Port -in @(18889,18890)) { throw 'Ports 18889 and 18890 are reserved.' }
            if ($Port) { $candidate.port = $Port }
            if ($Clients -or $NoClients) { $candidate.clients = [bool]$Clients }
            if ($Inventory -or $NoInventory) { $candidate.inventory = [bool]$Inventory }
            if ($candidate.inventory -and !$candidate.clients) { throw 'Enable Docker clients before inventory.' }
            if (!$active) { Assert-FreePort 18890 }
            if (!$active -or $active.port -ne $candidate.port) { Assert-FreePort $candidate.port }
            if ($Image) { $candidate.image = Resolve-Image $Image }
            if (!$candidate.image) { throw 'Install requires -Image with a reviewed image reference.' }
        }
        if ($candidate.lan) {
            $scope = Get-LanScope $candidate
            Assert-FirewallProfiles $candidate $scope
        }
        if ($candidate.clients) { Initialize-Clients }
        $null = Invoke-Compose $candidate $record.owner @('config','--quiet')
        Test-Isolation $candidate
        $record.pending = $candidate
        Save-Record $record $path
        try {
            if ($candidate.lan -or $active.lan) {
                if ($active) { $null = Invoke-Compose $active $record.owner @('down') }
                Remove-LanPolicy $record.owner
                if ($candidate.lan) {
                    Install-LanPolicy $candidate $record.owner
                    Test-LanListener $candidate
                }
            }
            $null = Invoke-Compose $candidate $record.owner @('up','-d','--pull','never')
            Test-Service $candidate $record.owner
            if ((ConvertTo-Json $active -Compress) -ne (ConvertTo-Json $candidate -Compress)) { $record.previous = $active }
            $record.active = $candidate
            $record.pending = $null
            Save-Record $record $path
        } catch {
            $journal = ConvertFrom-Json ([IO.File]::ReadAllText($path))
            Restore-Record $journal
            $journal.pending = $null
            Save-Record $journal $path
            throw
        }
        return "Installed $($candidate.image). Panel http://127.0.0.1:18890/ Proxy http://127.0.0.1:$($candidate.port)"
    } finally { $lock.Dispose() }
}

if ($MyInvocation.InvocationName -ne '.') { Invoke-Deployment }
