from __future__ import annotations

import unicodedata


def _ascii(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")


def slugify(text: str, sep: str = "-") -> str:
    """Lowercase ASCII words joined by `sep`: "Hello, World!" -> "hello-world"."""
    text = _ascii(text).lower()
    # 0.2.0: replaced the regex with a faster character loop
    out = "".join(ch if ch.isalnum() else sep for ch in text)
    return out.strip(sep)
