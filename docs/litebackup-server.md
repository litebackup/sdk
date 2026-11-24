# How to install a Litebackup server

Litebackup is a self-hosted daemon. Your Litebackup server receives changes
from app machines, keeps recent versions on disk, and uploads encrypted
backups to your bucket. One server can serve several app machines; your apps
keep their local SQLite files and drivers.

Run it on a dedicated Linux VM or host. The installer manages it as a systemd
service and supplies its HTTPS address and web console. Start here, then
[connect your app machines](litebackup-client.md).

## VMs and containers

A dedicated VM is the recommended server deployment. Your applications and
the Litebackup client can run in [Docker or other containers](sqlite-docker.md)
and connect to that server.

The current server package runs natively under systemd; it does not include
a standalone Docker image or a server Compose deployment. The client Docker
example is for backing up an application's SQLite files, not hosting the server.

## Choose a VM

Use a fresh **Debian 13 or Ubuntu 26.04 LTS** VM with systemd, a public IP,
and an SSD. The installer supports Linux amd64 and arm64. For a small deployment,
start with **2 vCPUs and 2–4 GB RAM**, then adjust for your workload.

| Provider | VM | Networking to configure |
| --- | --- | --- |
| [AWS](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/EC2_GetStarted.html) | EC2, Ubuntu or Debian, persistent EBS SSD | Public IP; security group permits TCP 443 and your SSH access |
| [Google Cloud](https://cloud.google.com/compute/docs/instances/create-start-instance) | Compute Engine, Ubuntu or Debian, persistent SSD | External IP; VPC firewall permits TCP 443 and your SSH access |
| [DigitalOcean](https://docs.digitalocean.com/products/droplets/how-to/create/) | Debian or Ubuntu Droplet with SSD storage | Public IP; Cloud Firewall permits TCP 443 and your SSH access |
| [Hetzner](https://docs.hetzner.com/cloud/servers/getting-started/creating-a-server/) / Akamai Linode / other VPS | Linux VM with systemd and persistent SSD | Public IP; provider and host firewalls permit TCP 443 |

Put the server near your app machines for faster syncs. Check transfer costs
if the server and bucket are in different regions or on different providers.

Disk usage depends on database size, write rate, and recent history. Leave
room for changes to accumulate during a bucket outage. A 20–40 GB SSD can suit
a small trial; monitor free space as you add databases.

## Install

SSH into the VM as a user with sudo access:

```sh
curl -fsSL https://litebackup.com/server.sh | bash
```

The installer verifies the download, installs a system service, and asks for
sudo when needed.

Open the printed claim link, sign in, and name the instance. Setup supplies an
HTTPS instance address, such as `your-instance.litebackup.cloud`; a custom
domain is not needed for the first install. TCP 443 must reach the advertised
public address. If you publish IPv6 as well, check its firewall too.

Finish the setup screens:

1. Add and test a [bucket](sqlite-cloud-backup.md), then enable it.
2. Save the recovery kit off this server, or set and safely store a recovery
   passphrase. Keep the bucket credentials separately too.
3. Choose how long to keep versions.

Clients wait while setup is incomplete. The [install page](https://litebackup.com/install)
tracks the current installer flow.

## Proxies and port forwarding

The production server serves HTTPS on TCP 443 and manages its own certificate.
For a TCP proxy, load balancer, or NAT gateway, forward public TCP 443 to the
server's TCP 443 with **TLS passthrough**: the encrypted connection reaches
Litebackup unchanged, and Litebackup terminates TLS.

If that public address differs from the server's outbound IP, supply it when
installing on the server. Replace `YOUR_PUBLIC_IP` with the proxy or gateway's
public IP:

```sh
curl -fsSL https://litebackup.com/server.sh | bash -s -- --address YOUR_PUBLIC_IP
```

Use the instance HTTPS hostname supplied during setup. Preserve TLS SNI, and
do not prepend a PROXY protocol header. Forward connections to this instance,
not to independent Litebackup servers sharing one hostname.

For example, HAProxy calls this [TLS passthrough](https://www.haproxy.com/documentation/haproxy-unified-gateway/usage/routes/tlsroute/).
The proxy forwards traffic; it does not need the instance's certificate or key.

A production recipe for a TLS-terminating reverse proxy is not currently
documented. The plain HTTP `--listen-address` setting applies to development
mode; do not use `--development` as a production proxy backend. Use direct
HTTPS or TCP/TLS passthrough for the current installation flow.

## Add a machine and verify a backup

In the console, open **Machines → Add machine** and copy the join token. On the
machine that holds your SQLite file:

```sh
curl -fsSL https://litebackup.com/cli.sh | bash
export LITEBACKUP_API_KEY='JOIN_TOKEN@your-instance.litebackup.cloud'
litebackup up "$LITEBACKUP_API_KEY"
litebackup start /var/lib/app/main.sqlite
litebackup status
```

The token includes both the secret and server address. Keep it out of git and
job logs. Each machine needs its own join; SDKs and the CLI share that
machine's [client session](litebackup-client.md).

Confirm the database appears in the console. Check both the server and cloud
versions: registration alone does not mean an upload has completed. Run a
[restore drill](sqlite-restore.md) into a new file before relying on the backup.

## Operate the server

The console shows databases, machine ownership, bucket status, retention,
disk, and server settings. Saving server settings restarts the server: it exits with
status 75, and its supervisor, the Apps launcher, starts it again. A server
run by hand, as with `--development`, needs a supervisor that restarts it on
that status, or it stays stopped. To administer remotely with the CLI:

```sh
litebackup admin login
litebackup admin --help
```

The server exposes local health and metrics on `127.0.0.1:8099`, or on the
address `--health-address` (`LITEBACKUP_HEALTH_ADDRESS`) sets; give each
server on one host its own:

```sh
# On the server VM:
curl -fsS http://127.0.0.1:8099/health
curl -fsS http://127.0.0.1:8099/metrics
```

Keep this listener private. Public client traffic uses HTTPS 443.

Update the server through its instance controls, then update each installed
client with `litebackup install cli`, run as the user that installed it. Keep npm SDK and binary versions
aligned when upgrading an app. See [troubleshooting](sqlite-backup-errors.md) for
compatibility errors and [restore](sqlite-restore.md) for recovery after losing the VM.
