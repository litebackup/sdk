//! Writes `src/generated/models.rs` from the socket API spec,
//! `../client-openapi.yaml`: a struct for each response schema, and a string
//! newtype with a constant per value for each enum, so values a newer daemon
//! sends survive. Request schemas are left out: the crate's options build
//! request bodies. The spec's `ErrorCode` enum is `scripts/internal/errorcodes`'.
//! `scripts/test generate` runs it; CI checks that the committed file is
//! current.
//!
//! Usage: `cargo xtask [output file]`

use std::collections::BTreeSet;
use std::fmt::Write as _;
use std::path::{Path, PathBuf};
use std::process::{Command, ExitCode};

use yaml_rust2::{Yaml, YamlLoader};

/// Heads the file the generator writes.
const GENERATED_NOTICE: &str = "// Code generated from client-openapi.yaml by `cargo xtask`. DO NOT EDIT.";

/// How wide a doc comment's text may run, its `///` included.
const COMMENT_WIDTH: usize = 100;

/// Schemas that are not models: the error codes, the error body the client
/// decodes by hand, and the empty answer.
const SKIPPED_SCHEMAS: [&str; 3] = ["ErrorCode", "Error", "Empty"];

/// Schemas the crate decodes but does not export: answers it unwraps.
const CRATE_SCHEMAS: [&str; 2] = ["BackupList", "ReplicaResult"];

/// Schemas whose Rust name differs from the spec's: the crate's `Failover`
/// is the handle that follows one.
const RENAMED_SCHEMAS: [(&str, &str); 1] = [("Failover", "FailoverStatus")];

/// The name of each enum a schema writes inline, by schema and field. One
/// name may serve several fields with the same values.
const INLINE_ENUMS: [(&str, &str, &str); 5] = [
    ("DatabaseStatus", "role", "Role"),
    ("Failover", "role", "Role"),
    ("DatabaseStatus", "state", "DatabaseState"),
    ("AcquireResult", "decision", "AcquireDecision"),
    ("ReplicaRequest", "write_protection", "WriteProtection"),
];

/// Fields the spec types as plain strings that hold an enum's values.
const ENUM_FIELDS: [(&str, &str, &str); 1] = [("PrefixReplica", "write_protection", "WriteProtection")];

/// Rust keywords a wire name may be, written as raw identifiers.
const KEYWORDS: [&str; 6] = ["type", "match", "ref", "move", "use", "where"];

fn main() -> ExitCode {
    let manifest = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let crate_directory = manifest.parent().unwrap_or(&manifest).to_path_buf();
    let output =
        std::env::args_os().nth(1).map_or_else(|| crate_directory.join("src/generated/models.rs"), PathBuf::from);
    match write_models(&crate_directory.join("../client-openapi.yaml"), &output) {
        Ok(()) => ExitCode::SUCCESS,
        Err(message) => {
            eprintln!("xtask: {message}");
            ExitCode::FAILURE
        }
    }
}

/// Reads the spec and writes the formatted models file.
fn write_models(spec_path: &Path, output: &Path) -> Result<(), String> {
    let text =
        std::fs::read_to_string(spec_path).map_err(|error| format!("reading {}: {error}", spec_path.display()))?;
    let documents = YamlLoader::load_from_str(&text).map_err(|error| format!("parsing the spec: {error}"))?;
    let spec = documents.first().ok_or("the spec is empty")?;
    let schemas = entries(&spec["components"]["schemas"], "components.schemas")?;
    let source = Models::new(&schemas)?.source()?;
    std::fs::write(output, source).map_err(|error| format!("writing {}: {error}", output.display()))?;
    let status = Command::new("rustfmt")
        .args(["--edition", "2024"])
        .arg(output)
        .status()
        .map_err(|error| format!("running rustfmt: {error}"))?;
    if !status.success() {
        return Err(format!("rustfmt failed on {}: {status}", output.display()));
    }
    Ok(())
}

/// The key and value of each entry of a mapping, in the spec's order.
fn entries<'spec>(value: &'spec Yaml, place: &str) -> Result<Vec<(&'spec str, &'spec Yaml)>, String> {
    let mapping = value.as_hash().ok_or_else(|| format!("{place} is not a mapping"))?;
    mapping
        .iter()
        .map(|(key, entry)| {
            key.as_str().map(|name| (name, entry)).ok_or_else(|| format!("{place} has a key that is not text"))
        })
        .collect()
}

