//! Pool adapters for SQLite drivers, each behind its feature: `rusqlite` and
//! `sqlx`. Both open an existing file read-write without creating it, roll
//! back an unfinished transaction when the pool closes the connection, and
//! never commit for the work.

#[cfg(feature = "rusqlite")]
pub mod rusqlite;
#[cfg(feature = "sqlx")]
pub mod sqlx;
