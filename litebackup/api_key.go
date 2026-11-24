package litebackup

import (
	"os"
	"regexp"
	"strings"
)

// The prefixes that tell Litebackup's credentials apart. Every API key
// starts with apiKeyPrefix; join tokens and invite tokens start with longer
// prefixes of it.
const (
	apiKeyPrefix    = "litebackup_"
	joinTokenPrefix = "litebackup_join_"
	invitePrefix    = "litebackup_invite_"
)

// credentialType is what a LITEBACKUP_API_KEY value holds, named as the
// contract fixtures name it.
type credentialType string

const (
	// credentialNone is an empty value: the daemon uses the key it saved.
	credentialNone credentialType = ""
	// credentialAPIKey is an API key the daemon uses as it is.
	credentialAPIKey credentialType = "api_key"
	// credentialJoinToken is a join token the daemon exchanges once for a
	// key of the machine.
	credentialJoinToken credentialType = "join_token"
)

// apiKeyCredential returns ConnectArgs.APIKey, or LITEBACKUP_API_KEY when
// it is empty.
func apiKeyCredential(args *ConnectArgs) string {
	if args.APIKey != "" {
		return args.APIKey
	}
	return os.Getenv(APIKeyEnvironment)
}

// checkCredential tells an API key from a join token by prefix, as the
// daemon does, and refuses what the daemon would refuse with
// invalid_argument: an invite token, a value with neither prefix, or one
// without @ and the server's address, a host or an http:// or https:// URL
// of one. No message repeats the credential.
func checkCredential(keyAndServer string) (credentialType, error) {
	keyAndServer = strings.TrimSpace(keyAndServer)
	if keyAndServer == "" {
		return credentialNone, nil
	}
	separator := strings.LastIndex(keyAndServer, "@")
	secret, server := keyAndServer[:max(separator, 0)], keyAndServer[separator+1:]
	if secret == "" || !serverAddressPattern.MatchString(server) {
		return credentialNone, invalidArgument(
			"expected <API_KEY>@<HOST>: an API key or a join token from Machines → Add machine, " +
				"then @ and the server's address: a host, or an http:// or https:// URL of one",
		)
	}
	switch {
	case strings.HasPrefix(secret, invitePrefix):
		return credentialNone, invalidArgument(
			"this is an invite token, which signs a person in to the console; " +
				"use an API key or a join token from Machines → Add machine",
		)
	case strings.HasPrefix(secret, joinTokenPrefix):
		return credentialJoinToken, nil
	case strings.HasPrefix(secret, apiKeyPrefix):
		return credentialAPIKey, nil
	}
	return credentialNone, invalidArgument(
		"expected an API key or a join token, which start with " + apiKeyPrefix +
			"; get one from Machines → Add machine",
	)
}

// serverAddressPattern is the server address a credential may name, as the
// daemon checks it: an optional http:// or https:// in any case, a host
// name or a bracketed IPv6 address, an optional port, and an optional path
// without a query or fragment, of ASCII URL characters and percent escapes.
var serverAddressPattern = regexp.MustCompile(
	`^(?:[Hh][Tt][Tt][Pp][Ss]?://)?(?:[A-Za-z0-9._-]+|\[[0-9A-Fa-f:.]+\])(?::[0-9]{1,5})?(?:/(?:[A-Za-z0-9._~!$&'()*+,;=:/-]|%[0-9A-Fa-f]{2})*)?$`,
)