/// A mapping's text field, or None when it is missing.
fn text<'spec>(value: &'spec Yaml, key: &str) -> Option<&'spec str> {
    value[key].as_str()
}

/// The text values of an enum, or none when the value has no enum.
fn enum_values(value: &Yaml, place: &str) -> Result<Vec<String>, String> {
    match &value["enum"] {
        Yaml::BadValue => Ok(Vec::new()),
        Yaml::Array(values) => values
            .iter()
            .map(|entry| {
                entry.as_str().map(str::to_owned).ok_or_else(|| format!("{place} has an enum value that is not text"))
            })
            .collect(),
        _ => Err(format!("{place}'s enum is not a list")),
    }
}

/// The schema name a `$ref` points at.
fn reference_name(value: &Yaml) -> Option<&str> {
    text(value, "$ref").and_then(|reference| reference.strip_prefix("#/components/schemas/"))
}

/// A wire value such as `role_changed` or `download-started` as `ROLE_CHANGED`.
fn constant_name(wire_value: &str) -> String {
    wire_value
        .chars()
        .map(|character| if character.is_ascii_alphanumeric() { character.to_ascii_uppercase() } else { '_' })
        .collect()
}

/// A wire field name as a Rust field name: itself, or a raw identifier for a keyword.
fn field_name(wire_name: &str) -> String {
    if KEYWORDS.contains(&wire_name) { format!("r#{wire_name}") } else { wire_name.to_owned() }
}

/// Text as doc comment lines at the indent, wrapped to `COMMENT_WIDTH`, with
/// the characters rustdoc reads as links or HTML escaped. The spec's
/// lowercase fragments become sentences.
fn doc_comment(text: &str, indent: &str) -> String {
    let mut sentence = text.trim().to_owned();
    if let Some(first) = sentence.chars().next() {
        sentence.replace_range(..first.len_utf8(), &first.to_uppercase().to_string());
    }
    if !sentence.ends_with(['.', '!', '?', ':']) {
        sentence.push('.');
    }
    let escaped = sentence.replace('[', "\\[").replace(']', "\\]").replace('<', "\\<").replace('>', "\\>");
    let width = COMMENT_WIDTH - indent.len() - 4;
    let mut lines: Vec<String> = vec![String::new()];
    for word in escaped.split_whitespace() {
        let line_count = lines.len();
        let last = &mut lines[line_count - 1];
        if !last.is_empty() && last.len() + word.len() + 1 > width {
            lines.push(word.to_owned());
        } else {
            if !last.is_empty() {
                last.push(' ');
            }
            last.push_str(word);
        }
    }
    lines.iter().fold(String::new(), |mut comment, line| {
        let _ = writeln!(comment, "{indent}/// {line}");
        comment
    })
}

/// An enum newtype to write: its name, description, and values.
struct WireEnum {
    name: String,
    description: String,
    values: Vec<String>,
}

/// The models of the spec's schemas.
struct Models<'spec> {
    schemas: &'spec [(&'spec str, &'spec Yaml)],
    enums: Vec<WireEnum>,
}

impl<'spec> Models<'spec> {
    /// Collects the enums: the spec's string schemas, then the inline enums
    /// `INLINE_ENUMS` names.
    fn new(schemas: &'spec [(&'spec str, &'spec Yaml)]) -> Result<Self, String> {
        let mut models = Models { schemas, enums: Vec::new() };
        for (name, schema) in schemas {
            if SKIPPED_SCHEMAS.contains(name) || text(schema, "type") != Some("string") {
                continue;
            }
            let values = enum_values(schema, name)?;
            if !values.is_empty() {
                let description = text(schema, "description").unwrap_or_default().to_owned();
                models.add_enum(name, &description, &values);
            }
        }
        for (schema_name, field, enum_name) in INLINE_ENUMS {
            let property = &models.schema(schema_name)?["properties"][field];
            let values = enum_values(property, &format!("{schema_name}.{field}"))?;
            if values.is_empty() {
                return Err(format!("{schema_name}.{field} has no enum; update INLINE_ENUMS"));
            }
            let description = text(property, "description").unwrap_or_default().to_owned();
            models.add_enum(enum_name, &description, &values);
        }
        Ok(models)
    }

