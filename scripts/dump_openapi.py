#!/usr/bin/env python3
"""Write FastAPI's OpenAPI document to a file, without starting a server.

V1 generated `webapp/src/api/schema.d.ts` by pointing `openapi-typescript` at a
running `127.0.0.1:8765`, which made the frontend's view of the API depend on
whoever happened to have a server up, with whichever database and settings file
that server had loaded. Nothing detected drift; the committed schema was simply
believed.

This script removes the server from the loop. `create_app()` is imported and its
`.openapi()` called in-process, against a throwaway SQLite file in a temporary
directory, so the document depends on the code alone and CI can regenerate it on
a clean runner and diff the result with `--check`. It writes nothing outside the
target file and reads no operator data.

Usage:
    python scripts/dump_openapi.py [--output frontend/openapi.json] [--check]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = REPO_ROOT / "frontend" / "openapi.json"


def build_document() -> dict:
    """Return the OpenAPI document of a freshly built app.

    The app is given a temporary database and settings path because
    `create_app()` bootstraps its schema eagerly; pointing it at the operator's
    real `data/tracker.db` would work but would also touch it, and a generator
    has no business writing to production data.
    """
    with tempfile.TemporaryDirectory(prefix="openapi-dump-") as tmp:
        scratch = Path(tmp)
        # `create_app` falls back to this variable before paths.DB_PATH; setting
        # it as well keeps the app off the real file even if the signature
        # changes.
        os.environ["JOBSEARCH_DB_PATH"] = str(scratch / "tracker.db")
        sys.path.insert(0, str(REPO_ROOT))
        from server.app import create_app

        app = create_app(db_path=scratch / "tracker.db",
                         settings_path=scratch / "settings.json")
        return app.openapi()


def serialize(document: dict) -> str:
    """Stable JSON: sorted keys and a trailing newline, so diffs mean something."""
    return json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT,
                        help="file to write (default: frontend/openapi.json)")
    parser.add_argument("--check", action="store_true",
                        help="exit 1 if the file on disk differs, writing nothing")
    args = parser.parse_args()

    rendered = serialize(build_document())

    if args.check:
        if not args.output.is_file():
            print(f"{args.output} does not exist; run scripts/dump_openapi.py",
                  file=sys.stderr)
            return 1
        if args.output.read_text(encoding="utf-8") != rendered:
            print(f"{args.output} is stale; run scripts/dump_openapi.py",
                  file=sys.stderr)
            return 1
        print(f"{args.output} matches the FastAPI application")
        return 0

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered, encoding="utf-8")
    paths = len(json.loads(rendered).get("paths", {}))
    print(f"wrote {args.output} ({paths} paths)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
