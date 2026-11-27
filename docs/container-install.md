# Install the container edition

Use the scripts and Compose file from the **same reviewed release or PR revision** as the image. Install Docker Engine 28 or newer and Compose 2.33.1 or newer. Select a local Docker context with Linux containers. The POSIX entry point uses Python 3.9 or newer. Linux Engine also requires iproute2, nftables, and administrative access. Docker Desktop runs with the signed-in operator's permissions.

Reserve host loopback port 18890 for management and 18888 for forwarding, or select another forwarding port during installation. Reserve Docker subnet 172.29.240.0/28 for host ingress. Enabling Docker clients also requires 172.29.241.0/28. Check host and VPN routes before installing. Keep the existing MiniPC service running on its own host during evaluation.

## Install a pinned revision

Copy the complete image reference from the passing PR or release check, including the registry name and index digest. Private images require Docker registry read access. Run the following commands from the reviewed checkout. An explicit channel tag is also accepted. The installer pulls that tag and records its immutable digest before replacing the current container.

On Linux, use the administrative Docker context. Set the image value in the shell before running the command.

```bash
IMAGE='ghcr.io/ikeniborn/outbound-proxy@sha256:REPLACE_WITH_INDEX_DIGEST'
sudo sh scripts/docker-proxy.sh install --image "$IMAGE"
```

On macOS, use the Desktop context as the signed-in operator.

```bash
IMAGE='ghcr.io/ikeniborn/outbound-proxy@sha256:REPLACE_WITH_INDEX_DIGEST'
sh scripts/docker-proxy.sh install --image "$IMAGE"
```

On Windows, open PowerShell in the reviewed checkout.

```powershell
$Image = 'ghcr.io/ikeniborn/outbound-proxy@sha256:REPLACE_WITH_INDEX_DIGEST'
.\scripts\docker-proxy.ps1 install -Image $Image
```

For an offline POSIX rehearsal, load the reviewed platform image first. Pass its full local image ID as `--image sha256:<64 hex digits>`. The installer verifies that exact ID locally and skips registry pulls. A local image ID identifies one platform. A registry index digest identifies the release across platforms.

