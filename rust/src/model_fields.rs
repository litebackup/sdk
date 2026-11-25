//! Decoding helpers for the generated models.

use serde::{Deserialize, Deserializer};

/// Decodes a required field, reading null as the type's default, as a field
/// the daemon leaves out reads.
pub(crate) fn null_as_default<'de, D, T>(deserializer: D) -> Result<T, D::Error>
where
    D: Deserializer<'de>,
    T: Deserialize<'de> + Default,
{
    Ok(Option::<T>::deserialize(deserializer)?.unwrap_or_default())
}
