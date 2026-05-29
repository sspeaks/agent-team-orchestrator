from __future__ import annotations

from importlib import resources


def read_static_text(name: str) -> str:
    package = "agent_team.web_static"
    if hasattr(resources, "files"):
        return resources.files(package).joinpath(name).read_text(encoding="utf-8")
    return resources.read_text(package, name, encoding="utf-8")


def app_js() -> str:
    return read_static_text("app.js")


def styles_css() -> str:
    return read_static_text("styles.css")
