pub fn clamp(value: usize, max: usize) -> usize {
    if value > max {
        max
    } else {
        value
    }
}

pub fn is_blank(text: &str) -> bool {
    text.trim().is_empty()
}

pub fn first_word(text: &str) -> &str {
    text.split_whitespace().next().unwrap_or("")
}
