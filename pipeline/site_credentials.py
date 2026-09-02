"""Local, gitignored store of generated credentials for third-party sites
the applier creates accounts on (e.g. jobup.ch, an Umantis-hosted employer).
Never synced anywhere; read/write only touches data/site_credentials.json.

This module must stay free of literal secrets: it is committed source. The
password for a new account comes from SITE_ACCOUNT_PASSWORD when set, and is
otherwise generated per site, which is the safer default anyway. Either way it
lands in data/site_credentials.json, so you can look it up to log in by hand.
"""
import json
import os
import secrets
import string

from pipeline import paths

CREDENTIALS_PATH = paths.DATA_DIR / "site_credentials.json"
_ALPHABET = string.ascii_letters + string.digits + "!@#%^&*-_+="
_PASSWORD_ENV = "SITE_ACCOUNT_PASSWORD"


def _generate_password(length: int = 20) -> str:
    import re
    while True:
        pwd = "".join(secrets.choice(_ALPHABET) for _ in range(length))
        if (re.search(r"[a-z]", pwd) and re.search(r"[A-Z]", pwd)
                and re.search(r"[0-9]", pwd) and re.search(r"[!@#%^&*\-_+=]", pwd)):
            return pwd


def _load() -> dict:
    if CREDENTIALS_PATH.exists():
        return json.loads(CREDENTIALS_PATH.read_text())
    return {}


def _save(data: dict) -> None:
    CREDENTIALS_PATH.parent.mkdir(parents=True, exist_ok=True)
    CREDENTIALS_PATH.write_text(json.dumps(data, indent=2) + "\n")


def default_password() -> str:
    """The password to use for a site seen for the first time."""
    return os.environ.get(_PASSWORD_ENV) or _generate_password()


def get_or_create(site_key: str, email: str, password: str | None = None) -> dict:
    """Return {"email", "password"} for `site_key`, persisting `password` the
    first time this site is seen. `password=None` means default_password():
    $SITE_ACCOUNT_PASSWORD, or a fresh generated one. Sites already in the
    store keep the credentials they were created with."""
    data = _load()
    if site_key in data:
        return data[site_key]
    entry = {"email": email, "password": password or default_password()}
    data[site_key] = entry
    _save(data)
    return entry
