use prism_fixture::engine::Engine;

#[test]
fn keeps_up_to_limit() {
    let engine = Engine::new(2);
    assert_eq!(engine.run("a\nb\nc").len(), 2);
}
