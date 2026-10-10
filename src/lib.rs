pub mod engine;
pub mod notes;
pub mod parser;
pub mod render;

pub fn version() -> &'static str {
    "0.1.0"
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reports_version() {
        assert_eq!(version(), "0.1.0");
    }
}
