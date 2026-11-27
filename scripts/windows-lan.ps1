# Manage the two deployment-owned Windows Defender Firewall rules for one IPv4 listener.
function ConvertTo-IPv4Number([string]$Address) {
    $ip = [Net.IPAddress]::Parse($Address)
    if ($ip.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork -or $ip.ToString() -ne $Address) {
        throw "Expected a dotted IPv4 address, received $Address."
    }
    [long]$number = 0
    foreach ($byte in $ip.GetAddressBytes()) { $number = $number * 256 + $byte }
    return $number
}

function ConvertFrom-IPv4Number([long]$Number) {
    return "$(($Number -shr 24) -band 255).$(($Number -shr 16) -band 255).$(($Number -shr 8) -band 255).$($Number -band 255)"
}

function Get-IPv4Interval([string]$Text) {
    if ($Text.Contains('-')) {
        $ends = $Text.Split('-')
        if ($ends.Count -ne 2) { throw 'Invalid IPv4 range.' }
        return @((ConvertTo-IPv4Number $ends[0]), (ConvertTo-IPv4Number $ends[1]))
    }
    $parts = $Text.Split('/')
    $start = ConvertTo-IPv4Number $parts[0]
    if ($parts.Count -eq 1) { return @($start,$start) }
    if ($parts.Count -ne 2) { throw 'Invalid IPv4 network.' }
    $prefix = $parts[1]
    if ($prefix.Contains('.')) {
        $bits = [Convert]::ToString((ConvertTo-IPv4Number $prefix),2).PadLeft(32,'0')
        if ($bits -notmatch '^1*0*$') { throw 'Invalid network mask.' }
        $prefix = $bits.TrimEnd('0').Length
    }
    if ([string]$prefix -notmatch '^(0|[1-9]|[12][0-9]|3[0-2])$') { throw 'Invalid prefix length.' }
    [long]$size = [long]1 -shl (32-[int]$prefix)
    return @(($start - $start % $size), ($start - $start % $size + $size - 1))
}

function Get-LanScope($Config) {
    if ($Config.subnet -notmatch '^([\d.]+)/(\d+)$') { throw 'Use an IPv4 source network in CIDR form.' }
    $requestedStart = ConvertTo-IPv4Number $Matches[1]
    $range = Get-IPv4Interval $Config.subnet
    $hostIP = ConvertTo-IPv4Number $Config.lan
    if ($requestedStart -ne $range[0]) { throw 'Use the network address for the source CIDR.' }
    foreach ($excluded in @('0.0.0.0/8','127.0.0.0/8','169.254.0.0/16','224.0.0.0/3','172.29.240.0/28','172.29.241.0/28')) {
        $reserved = Get-IPv4Interval $excluded
        if (($range[0] -le $reserved[1] -and $range[1] -ge $reserved[0]) -or
            ($hostIP -ge $reserved[0] -and $hostIP -le $reserved[1])) { throw 'Select a unicast LAN address and subnet.' }
    }
    $addresses = @(Get-NetIPAddress -AddressFamily IPv4 -IPAddress $Config.lan -ErrorAction Stop |
        Where-Object { $_.AddressState -eq 'Preferred' })
    if ($addresses.Count -ne 1) { throw 'Select an IPv4 address on one active LAN adapter.' }
    $adapter = Get-NetAdapter -InterfaceIndex $addresses[0].InterfaceIndex -ErrorAction Stop
    if ($adapter.Status -ne 'Up' -or !$adapter.HardwareInterface) { throw 'Select an active physical LAN adapter.' }
    $attached = Get-IPv4Interval "$($Config.lan)/$($addresses[0].PrefixLength)"
    if ($range[0] -lt $attached[0] -or $range[1] -gt $attached[1]) { throw 'The permitted source CIDR must fit within the attached LAN.' }
    return @{interface=$adapter.Name; allow=@($Config.subnet); block=@(
        "0.0.0.0-$(ConvertFrom-IPv4Number ($range[0]-1))",
        "$(ConvertFrom-IPv4Number ($range[1]+1))-255.255.255.255")}
}

function Assert-LanAdministrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    if (!$principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'Open an elevated PowerShell session to manage the Windows LAN rules.'
    }
}

