# Direct HTTP API credentials

Create an API key scoped to the namespace and access mode your integration
needs. Send it as a bearer token to your Litebackup server over HTTPS.

```sh
curl --fail --silent --show-error \
  --header "Authorization: Bearer ${LITEBACKUP_API_KEY}" \
  "https://${LITEBACKUP_HOST}/v1/settings/show"
```

For SDK setup, copy the join token and server address from **Machines → Add machine**.
