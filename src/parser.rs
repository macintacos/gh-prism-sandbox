/// Splits input into trimmed, non-empty lines.
///
/// Blank lines are dropped.
pub fn parse(input: &str) -> Vec<String> {
    input
        .lines()
        .map(str::trim)
        .filter(|line| !line.is_empty())
        .map(String::from)
        .collect()
}
