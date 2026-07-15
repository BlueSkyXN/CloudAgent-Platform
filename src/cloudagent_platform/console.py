from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


WEB_ROOT = Path(__file__).resolve().parent / "web"


@dataclass(frozen=True)
class ConsoleAsset:
    content: bytes
    content_type: str
    cache_control: str


def get_console_asset(path: str) -> ConsoleAsset | None:
    """Return only the named, package-local console files exposed by the HTTP server."""
    routes = {
        "/": ("index.html", "text/html; charset=utf-8", "no-store"),
        "/admin": ("index.html", "text/html; charset=utf-8", "no-store"),
        "/admin/assets/console.css": ("console.css", "text/css; charset=utf-8", "no-cache"),
        "/admin/assets/console.js": (
            "console.js",
            "application/javascript; charset=utf-8",
            "no-cache",
        ),
        "/admin/assets/mark.svg": ("mark.svg", "image/svg+xml", "no-cache"),
    }
    route = routes.get(path)
    if route is None:
        return None
    filename, content_type, cache_control = route
    try:
        content = (WEB_ROOT / filename).read_bytes()
    except FileNotFoundError:
        return None
    return ConsoleAsset(content=content, content_type=content_type, cache_control=cache_control)