    /// Adds an enum's values, merging those of an enum of the same name.
    fn add_enum(&mut self, name: &str, description: &str, values: &[String]) {
        if let Some(existing) = self.enums.iter_mut().find(|wire_enum| wire_enum.name == name) {
            for value in values {
                if !existing.values.contains(value) {
                    existing.values.push(value.clone());
                }
            }
            return;
        }
        self.enums.push(WireEnum {
            name: name.to_owned(),
            description: description.to_owned(),
            values: values.to_vec(),
        });
    }

    fn schema(&self, name: &str) -> Result<&'spec Yaml, String> {
        self.schemas
            .iter()
            .find(|(schema_name, _)| *schema_name == name)
            .map(|(_, schema)| *schema)
            .ok_or_else(|| format!("the spec has no schema {name}"))
    }

    /// The Rust name of a schema.
    fn type_name(name: &str) -> String {
        RENAMED_SCHEMAS
            .iter()
            .find(|(spec_name, _)| *spec_name == name)
            .map_or_else(|| name.to_owned(), |(_, rust_name)| (*rust_name).to_owned())
    }

    /// Whether a schema becomes a struct: an object that is not a request or skipped.
    fn written_struct(name: &str, schema: &Yaml) -> bool {
        text(schema, "type") == Some("object") && !SKIPPED_SCHEMAS.contains(&name) && !name.ends_with("Request")
    }

    /// The whole file.
    fn source(&self) -> Result<String, String> {
        let mut source = format!(
            "{GENERATED_NOTICE}\n\n\
             //! The socket API's answers, written from `client-openapi.yaml`. Fields keep\n\
             //! their wire names. Unknown fields are ignored, and a required field the\n\
             //! daemon leaves out or sends as null holds its type's default, so a newer\n\
             //! daemon's answers keep decoding; a field of the wrong type is\n\
             //! `invalid_response`. Optional fields are `Option`s, lists are empty when\n\
             //! left out, and enums are string newtypes, so values a newer daemon sends\n\
             //! survive.\n\
             #![allow(\n    clippy::doc_markdown,\n    reason = \"the descriptions are the spec's prose\"\n)]\n\n\
             use std::borrow::Cow;\nuse std::fmt;\n\n\
             use serde::{{Deserialize, Serialize}};\n\n\
             use crate::model_fields::null_as_default;\n"
        );
        for wire_enum in &self.enums {
            source.push('\n');
            source.push_str(&enum_source(wire_enum));
        }
        for (name, schema) in self.schemas {
            if Self::written_struct(name, schema) {
                source.push('\n');
                source.push_str(&self.struct_source(name, schema)?);
            }
        }
        Ok(source)
    }

    /// The Rust type of one field, without `Option`, and whether it is a list.
    fn field_type(&self, schema_name: &str, field: &str, property: &Yaml) -> Result<(String, bool), String> {
        if let Some((_, _, enum_name)) = INLINE_ENUMS
            .iter()
            .chain(ENUM_FIELDS.iter())
            .find(|(schema, name, _)| *schema == schema_name && *name == field)
        {
            return Ok(((*enum_name).to_owned(), false));
        }
        if !enum_values(property, field)?.is_empty() {
            return Err(format!("{schema_name}.{field} is an inline enum; name it in INLINE_ENUMS"));
        }
        if let Some(target) = reference_name(property) {
            let target_schema = self.schema(target)?;
            if SKIPPED_SCHEMAS.contains(&target) {
                // The error codes stay strings, so codes a newer daemon sends survive.
                return Ok(("String".to_owned(), false));
            }
            return match text(target_schema, "type") {
                Some("object") => Ok((Self::type_name(target), false)),
                Some("string") if !enum_values(target_schema, target)?.is_empty() => Ok((target.to_owned(), false)),
                _ => self.field_type(schema_name, field, target_schema),
            };
        }
        match text(property, "type") {
            Some("string") if text(property, "format") == Some("date-time") => {
                Ok(("jiff::Timestamp".to_owned(), false))
            }
            Some("string") => Ok(("String".to_owned(), false)),
            Some("integer") => Ok(("i64".to_owned(), false)),
            Some("boolean") => Ok(("bool".to_owned(), false)),
            Some("array") => {
                let (item, _) = self.field_type(schema_name, field, &property["items"])?;
                Ok((item, true))
            }
            _ => Err(format!("{schema_name}.{field} has a schema this generator does not write")),
        }
    }

    /// The doc text of a field: its description, else that of the schema it refers to.
    fn field_description(&self, field: &str, property: &Yaml) -> String {
        if let Some(description) = text(property, "description") {
            return description.to_owned();
        }
        if let Some(target) = reference_name(property)
            && let Some(description) = self.schema(target).ok().and_then(|schema| text(schema, "description"))
        {
            return description.to_owned();
        }
        format!("The daemon's `{field}`.")
    }

    fn struct_source(&self, name: &str, schema: &Yaml) -> Result<String, String> {
        let type_name = Self::type_name(name);
        let required: BTreeSet<&str> = match &schema["required"] {
            Yaml::Array(names) => names.iter().filter_map(Yaml::as_str).collect(),
            _ => BTreeSet::new(),
        };
        let visibility = if CRATE_SCHEMAS.contains(&name) { "pub(crate)" } else { "pub" };
        let description =
            text(schema, "description").map_or_else(|| format!("A `{name}` the daemon answers with."), str::to_owned);
        let mut source = doc_comment(&description, "");
        source.push_str("#[derive(Debug, Clone, PartialEq, Eq, Default, Serialize, Deserialize)]\n");
        source.push_str("#[serde(default)]\n#[non_exhaustive]\n");
        let properties = match &schema["properties"] {
            Yaml::BadValue => Vec::new(),
            value => entries(value, name)?,
        };
        let _ = writeln!(source, "{visibility} struct {type_name} {{");
        for (field, property) in properties {
            let (field_type, list) = self.field_type(name, field, property)?;
            source.push_str(&doc_comment(&self.field_description(field, property), "    "));
            let rust_type = if list {
                source.push_str("    #[serde(deserialize_with = \"null_as_default\")]\n");
                format!("Vec<{field_type}>")
            } else if required.contains(field) && field_type != "jiff::Timestamp" {
                source.push_str("    #[serde(deserialize_with = \"null_as_default\")]\n");
                field_type
            } else {
                source.push_str("    #[serde(skip_serializing_if = \"Option::is_none\")]\n");
                format!("Option<{field_type}>")
            };
            let _ = writeln!(source, "    {visibility} {}: {rust_type},", field_name(field));
        }
        source.push_str("}\n");
        Ok(source)
    }
}

