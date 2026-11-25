//! Verifying Litebackup's signed package metadata, as
//! <https://litebackup.com/pkg/metadata> serves it (feature `release`), for
//! an app that downloads the litebackup binary itself. A release is always
//! read from its signed document, never from the unsigned listing.
//!
//! ```no_run
//! # fn example(metadata: &str) -> Result<(), litebackup::release::ReleaseError> {
//! use litebackup::release::{ReleaseSearch, find_release};
//!
//! let release = find_release(metadata, &ReleaseSearch::default().version("1.5.0"))?;
//! if let Some(file) = release.file("litebackup-linux-amd64") {
//!     println!("{} {} {}", file.url, file.sha256, file.size);
//! }
//! # Ok(())
//! # }
//! ```

use std::fmt;

use base64::Engine as _;
use base64::engine::{DecodePaddingMode, GeneralPurpose, GeneralPurposeConfig};
use ed25519_dalek::{Signature, VerifyingKey};
use serde_json::{Map, Value};

/// Litebackup's Ed25519 release keys by key ID, base64url, as the
/// `litebackup` command checks them.
pub const RELEASE_KEYS: [(&str, &str); 1] =
    [("01a1ead6-4b94-79ca-81f4-3a67c112bc0b", "t3C_3l71PimyhkPZOzJ_Xi4dOuHJ1CmSdDZ8lTm0vNI")];
/// The package of the `litebackup` command.
pub const COMMAND_PACKAGE: &str = "cli";
/// The channel releases are read from unless a version is chosen.
pub const STABLE_CHANNEL: &str = "stable";
const SIGNATURE_CONTEXT: &[u8] = b"clor-apps-v1\0";
const SIGNATURE_SEPARATOR: &[u8] = b"\0";

/// Base64url with or without padding, as the metadata writes it.
const BASE64URL: GeneralPurpose = GeneralPurpose::new(
    &base64::alphabet::URL_SAFE,
    GeneralPurposeConfig::new().with_decode_padding_mode(DecodePaddingMode::Indifferent),
);

/// Which release [`find_release`] picks: the release with `version`, or else
/// the newest on `channel`, of `package`.
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub struct ReleaseSearch {
    /// The package's slug; [`COMMAND_PACKAGE`] by default.
    pub package: String,
    /// The release's version; the newest on `channel` when unset.
    pub version: Option<String>,
    /// The channel; [`STABLE_CHANNEL`] by default.
    pub channel: String,
}

impl Default for ReleaseSearch {
    fn default() -> Self {
        Self { package: COMMAND_PACKAGE.to_owned(), version: None, channel: STABLE_CHANNEL.to_owned() }
    }
}

impl ReleaseSearch {
    /// Picks the release with this version.
    #[must_use]
    pub fn version(mut self, version: impl Into<String>) -> Self {
        self.version = Some(version.into());
        self
    }

    /// Picks the newest release on this channel.
    #[must_use]
    pub fn channel(mut self, channel: impl Into<String>) -> Self {
        self.channel = channel.into();
        self
    }

    /// Picks a release of this package.
    #[must_use]
    pub fn package(mut self, package: impl Into<String>) -> Self {
        self.package = package.into();
        self
    }
}

/// A release read from its verified signed document.
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub struct PackageRelease {
    /// The release's version.
    pub version: String,
    /// The channels it is on.
    pub channels: Vec<String>,
    /// Its published files.
    pub files: Vec<ReleaseFile>,
}

impl PackageRelease {
    /// The file with this name, such as `litebackup-linux-amd64`.
    #[must_use]
    pub fn file(&self, name: &str) -> Option<&ReleaseFile> {
        self.files.iter().find(|file| file.name == name)
    }
}

/// One published file of a release.
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub struct ReleaseFile {
    /// The file's name in the release.
    pub name: String,
    /// The name to save it under.
    pub filename: String,
    /// Where to download it.
    pub url: String,
    /// Its SHA-256, in hexadecimal; check a download against it.
    pub sha256: String,
    /// Its size in bytes.
    pub size: u64,
    /// The operating system it runs on, or empty.
    pub os: String,
    /// The architecture it runs on, or empty.
    pub architecture: String,
}

/// Why [`find_release`] found no release.
#[derive(Debug, Clone, PartialEq, Eq)]
#[non_exhaustive]
pub enum ReleaseError {
    /// The metadata lists no such package or release, or is not JSON.
    NotListed(String),
    /// The release's signature does not verify against a release key, or its
    /// signed document does not match the listed version: the metadata was
    /// changed after Litebackup signed it.
    Signature,
}

impl fmt::Display for ReleaseError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NotListed(reason) => formatter.write_str(reason),
            Self::Signature => {
                formatter.write_str("the release's signature does not verify against Litebackup's release keys")
            }
        }
    }
}

impl std::error::Error for ReleaseError {}

/// Reads package metadata and returns the release `search` picks, from its
/// signed document.
///
/// # Errors
///
/// [`ReleaseError::Signature`] when the signature does not verify or the
/// signed version is not the listed one, and [`ReleaseError::NotListed`] for
/// a missing package or release.
pub fn find_release(metadata: &str, search: &ReleaseSearch) -> Result<PackageRelease, ReleaseError> {
    find_release_with(metadata, search, &RELEASE_KEYS)
}

