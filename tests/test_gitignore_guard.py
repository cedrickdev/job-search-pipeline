# tests/test_gitignore_guard.py
"""Mandate §2 / §11: the four sensitive paths must be gitignored."""
from pathlib import Path

REQUIRED = ["frontend/node_modules/", "frontend/.output/", ".firecrawl/", ".playwright-mcp/"]


def test_gitignore_covers_sensitive_paths():
    root = Path(__file__).resolve().parent.parent
    text = (root / ".gitignore").read_text()
    lines = {ln.strip() for ln in text.splitlines()}
    missing = [p for p in REQUIRED if p not in lines]
    assert not missing, f".gitignore missing: {missing}"


def test_gitignore_ignores_the_generated_dist_symlink():
    """`nuxt generate` recreates frontend/dist as a symlink to .output/public.

    Its target is an absolute path on the machine that ran the build, so committing
    it would break every other checkout. Git matches a symlink as a file, which is
    why the pattern must not end in a slash.
    """
    root = Path(__file__).resolve().parent.parent
    lines = {ln.strip() for ln in (root / ".gitignore").read_text().splitlines()}
    assert "frontend/dist" in lines, "frontend/dist must be ignored, without a trailing slash"


def test_server_in_packages():
    root = Path(__file__).resolve().parent.parent
    text = (root / "pyproject.toml").read_text()
    assert '"server"' in text, "server must be in [tool.setuptools] packages"