Open [the local panel](http://127.0.0.1:18890/). The container edition opens a local browser session automatically. Add an HTTPS upstream, check it, select it, and enable forwarding. Point the client at the displayed host port. Disabling forwarding leaves management available. Automatic failover requires separate operator opt-in.

## Change the host port or image

Use the installer to change the host mapping. The panel displays the configured host port. Change the client proxy URL after a successful port change. These POSIX commands use Linux privileges. On macOS omit the leading sudo.

```bash
sudo sh scripts/docker-proxy.sh reconfigure --port 19999
sudo sh scripts/docker-proxy.sh reconfigure --image "$IMAGE"
sudo sh scripts/docker-proxy.sh status
sudo sh scripts/docker-proxy.sh rollback
```

```powershell
.\scripts\docker-proxy.ps1 reconfigure -Port 19999
.\scripts\docker-proxy.ps1 reconfigure -Image $Image
.\scripts\docker-proxy.ps1 status
.\scripts\docker-proxy.ps1 rollback
```

Before replacement, the POSIX installer checks image resolution, port availability and Compose configuration. A disposable HTTP listener checks whether another container can reach a loopback publication through the host gateway before the installer mounts the catalog. The installer then checks the running image identity, management response and forbidden management paths. A failed replacement restores the prior image, mapping and LAN policy. The deployment record retains the last successful settings and an interrupted transaction for recovery on the next modifying invocation. Keep the previous image cached until the new installation has passed acceptance.

## LAN forwarding on Linux Engine

Choose the LAN IPv4 address actually configured on this host and a permitted source CIDR on the same interface. Both values are required. The example uses documentation addresses. Replace them with local values.

```bash
sudo sh scripts/docker-proxy.sh reconfigure --lan 192.0.2.10 --subnet 192.0.2.0/26
sudo sh scripts/docker-proxy.sh reconfigure --local-only
```

Compose publishes both ports on IPv4 loopback. The Linux installer installs an owned nftables prerouting rule for LAN forwarding. It matches the ingress interface, original destination, destination port and original source address before translating the forwarding connection to the container. A separate raw filter rejects packets addressed directly to the ingress container and packets outside the LAN rule. The manager has no LAN mapping. A different installer-owned table identifier or a failed host rule check stops deployment.

The kernel retains the LAN rule across a Docker daemon restart. On systemd Linux hosts the installer enables a deployment-owned oneshot unit, `outbound-proxy-lan.service`. At boot it locks the deployment record, checks the committed LAN interface and restores only its nftables table. An interrupted transaction or a changed interface leaves LAN publication closed. The local-only and removal actions remove the owned unit and its boot helper. After an independent firewall reload, reapply committed settings with the reconfigure action. Confirm forwarding from an authorized second PC and rejection from an excluded subnet. Confirm LAN management denial. Desktop LAN publication is withheld until its host ingress path can enforce and verify the selected source scope.

## Docker clients and inventory

Use the dedicated internal bridge only for authorized client containers. Enable the deployment network, attach the intended client, then enable Docker forwarding in the panel. Disabling the deployment option disconnects the proxy from that bridge. Existing client attachments remain available for reinstallation.

```bash
sudo sh scripts/docker-proxy.sh reconfigure --clients
docker network connect outbound-proxy-clients my-client
docker exec outbound-proxy-proxy-1 outbound-proxy docker on
```

Set the attached client's HTTP proxy to http://172.29.241.2:18888. Docker clients use the fixed container port. The optional inventory reports membership of this named network. It requires an explicit deployment flag as well as Docker client access.

```bash
sudo sh scripts/docker-proxy.sh reconfigure --clients --inventory
sudo sh scripts/docker-proxy.sh reconfigure --no-inventory --no-clients
```

With inventory enabled, the bootstrap process starts a separate unprivileged inventory helper with access to the Unix daemon socket and drops the panel's privileges. The helper projects member information from the fixed network inspection. By default the socket is absent. A read-only mount of a daemon socket still confers privileged API access on processes that can open it.

Docker selects one IPv4 endpoint for published ports. The host ingress bridge therefore carries both the local panel and host forwarding listeners at 172.29.240.2. This updates the draft interface layout. The installer requires the proxy to be the sole member. Docker clients use a distinct proxy-only interface and cannot join ingress through the panel. Test the actual host gateway path on each supported Desktop backend. Docker users who control the daemon are trusted host operators. [Docker port publishing](https://docs.docker.com/engine/network/port-publishing/) · [Docker Desktop networking](https://docs.docker.com/desktop/features/networking/)

## Remove, back up and restore

Removal stops the service and removes its Compose host ingress network. It retains the private catalog volume. On POSIX it also retains the optional external client network with its attachments. Repeating removal is safe.

```bash
sudo sh scripts/docker-proxy.sh remove
```

```powershell
.\scripts\docker-proxy.ps1 remove
```

Record the selected immutable image and deployment settings before removal. For a consistent offline archive, remove the service first. Use the same operator context for the Docker commands. The archive contains proxy credentials. Store it in a private location.

```bash
umask 077
docker run --rm --pull=never --network none --read-only --entrypoint tar -v outbound-proxy-catalog:/catalog:ro "$IMAGE" -C /catalog -cf - . > outbound-proxy-catalog.tar
```

To recover on a fresh Docker host, install and remove the same reviewed image before adding any upstreams. This creates the deployment-owned catalog volume. Use only a trusted archive. Restore the archive into this fresh volume, reinstall the image, and reapply the saved host settings. The installer refuses an unrelated pre-existing volume or ingress network with the same name.

```bash
docker run --rm -i --pull=never --network none --read-only --entrypoint tar -v outbound-proxy-catalog:/var/lib/outbound-proxy "$IMAGE" -C /var/lib/outbound-proxy -xf - < outbound-proxy-catalog.tar
```

The POSIX deployment record is in `/var/lib/outbound-proxy-deployment/deployment.json` on Linux and `~/Library/Application Support/outbound-proxy/deployment.json` on macOS. Keep this record when recovering the same installation. It contains image and mapping settings. Catalog credentials remain in the volume.
