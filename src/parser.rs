/// Splits input into trimmed, non-empty lines.
///
/// Blank and whitespace-only lines are dropped.
pub fn parse(input: &str) -> Vec<String> {
    input
        .lines()
        .map(str::trim)
        .filter(|line| !line.is_empty())
        .map(String::from)
        .collect()
}
