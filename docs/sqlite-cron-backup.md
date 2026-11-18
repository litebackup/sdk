# Schedule SQLite backups with Cron or Kubernetes

Run a backup script on a schedule when you need a job that backs up files
and exits. Each run syncs the files to your bucket; later writes stay in the
local SQLite files for the next run. Keep them on [persistent storage](sqlite-backup.md#keep-the-files-on-persistent-storage).
For apps that stay up, consider [continuous backup](sqlite-backup.md).

## Prepare the database host

Install a [server](litebackup-server.md) and the CLI, then join once as the OS user that runs the job.

```sh
litebackup up "$LITEBACKUP_TOKEN"
litebackup backup ./main.sqlite scheduled/ --wait cloud
```

## Back up a list with the CLI

The [script](../examples/cron/backup-sqlite.sh) backs up each path, waits for
the bucket, and releases its writer lock. Install it somewhere
the cron user can run it, such as `/opt/app/backup-sqlite.sh`.

```sh
/opt/app/backup-sqlite.sh /var/lib/app/exports/main.sqlite /var/lib/app/exports/audit.sqlite
```

It uses `scheduled/` names, so these become `scheduled/main.sqlite` and
`scheduled/audit.sqlite`. Reserve those names for this job. Different input
paths with the same basename need a modified script with explicit names.
These export files must not be covered by the app's continuous backup; see
[one-shot jobs](sqlite-backup-script.md) for waiting on an existing backup instead.

Create writable `/var/lib/app/run` and `/var/lib/app/log` directories, then
edit that user's crontab:

```cron
SHELL=/bin/bash
PATH=/usr/local/bin:/usr/bin:/bin
# Every hour; refuse overlap and cap total runtime on this Linux host.
0 * * * * /usr/bin/flock -n /var/lib/app/run/backup.lock /usr/bin/timeout --signal=TERM --kill-after=30s 75m /opt/app/backup-sqlite.sh /var/lib/app/exports/main.sqlite /var/lib/app/exports/audit.sqlite >> /var/lib/app/log/backup.log 2>&1
```

This uses Linux `flock` and GNU `timeout`. Verify their paths on your VM.
A skipped lock acquisition or failed command exits nonzero. Configure your
job monitoring to alert on failures and on missing successful runs. Cron's
schedule uses the host's timezone unless you configure it otherwise.

The script allows 30 minutes per file and 75 minutes for the two-file job.
The default archive delay is 15 minutes; leave time for uploads too. A forced kill may leave the remote lease active until expiry.

The [TypeScript runner](sqlite-backup-script.md) works with the same cron setup.

## Kubernetes CronJob

The pod must mount the actual SQLite directory, including WAL files. A pod
on an unrelated node cannot access a path from another pod without an
appropriate volume. Check PVC access modes and placement: a `ReadWriteOnce`
volume may prevent a separate job from mounting while the app runs elsewhere.
Using the app's existing client on its host can be simpler.

Build the [pinned CLI image](sqlite-docker.md#install-a-pinned-cli-in-your-own-dockerfile) for the target architecture and matching SDK version, then use that image to build the job from the SDK repository root.

```sh
docker build --build-context cli=docker-image://litebackup-cli:pinned \
  -f examples/cron/Dockerfile -t YOUR_REGISTRY/litebackup-job:latest .
# Push the image to your registry before applying the manifest.
```

Create a Secret named `litebackup-join` with key `token` through your secret
manager. Provision `app-databases` and `litebackup-client-state` PVCs, replace
the image reference, and adapt volume permissions for your cluster:

```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: sqlite-backup
spec:
  schedule: "0 * * * *"
  timeZone: Etc/UTC
  concurrencyPolicy: Forbid
  startingDeadlineSeconds: 300
  jobTemplate:
    spec:
      activeDeadlineSeconds: 4500
      backoffLimit: 1
      template:
        spec:
          restartPolicy: Never
          securityContext:
            runAsNonRoot: true
            runAsUser: 1000
            runAsGroup: 1000
            fsGroup: 1000
          terminationGracePeriodSeconds: 30
          containers:
            - name: backup
              image: YOUR_REGISTRY/litebackup-job:latest
              args: ["/data/exports/main.sqlite", "/data/exports/audit.sqlite"]
              env:
                - name: LITEBACKUP_TOKEN
                  valueFrom:
                    secretKeyRef:
                      name: litebackup-join
                      key: token
              volumeMounts:
                - name: databases
                  mountPath: /data
                - name: client-state
                  mountPath: /state
          volumes:
            - name: databases
              persistentVolumeClaim:
                claimName: app-databases
            - name: client-state
              persistentVolumeClaim:
                claimName: litebackup-client-state
```

The image runs as `node` (UID 1000). Set the pod's UID and GID to match your
app and grant that user access to both volumes. The app must create the
export files before this job runs.

`Forbid` only prevents overlap within this CronJob. It does not coordinate
other CronJobs or writers using the same backup names. The persistent client
state keeps the joined machine identity across pods. Each foreground job has its own writer session. A successful pod has waited for the bucket and released its locks.

Kubernetes schedules are approximate and Jobs may be retried. Keep the job
idempotent, use stable names, and alert on failed Jobs. See the
[Kubernetes CronJob reference](https://kubernetes.io/docs/concepts/workloads/controllers/cron-jobs/)
for scheduling and concurrency semantics.
