# Guide

Create an engine with a limit, then call `run` on some text.
The engine keeps the first lines up to the limit and ignores the rest.
A zero limit still keeps the first line.

Pass the result to `render` to get a bulleted list.