/// One enum newtype: its constants, `new`, `as_str`, and `Display`.
fn enum_source(wire_enum: &WireEnum) -> String {
    let WireEnum { name, description, values } = wire_enum;
    let description = if description.is_empty() { format!("A `{name}` value.") } else { description.clone() };
    let mut source = doc_comment(&description, "");
    source.push_str("///\n/// Values this SDK does not know are kept as they are, so compare with `==`\n");
    source.push_str("/// against the constants or read [`as_str`](Self::as_str).\n");
    source.push_str("#[derive(Debug, Clone, PartialEq, Eq, Hash, Default, Serialize, Deserialize)]\n");
    source.push_str("#[serde(transparent)]\n");
    let _ = writeln!(source, "pub struct {name}(pub(crate) Cow<'static, str>);\n");
    let _ = writeln!(source, "impl {name} {{");
    for value in values {
        let _ = writeln!(source, "    /// `{value}`.");
        let _ = writeln!(source, "    pub const {}: Self = Self(Cow::Borrowed({value:?}));", constant_name(value));
    }
    source.push_str("\n    /// A value as the daemon sends it, known to this SDK or not.\n");
    source.push_str("    #[must_use]\n");
    source.push_str(
        "    pub fn new(value: impl Into<String>) -> Self {\n        Self(Cow::Owned(value.into()))\n    }\n\n",
    );
    source.push_str("    /// The wire value.\n    #[must_use]\n");
    source.push_str("    pub fn as_str(&self) -> &str {\n        &self.0\n    }\n}\n\n");
    let _ = writeln!(source, "impl fmt::Display for {name} {{");
    source.push_str("    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {\n");
    source.push_str("        formatter.write_str(&self.0)\n    }\n}\n");
    source
}