function Get-FirewallNames($Owner) { return @("outbound-proxy-$Owner-block","outbound-proxy-$Owner-allow") }

function Remove-LanPolicy($Owner) {
    $names = Get-FirewallNames $Owner
    $rules = @(Get-NetFirewallRule -PolicyStore PersistentStore -ErrorAction Stop | Where-Object { $_.Name -in $names })
    foreach ($rule in $rules) {
        if ($rule.Name -notin $names -or $rule.Group -ne "outbound-proxy-$Owner") { throw 'Firewall rule ownership conflict.' }
    }
    # Call only after the service and its published LAN port have stopped.
    foreach ($rule in $rules) { $rule | Remove-NetFirewallRule -ErrorAction Stop }
}

function Get-RangeKeys($Addresses) {
    foreach ($address in $Addresses) {
        $range = Get-IPv4Interval $address
        "$($range[0])-$($range[1])"
    }
}

function Test-BypassApplies($Rule, $Config) {
    $portFilter = $Rule | Get-NetFirewallPortFilter -ErrorAction Stop
    if ($portFilter.Protocol -notin @('Any','TCP','6')) { return $false }
    $matchesPort = $false
    foreach ($value in $portFilter.LocalPort) {
        if ($value -match '^(\d+)(?:-(\d+))?$') {
            $last = if ($Matches[2]) { [int]$Matches[2] } else { [int]$Matches[1] }
            if ($Config.port -ge [int]$Matches[1] -and $Config.port -le $last) { $matchesPort=$true }
        } else { $matchesPort=$true }
    }
    if (!$matchesPort) { return $false }
    $addressFilter = $Rule | Get-NetFirewallAddressFilter -ErrorAction Stop
    $ip = ConvertTo-IPv4Number $Config.lan
    foreach ($local in $addressFilter.LocalAddress) {
        if ($local.Contains(':')) { continue }
        try { $range = Get-IPv4Interval $local } catch { return $true }
        if ($ip -ge $range[0] -and $ip -le $range[1]) { return $true }
    }
    return $false
}

function Assert-FirewallProfiles($Config,$Scope) {
    $services = @(Get-CimInstance -ClassName Win32_Service -Filter "Name='BFE' OR Name='MpsSvc'" -ErrorAction Stop)
    if ($services.Count -ne 2 -or @($services | Where-Object { $_.State -ne 'Running' -or $_.StartMode -ne 'Auto' }).Count) {
        throw 'Windows firewall services must be running and configured for automatic startup.'
    }
    $profiles = @(Get-NetFirewallProfile -PolicyStore ActiveStore -ErrorAction Stop)
    foreach ($name in @('Domain','Private','Public')) {
        $profile = @($profiles | Where-Object Name -eq $name)
        if ($profile.Count -ne 1 -or [string]$profile[0].Enabled -ne 'True' -or
            [string]$profile[0].AllowLocalFirewallRules -eq 'False' -or [string]$profile[0].AllowInboundRules -eq 'False') { throw "Firewall profile $name cannot enforce the deployment rules." }
        foreach ($alias in $profile[0].DisabledInterfaceAliases) {
            if ($alias -eq 'All' -or $Scope.interface -like $alias) { throw "Firewall profile $name exempts this adapter." }
        }
    }
    # IPsec authenticated bypass rules can override ordinary explicit blocks.
    $filters = Get-NetFirewallSecurityFilter -PolicyStore ActiveStore -ErrorAction Stop
    foreach ($bypass in @($filters | Where-Object { [string]$_.OverrideBlockRules -eq 'True' })) {
        $rule = $bypass | Get-NetFirewallRule -ErrorAction Stop
        if ([string]$rule.Enabled -eq 'True' -and $rule.Direction -eq 'Inbound' -and
            (Test-BypassApplies $rule $Config)) { throw 'An authenticated firewall bypass may override the source restriction.' }
    }
}

