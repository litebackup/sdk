//! Builder setters for the options structs.

/// Writes a setter for each listed field of an options struct, which takes
/// the setter's type and stores the conversion: `timeout: Duration =>
/// Some(timeout)` writes `fn timeout(self, timeout: Duration) -> Self`.
macro_rules! option_setters {
    ($options:ident { $($field:ident: $setter_type:ty => $stored:expr),* $(,)? }) => {
        impl $options {
            $(
                #[doc = concat!("Sets [`", stringify!($field), "`](Self::", stringify!($field), ").")]
                #[must_use]
                pub fn $field(mut self, $field: $setter_type) -> Self {
                    self.$field = $stored;
                    self
                }
            )*
        }
    };
}

pub(crate) use option_setters;
