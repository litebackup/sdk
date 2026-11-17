# How to install a Litebackup server

Your server receives changes from app machines, keeps recent versions on disk,
and uploads encrypted backups to your bucket. It can run separately from your
app. Start here, then [connect your app machines](litebackup-client.md).

## Choose a VM

Use a fresh **Ubuntu 24.04 LTS or Debian 13** VM with systemd, a public IP,
and an SSD. The installer supports Linux amd64 and arm64. For a small deployment,
start with **2 vCPUs and 2–4 GB RAM**, then adjust for your workload.

| Provider | VM | Networking to configure |
| --- | --- | --- |
| [AWS](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/EC2_GetStarted.html) | EC2, Ubuntu or Debian, persistent EBS SSD | Public IP; security group permits TCP 443 and your SSH access |
| [Google Cloud](https://cloud.google.com/compute/docs/instances/create-start-instance) | Compute Engine, Ubuntu or Debian, persistent SSD | External IP; VPC firewall permits TCP 443 and your SSH access |
| [DigitalOcean](https://docs.digitalocean.com/products/droplets/how-to/create/) | Ubuntu Droplet with SSD storage | Public IP; Cloud Firewall permits TCP 443 and your SSH access |
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

## Add a machine and verify a backup

In the console, open **Machines → Add machine** and copy the join token. On the
machine that holds your SQLite file:

```sh
curl -fsSL https://litebackup.com/cli.sh | sudo bash
export LITEBACKUP_TOKEN='JOIN_TOKEN@your-instance.litebackup.cloud'
litebackup up "$LITEBACKUP_TOKEN"
litebackup backup /var/lib/app/main.sqlite
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
disk, and tuning. To administer remotely with the CLI:

```sh
litebackup admin login
litebackup admin --help
```

The server exposes local health and metrics on `127.0.0.1:8099`:

```sh
# On the server VM:
curl -fsS http://127.0.0.1:8099/health
curl -fsS http://127.0.0.1:8099/metrics
```

Keep this listener private. Public client traffic uses HTTPS 443.

Update the server through its instance controls, then update each installed
client with `sudo litebackup install cli`. Keep npm SDK and binary versions
aligned when upgrading an app. See [troubleshooting](sqlite-backup-errors.md) for
compatibility errors and [restore](sqlite-restore.md) for recovery after losing the VM.
