//! Fixture crate for the migration overlay.

pub mod models;
pub mod service;

/// An old helper with no Python counterpart.
pub fn legacy_helper() -> u32 {
    1
}

fn private_helper() -> u32 {
    2
}
