use crate::parser::parse;

pub struct Engine {
    limit: usize,
}

impl Engine {
    pub fn new(limit: usize) -> Self {
        Self { limit }
    }

    pub fn run(&self, input: &str) -> Vec<String> {
        let items = parse(input);
        items.into_iter().take(self.limit.max(1)).collect()
    }

    pub fn limit(&self) -> usize {
        self.limit
    }
}
