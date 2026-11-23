//! Data models.

/// A user record.
pub struct User {
    /// Stable identifier.
    pub id: i64,
    /// Display name.
    pub name: String,
}

impl User {
    /// Render the display name.
    pub fn display_name(&self) -> String {
        self.name.clone()
    }
}

/// A widget with a size.
pub struct Widget {
    pub size: i64,
    pub label: String,
}

/// A colour.
pub enum Colour {
    Red,
    Blue,
}

/// A runner trait.
pub trait Runner {
    /// Run one value.
    fn run(&self, value: i64) -> String;
}

/// Flush a cache.
pub fn flush_cache(force: bool, retries: u32) -> bool {
    force && retries > 0
}
