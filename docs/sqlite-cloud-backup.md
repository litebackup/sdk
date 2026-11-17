# Back up SQLite to S3, R2, or Google Cloud Storage

Litebackup encrypts changes on your server before uploading them. App machines
need a Litebackup join token; bucket credentials belong on the server. Complete
[server installation](litebackup-server.md), then add storage in setup or the console.

## Provider setup

| Provider | Choose in Litebackup | Supply |
| --- | --- | --- |
| Amazon S3 | Amazon S3 | Bucket name, region, access key ID, secret access key |
| Cloudflare R2 | Cloudflare R2 | Bucket name, account ID, S3 access key ID and secret |
| Google Cloud Storage | Google Cloud Storage | Bucket name and interoperability HMAC access key pair |
| DigitalOcean Spaces, Backblaze B2, MinIO, other compatible stores | Other S3-compatible | Bucket name, S3 endpoint URL, key pair, region if required |

Use a dedicated bucket or a dedicated Litebackup prefix. Do not point two
independent servers at the same prefix: Litebackup records bucket ownership
and rejects a second owner. Recovery has a separate ownership flow.

The console tests reading, writing, listing, deleting, and bucket settings
before enabling the bucket. Passing that check is the way to verify a
particular S3-compatible provider; compatibility varies between providers.

## Amazon S3

Create a private bucket and a credential restricted to it. The console
provides a policy matching your bucket. Its required actions are:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:ListBucket", "s3:GetBucketVersioning"],
      "Resource": "arn:aws:s3:::YOUR_BUCKET"
    },
    {
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
      "Resource": "arn:aws:s3:::YOUR_BUCKET/*"
    }
  ]
}
```

Replace `YOUR_BUCKET` in both resources. See AWS's
[bucket policy examples](https://docs.aws.amazon.com/AmazonS3/latest/userguide/example-bucket-policies.html).
Setup requires an access key pair; it does not use an attached VM role.

## Cloudflare R2

Create an R2 bucket and a bucket-scoped credential with object read and write
permissions. Enter its **S3 access key pair**, not the Cloudflare API bearer
token. Copy the account ID from R2. Litebackup constructs the R2 endpoint.

[Create R2 S3 credentials](https://developers.cloudflare.com/r2/api/s3/tokens/).

## Google Cloud Storage

Create a private GCS bucket and an interoperability HMAC key for a service
account authorized to manage its objects. Supply the HMAC access ID and
secret in Litebackup. This integration uses GCS's S3-compatible XML API;
a service-account JSON file is not the credential format for these fields.

[Manage HMAC keys](https://cloud.google.com/storage/docs/authentication/managing-hmackeys).

## Retention and recovery

Set retention in Litebackup. Avoid independent lifecycle rules that delete
objects Litebackup still needs. The console checks settings such as versioning
and lifecycle compatibility; resolve its reported problems before enabling
storage. Do not manually rename or edit Litebackup objects.

The server and cloud version counters are separate. A cloud wait can include
the configured archive delay; increase a scheduled job's deadline accordingly.

Keep your recovery kit or recovery passphrase outside the server. Losing both
the key and its recovery material makes encrypted backups unusable, even if
the bucket survives. Follow the [server recovery procedure](sqlite-restore.md#recover-after-losing-the-server).
