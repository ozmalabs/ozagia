//! Service functions.

/// Fetch a user's display name.
pub async fn fetch_user(id: u64) -> Option<String> {
    None
}

/// Compute a checksum.
pub fn checksum(data: &[u8]) -> u64 {
    data.iter().map(|byte| u64::from(*byte)).sum()
}

/// A Rust-only helper.
pub fn service_extra() -> bool {
    true
}
