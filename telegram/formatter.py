from typing import Any


def bold(s: str) -> str:
    return f"*{s}*"


def code(s: str) -> str:
    return f"`{s}`"


def italic(s: str) -> str:
    return f"_{s}_"


def link(text: str, url: str) -> str:
    return f"[{text}]({url})"


def fmt_balance(value: float) -> str:
    return f"${value:,.2f}"


def fmt_pnl(value: float) -> str:
    return f"{value:+,.2f}"


def fmt_ratio(value: float) -> str:
    return f"{value:.2%}"


def fmt_time(seconds: float) -> str:
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def key_val(key: str, val: str) -> str:
    return f"{key}: {val}"


def listify(items: list[str], bullet: str = "•") -> str:
    return "\n".join(f"{bullet} {i}" for i in items)
