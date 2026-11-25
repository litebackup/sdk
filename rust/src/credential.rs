//! Telling an API key from a join token, as the daemon does. A credential is
//! an API key, or a join token the daemon exchanges once for a key of the
//! machine, then `@` and the server's address.

use crate::error::{Error, Result};

/// Holds the credential a daemon uses when it starts.
pub(crate) const API_KEY_ENVIRONMENT: &str = "LITEBACKUP_API_KEY";
const API_KEY_PREFIX: &str = "litebackup_";
const JOIN_TOKEN_PREFIX: &str = "litebackup_join_";
const INVITE_PREFIX: &str = "litebackup_invite_";

/// What a credential is.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum CredentialType {
    ApiKey,
    JoinToken,
}

impl CredentialType {
    /// The name the contract fixtures use.
    #[cfg(test)]
    pub(crate) fn as_str(self) -> &'static str {
        match self {
            Self::ApiKey => "api_key",
            Self::JoinToken => "join_token",
        }
    }
}

/// The characters spaces around a credential may be, as PHP's `trim` and
/// the other SDKs read them.
fn trimmed(character: char) -> bool {
    matches!(character, ' ' | '\t' | '\n' | '\r' | '\0' | '\u{0B}' | '\u{0C}')
}

/// Tells an API key from a join token by prefix, or `None` for an empty
/// credential, which leaves the daemon its saved key. Fails with
/// `invalid_argument`, without repeating the credential, for an invite
/// token, a credential with neither prefix, or one without `@` and the
/// server's address.
pub(crate) fn credential_type(key_and_server: Option<&str>) -> Result<Option<CredentialType>> {
    let stripped = key_and_server.unwrap_or_default().trim_matches(trimmed);
    if stripped.is_empty() {
        return Ok(None);
    }
    let (secret, server) = stripped.rsplit_once('@').unwrap_or_default();
    if secret.is_empty() || !server_address(server) {
        return Err(Error::invalid_argument(
            "expected <API_KEY>@<HOST>: an API key or a join token from Machines → Add machine, \
             then @ and the server's address: a host, or an http:// or https:// URL of one",
        ));
    }
    if secret.starts_with(INVITE_PREFIX) {
        return Err(Error::invalid_argument(
            "this is an invite token, which signs a person in to the console; \
             use an API key or a join token from Machines → Add machine",
        ));
    }
    if secret.starts_with(JOIN_TOKEN_PREFIX) {
        return Ok(Some(CredentialType::JoinToken));
    }
    if secret.starts_with(API_KEY_PREFIX) {
        return Ok(Some(CredentialType::ApiKey));
    }
    Err(Error::invalid_argument(format!(
        "expected an API key or a join token, which start with {API_KEY_PREFIX}; get one from Machines → Add machine"
    )))
}

/// Whether text is a server address as the daemon checks it: an optional
/// `http://` or `https://` in any case, a host name or a bracketed IPv6
/// address, an optional port, and an optional path without a query or
/// fragment, of ASCII URL characters and percent escapes.
fn server_address(address: &str) -> bool {
    let mut rest = address;
    for scheme in ["https://", "http://"] {
        if rest.len() >= scheme.len()
            && rest.is_char_boundary(scheme.len())
            && rest[..scheme.len()].eq_ignore_ascii_case(scheme)
        {
            rest = &rest[scheme.len()..];
            break;
        }
    }
    let host_end = if let Some(bracketed) = rest.strip_prefix('[') {
        let Some(close) = bracketed.find(']') else {
            return false;
        };
        let inside = &bracketed[..close];
        if inside.is_empty() || !inside.bytes().all(|byte| byte.is_ascii_hexdigit() || byte == b':' || byte == b'.') {
            return false;
        }
        close + 2
    } else {
        let length =
            rest.bytes().take_while(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-')).count();
        if length == 0 {
            return false;
        }
        length
    };
    rest = &rest[host_end..];
    if let Some(port) = rest.strip_prefix(':') {
        let digits = port.bytes().take_while(u8::is_ascii_digit).count();
        if digits == 0 || digits > 5 {
            return false;
        }
        rest = &port[digits..];
    }
    let Some(path) = rest.strip_prefix('/') else {
        return rest.is_empty();
    };
    let bytes = path.as_bytes();
    let mut index = 0;
    while index < bytes.len() {
        let byte = bytes[index];
        if byte == b'%' {
            // A percent escape is two hex digits, both there.
            if !(bytes.get(index + 1).is_some_and(u8::is_ascii_hexdigit)
                && bytes.get(index + 2).is_some_and(u8::is_ascii_hexdigit))
            {
                return false;
            }
            index += 3;
            continue;
        }
        if !(byte.is_ascii_alphanumeric() || b"._~!$&'()*+,;=:/-".contains(&byte)) {
            return false;
        }
        index += 1;
    }
    true
}
