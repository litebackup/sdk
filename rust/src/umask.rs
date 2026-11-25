//! The caller's file mode creation mask, which acquire and restore send.

use crate::error::{Error, Result};

/// This process's umask, so a file the daemon creates gets the bits a file
/// this process created would. On Linux it reads the `Umask` line of
/// `/proc/self/status`, which leaves the mask alone. Elsewhere, as on macOS,
/// it sets the mask and puts it back at once, under a lock all of this
/// crate's reads take, so a file another thread creates in that moment could
/// get the mask 0, as in the Go and Python SDKs.
pub(crate) fn current_umask() -> u32 {
    static READ_LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());
    if let Some(mask) = std::fs::read_to_string("/proc/self/status").ok().as_deref().and_then(status_umask) {
        return mask;
    }
    let _guard = READ_LOCK.lock().unwrap_or_else(std::sync::PoisonError::into_inner);
    let previous = rustix::process::umask(rustix::fs::Mode::empty());
    rustix::process::umask(previous);
    previous.bits()
}

/// The octal `Umask` line of a `/proc` status file, or `None` without one.
pub(crate) fn status_umask(status: &str) -> Option<u32> {
    let line = status.lines().find_map(|line| line.strip_prefix("Umask:"))?;
    u32::from_str_radix(line.trim(), 8).ok().filter(|mask| *mask <= 0o777)
}

/// The umask a request sends: the one given, checked, or this process's.
pub(crate) fn request_umask(umask: Option<u32>) -> Result<u32> {
    match umask {
        None => Ok(current_umask()),
        Some(mask) if mask <= 0o777 => Ok(mask),
        Some(_) => Err(Error::invalid_argument("umask must be between 0 and 0777")),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_status_line_is_octal() {
        assert_eq!(status_umask("Name:\tx\nUmask:\t0022\nState:\tR\n"), Some(0o22));
        assert_eq!(status_umask("Umask:\t0000\n"), Some(0));
        assert_eq!(status_umask("Name:\tx\n"), None);
        assert_eq!(status_umask("Umask:\t1777\n"), None);
    }
}
