"""Read-only IMAP access to the candidate's mailbox.

Used during autonomous account creation/login (e.g. scripts/jobup_signup.py)
to fetch a signup confirmation link or a login security code without the
user touching a browser. Read-only: this module only searches and reads: it
never deletes, flags, or modifies mailbox state.

Sending mail is a separate concern with a separate credential
(pipeline/gmail_recruiter.py, SMTP-only).
"""
from __future__ import annotations

import email
import imaplib
import os
import re
import time
from email.header import decode_header
from email.message import Message

from dotenv import load_dotenv

load_dotenv()

DEFAULT_TIMEOUT = 120
DEFAULT_POLL_INTERVAL = 5
LOOKBACK = 10  # newest N emails from the sender to scan, most recent first

LINK_PATTERN = r'https?://[^\s"\'<>]+'
CODE_PATTERN = r'\b(\d{6})\b'


class InboxError(Exception):
    """Mailbox unreachable, misconfigured, or nothing matched in time."""


DEFAULT_PROVIDER = "infomaniak"  # primary applicant mailbox for new accounts


def _connect(provider: str = DEFAULT_PROVIDER) -> imaplib.IMAP4_SSL:
    prefix = provider.upper()
    host = os.environ.get(f"{prefix}_IMAP_HOST")
    port = int(os.environ.get(f"{prefix}_IMAP_PORT", "993"))
    user = os.environ.get(f"{prefix}_IMAP_USER")
    password = os.environ.get(f"{prefix}_IMAP_PASSWORD")
    if not (host and user and password):
        raise InboxError(f"{prefix}_IMAP_* not set (see .env.example)")
    conn = imaplib.IMAP4_SSL(host, port)
    conn.login(user, password)
    return conn


def _decode(value: str | None) -> str:
    if not value:
        return ""
    return "".join(
        part.decode(enc or "utf-8", errors="ignore") if isinstance(part, bytes) else part
        for part, enc in decode_header(value)
    )


def _body_text(msg: Message) -> str:
    if not msg.is_multipart():
        payload = msg.get_payload(decode=True)
        return payload.decode(msg.get_content_charset() or "utf-8", errors="ignore") if payload else ""
    chunks = []
    for part in msg.walk():
        if part.get_content_type() in ("text/plain", "text/html"):
            payload = part.get_payload(decode=True)
            if payload:
                chunks.append(payload.decode(part.get_content_charset() or "utf-8", errors="ignore"))
    return "\n".join(chunks)


def _recent_bodies(conn: imaplib.IMAP4_SSL, from_contains: str, subject_contains: str | None,
                   mailbox: str) -> list[str]:
    """Bodies of the most recent matching emails, newest first."""
    conn.select(mailbox)
    _, data = conn.search(None, f'(FROM "{from_contains}")')
    ids = data[0].split()
    bodies = []
    for msg_id in reversed(ids[-LOOKBACK:]):
        _, msg_data = conn.fetch(msg_id, "(RFC822)")
        if not msg_data or not msg_data[0]:
            continue
        msg = email.message_from_bytes(msg_data[0][1])
        if subject_contains and subject_contains.lower() not in _decode(msg["Subject"]).lower():
            continue
        bodies.append(_body_text(msg))
    return bodies


def find_latest_match(from_contains: str, pattern: str, *, mailbox: str = "INBOX",
                      subject_contains: str | None = None,
                      provider: str = DEFAULT_PROVIDER) -> str | None:
    """First regex match in the newest matching email from `from_contains`,
    or None if no email or no match."""
    conn = _connect(provider)
    try:
        for body in _recent_bodies(conn, from_contains, subject_contains, mailbox):
            match = re.search(pattern, body)
            if match:
                return match.group(1) if match.groups() else match.group(0)
        return None
    finally:
        conn.logout()


def wait_for_match(from_contains: str, pattern: str, *, timeout: int = DEFAULT_TIMEOUT,
                   poll_interval: int = DEFAULT_POLL_INTERVAL, mailbox: str = "INBOX",
                   subject_contains: str | None = None,
                   provider: str = DEFAULT_PROVIDER) -> str:
    """Poll until a regex match appears (new confirmation email arriving after
    a signup/login attempt) or raise InboxError after `timeout` seconds."""
    deadline = time.monotonic() + timeout
    while True:
        found = find_latest_match(from_contains, pattern, mailbox=mailbox,
                                  subject_contains=subject_contains, provider=provider)
        if found:
            return found
        if time.monotonic() >= deadline:
            raise InboxError(f"No match for {pattern!r} from {from_contains!r} within {timeout}s")
        time.sleep(poll_interval)


def wait_for_link(from_contains: str, **kwargs) -> str:
    return wait_for_match(from_contains, LINK_PATTERN, **kwargs)


def wait_for_code(from_contains: str, **kwargs) -> str:
    return wait_for_match(from_contains, CODE_PATTERN, **kwargs)