fn find_release_with(
    metadata: &str,
    search: &ReleaseSearch,
    keys: &[(&str, &str)],
) -> Result<PackageRelease, ReleaseError> {
    let listing: Value = serde_json::from_str(metadata)
        .map_err(|error| ReleaseError::NotListed(format!("the package metadata is not JSON: {error}")))?;
    let listed = entries(&listing["packages"])
        .find(|candidate| candidate["slug"].as_str() == Some(search.package.as_str()))
        .ok_or_else(|| ReleaseError::NotListed(format!("no package {}", search.package)))?;
    let chosen = entries(&listed["releases"])
        .find(|release| match &search.version {
            Some(version) => release["version"].as_str() == Some(version.as_str()),
            None => entries(&release["channels"]).any(|channel| channel.as_str() == Some(search.channel.as_str())),
        })
        .ok_or_else(|| {
            let wanted = search.version.clone().unwrap_or_else(|| format!("on {}", search.channel));
            ReleaseError::NotListed(format!("package {} has no release {wanted}", search.package))
        })?;
    let document = verify(&chosen["signature"], keys)?;
    let version = document.get("version").and_then(Value::as_str).ok_or(ReleaseError::Signature)?;
    if chosen["version"].as_str() != Some(version) {
        return Err(ReleaseError::Signature);
    }
    let files =
        entries(document.get("artifacts").unwrap_or(&Value::Null)).map(release_file).collect::<Result<Vec<_>, _>>()?;
    let channels = entries(&chosen["channels"]).filter_map(Value::as_str).map(str::to_owned).collect();
    Ok(PackageRelease { version: version.to_owned(), channels, files })
}

/// The entries of a JSON array, none for anything else.
fn entries(value: &Value) -> impl Iterator<Item = &Value> {
    value.as_array().into_iter().flatten()
}

fn release_file(artifact: &Value) -> Result<ReleaseFile, ReleaseError> {
    let text = |key: &str| artifact[key].as_str().map(str::to_owned).ok_or(ReleaseError::Signature);
    let optional_text = |key: &str| artifact[key].as_str().unwrap_or_default().to_owned();
    Ok(ReleaseFile {
        name: text("name")?,
        filename: text("filename")?,
        url: text("url")?,
        sha256: text("sha256")?,
        size: artifact["size"].as_u64().ok_or(ReleaseError::Signature)?,
        os: optional_text("os"),
        architecture: optional_text("architecture"),
    })
}

/// The signed document, decoded, once its signature matches one of the keys.
fn verify(signed: &Value, keys: &[(&str, &str)]) -> Result<Map<String, Value>, ReleaseError> {
    let key_id = signed["key_id"].as_str().ok_or(ReleaseError::Signature)?;
    let (_, public_key) = keys.iter().find(|(id, _)| *id == key_id).ok_or(ReleaseError::Signature)?;
    let decode = |text: Option<&str>| {
        BASE64URL.decode(text.ok_or(ReleaseError::Signature)?).map_err(|_| ReleaseError::Signature)
    };
    let public_key: [u8; 32] = decode(Some(public_key))?.try_into().map_err(|_| ReleaseError::Signature)?;
    let document = decode(signed["document"].as_str())?;
    let signature: [u8; 64] = decode(signed["signature"].as_str())?.try_into().map_err(|_| ReleaseError::Signature)?;
    let message = [SIGNATURE_CONTEXT, key_id.as_bytes(), SIGNATURE_SEPARATOR, &document].concat();
    VerifyingKey::from_bytes(&public_key)
        .map_err(|_| ReleaseError::Signature)?
        .verify_strict(&message, &Signature::from_bytes(&signature))
        .map_err(|_| ReleaseError::Signature)?;
    match serde_json::from_slice(&document) {
        Ok(Value::Object(document)) => Ok(document),
        _ => Err(ReleaseError::Signature),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn metadata(name: &str) -> String {
        let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../testdata/contract/binary").join(name);
        std::fs::read_to_string(path).expect("the metadata fixture")
    }

    #[test]
    fn a_signed_release_is_read_from_its_document() {
        let release = find_release(&metadata("package-metadata.json"), &ReleaseSearch::default().version("0.3.30"));
        let release = release.expect("a verified release");
        assert_eq!(release.version, "0.3.30");
        let file = release.file("litebackup-linux-amd64").expect("the linux file");
        assert_eq!(file.filename, "litebackup-linux-amd64");
        assert_eq!(file.size, 47_322_128);
        assert!(release.file("missing").is_none());
    }

    #[test]
    fn tampered_metadata_is_refused() {
        let found =
            find_release(&metadata("package-metadata-tampered.json"), &ReleaseSearch::default().version("0.3.30"));
        assert_eq!(found, Err(ReleaseError::Signature));
    }

    #[test]
    fn a_missing_release_is_not_listed() {
        let found = find_release(&metadata("package-metadata.json"), &ReleaseSearch::default().version("9.9.9"));
        assert!(matches!(found, Err(ReleaseError::NotListed(_))));
        let found = find_release(&metadata("package-metadata.json"), &ReleaseSearch::default().package("none"));
        assert!(matches!(found, Err(ReleaseError::NotListed(_))));
    }

    #[test]
    fn an_unknown_key_is_refused() {
        let found =
            find_release_with(&metadata("package-metadata.json"), &ReleaseSearch::default().version("0.3.30"), &[]);
        assert_eq!(found, Err(ReleaseError::Signature));
    }
}
