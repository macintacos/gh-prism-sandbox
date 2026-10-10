/// Renders items as a bulleted list.
pub fn render(items: &[String]) -> String {
    items
        .iter()
        .map(|item| format!("- {item}"))
        .collect::<Vec<_>>()
        .join("\n")
}

/// Renders a heading line.
pub fn heading(text: &str) -> String {
    format!("# {text}")
}

/// Renders a horizontal rule.
pub fn rule() -> String {
    String::from("---")
}

/// Renders a blank separator.
pub fn blank() -> String {
    String::new()
}

/// Renders a count summary.
pub fn summary(count: usize) -> String {
    format!("{count} items")
}
