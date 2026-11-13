/*
Design notes for the fixture engine.
The engine reads text and splits it into lines.
Each line is trimmed of surrounding whitespace first.
Empty lines never reach the limit check.
The limit counts kept lines, not input lines.
Rendering happens after the limit is applied.
The renderer only knows about bullets and headings.
Headings use a single hash and a space.
Bullets use a dash unless config says otherwise.
Config is read once at startup.
A timeout guards slow inputs.
The timeout is measured in milliseconds.
Generated schema code lives in its own folder.
Nobody edits generated code by hand.
Helpers stay small and have no state.
Tests live both inline and in the tests folder.
Inline tests cover private helpers.
Integration tests cover the public engine.
The lockfile pins every dependency.
Docs describe behaviour, not internals.
The guide is the entry point for new readers.
Notes like these are for maintainers.
They change rarely and stay in prose.
Nothing here is real; it stages review scenarios.
Line wrapping in these notes is deliberate.
Every sentence sits on a line of its own.
That keeps diffs of this comment narrow.
End of design notes.
*/

pub const NOTES_VERSION: u32 = 1;
