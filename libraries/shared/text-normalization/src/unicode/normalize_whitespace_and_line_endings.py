"""Whitespace and line-ending normalization."""


def normalize(text):
    # Windows line endings become Unix ones.
    text = text.replace("\r\n", "\n")
    return " ".join(text.split())
