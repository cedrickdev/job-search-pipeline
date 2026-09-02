# tests/test_gitignore_guard.py
"""Mandate §2 / §11: the four sensitive paths must be gitignored."""
from pathlib import Path

REQUIRED = ["webapp/node_modules/", "webapp/dist/", ".firecrawl/", ".playwright-mcp/"]


def test_gitignore_covers_sensitive_paths():
    root = Path(__file__).resolve().parent.parent
    text = (root / ".gitignore").read_text()
    lines = {ln.strip() for ln in text.splitlines()}
    missing = [p for p in REQUIRED if p not in lines]
    assert not missing, f".gitignore missing: {missing}"


def test_server_in_packages():
    root = Path(__file__).resolve().parent.parent
    text = (root / "pyproject.toml").read_text()
    assert '"server"' in text, "server must be in [tool.setuptools] packages"
