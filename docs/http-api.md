# Direct HTTP API credentials

Create an API key scoped to the namespace and access mode your integration
needs. Send it as a bearer token to your Litebackup server over HTTPS.

`LITEBACKUP_API_KEY` holds the key and the server address, `API_KEY@HOST`,
where HOST is a host or an `http://` or `https://` URL. The request sends the
key alone:

```sh
server="${LITEBACKUP_API_KEY##*@}"
case "${server}" in
  [Hh][Tt][Tt][Pp]://* | [Hh][Tt][Tt][Pp][Ss]://*) ;;
  *) server="https://${server}" ;;
esac
curl --fail --silent --show-error \
  --header "Authorization: Bearer ${LITEBACKUP_API_KEY%@*}" \
  "${server%/}/v1/settings/show"
```

The same `LITEBACKUP_API_KEY` sets up the SDKs and the client, which use the
key as it is. For them, a join token from **Machines → Add machine** works too.