function Assert-LanPolicy($Config, $Owner) {
    $scope = Get-LanScope $Config
    Assert-FirewallProfiles $Config $scope
    foreach ($actionName in @('Block','Allow')) {
        $name = "outbound-proxy-$Owner-$($actionName.ToLowerInvariant())"
        $rule = Get-NetFirewallRule -PolicyStore ActiveStore -TracePolicyStore -Name $name -ErrorAction Stop
        if ($rule.Group -ne "outbound-proxy-$Owner" -or [string]$rule.Enabled -ne 'True' -or
            $rule.PrimaryStatus -ne 'OK' -or $rule.EnforcementStatus -ne 'Full' -or $rule.PolicyStoreSourceType -ne 'Local' -or
            $rule.Direction -ne 'Inbound' -or $rule.Action -ne $actionName -or [string]$rule.Profile -ne 'Any') {
            throw "ActiveStore does not enforce the expected $actionName rule."
        }
        $addresses = $rule | Get-NetFirewallAddressFilter -ErrorAction Stop
        $ports = $rule | Get-NetFirewallPortFilter -ErrorAction Stop
        $interfaces = $rule | Get-NetFirewallInterfaceFilter -ErrorAction Stop
        $application = $rule | Get-NetFirewallApplicationFilter -ErrorAction Stop
        $service = $rule | Get-NetFirewallServiceFilter -ErrorAction Stop
        $expectedInterface = if ($actionName -eq 'Allow') { $scope.interface } else { 'Any' }
        $expectedSource = @(Get-RangeKeys $scope[$actionName.ToLowerInvariant()] | Sort-Object)
        $actualSources = @(Get-RangeKeys $addresses.RemoteAddress | Sort-Object)
        if (($actualSources -join ',') -ne ($expectedSource -join ',') -or
            (@(Get-RangeKeys $addresses.LocalAddress) -join ',') -ne (@(Get-RangeKeys $Config.lan) -join ',') -or
            $application.Program -ne 'Any' -or $service.Service -ne 'Any' -or (@($ports.RemotePort) -join ',') -ne 'Any' -or
            $ports.Protocol -notin @('TCP','6') -or (@($ports.LocalPort) -join ',') -ne [string]$Config.port -or
            (@($interfaces.InterfaceAlias) -join ',') -ne $expectedInterface) {
            throw 'Effective firewall scope differs from the requested endpoint.'
        }
    }
}

function Install-LanPolicy($Config, $Owner) {
    $scope = Get-LanScope $Config
    Assert-FirewallProfiles $Config $scope
    $common = @{PolicyStore='PersistentStore'; Group="outbound-proxy-$Owner"; Direction='Inbound'; Enabled='True';
        Protocol='TCP'; LocalAddress=$Config.lan; LocalPort=$Config.port; Profile='Any'; ErrorAction='Stop'}
    $names = Get-FirewallNames $Owner
    $null = New-NetFirewallRule @common -Name $names[0] -DisplayName $names[0] -Action Block -RemoteAddress $scope.block
    $null = New-NetFirewallRule @common -Name $names[1] -DisplayName $names[1] -Action Allow -RemoteAddress $scope.allow -InterfaceAlias $scope.interface
    Assert-LanPolicy $Config $Owner
}

function Test-LanListener($Config) {
    # The canary publishes on the final endpoint, through the same Docker Desktop backend.
    $port = [string]$Config.port
    $mapping = "$($Config.lan):${port}:18888"
    $listener = "import http.server,os,signal; signal.alarm(120); os.chdir('/tmp'); http.server.HTTPServer(('',18888),http.server.SimpleHTTPRequestHandler).serve_forever()"
    $id = Invoke-Docker @('run','--rm','-d','--pull=never','--cap-drop','ALL','--read-only','--user','10001:10001',
        '-p',$mapping,'--entrypoint','python3',$Config.image,'-c',$listener)
    try {
        if ((Invoke-Docker @('port',$id,'18888/tcp')) -ne "$($Config.lan):$port") { throw 'Unexpected LAN canary binding.' }
        Wait-Page $Config.port 'Directory listing for' $Config.lan
        Test-Denial $Config $Config.port -LanEndpoint
        $listenerProcess = @(Get-NetTCPConnection -State Listen -LocalPort $Config.port -ErrorAction Stop |
            Where-Object LocalAddress -eq $Config.lan)
        if ($listenerProcess.Count -ne 1 -or (Get-Process -Id $listenerProcess[0].OwningProcess).ProcessName -ne 'com.docker.backend') {
            throw 'The selected LAN endpoint lacks a verified Windows Docker Desktop listener.'
        }
    } finally { $null = Invoke-Docker @('rm','-f',$id) }
}
