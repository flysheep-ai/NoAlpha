"""Language selection for user-facing strings the backend generates.

Narratives (decision summaries, theses, daily reports) are written at the moment
a cycle runs, not when a client fetches them — a background loop has no request
to read a header from. The language therefore travels with the thing being
described: a trading session carries `lang`, and everything that session
generates is written in it. Request-scoped surfaces (the conversation agent)
take `lang` straight from the caller.

Consequence, stated plainly: changing `lang` does not rewrite rows already in
SQLite. Existing narratives stay in the language they were generated in.
"""

from __future__ import annotations

DEFAULT_LANG = "zh-CN"


def is_zh(lang: str | None) -> bool:
    """True for any Chinese tag ('zh', 'zh-CN', 'zh-Hans', …)."""
    return (lang or DEFAULT_LANG).lower().startswith("zh")


def t(lang: str | None, zh: str, en: str) -> str:
    """Pick one of two literals. Kept deliberately dumb: no catalog, no keys.

    The alternative — a message catalog — buys nothing here because every one of
    these strings is interpolated with numbers at the call site anyway, and a key
    indirection would only move the f-string somewhere harder to read.
    """
    return zh if is_zh(lang) else en
