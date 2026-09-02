"""Email application channel: send a full application (cover-letter body + CV
and letter PDFs attached) to an employer address, and archive a verifiable copy
in the sender's IMAP "Sent" folder so the applicant can confirm it went out.

Used for employers with no online ATS (small restaurants, shops) and for dev
roles that ask for applications by email. Sending over SMTP does NOT file the
message in the mailbox by itself, so we APPEND a copy explicitly — otherwise
"did I really apply?" has no answer in the mailbox.
"""
from __future__ import annotations

import imaplib
import smtplib
import time
from email.message import EmailMessage
from pathlib import Path

from dotenv import dotenv_values

from pipeline import paths

_SENT_FOLDERS = ['"Sent Items"', "Sent", '"Sent Mail"', "INBOX.Sent"]


def _cfg() -> dict:
    """Read .env robustly (a '&' in a password breaks shell-style loaders)."""
    return dotenv_values(paths.ROOT / ".env") if (paths.ROOT / ".env").exists() else dotenv_values(".env")


def build_message(*, sender_name: str, sender_email: str, to: str, subject: str,
                  body: str, attachments: list[tuple[str, str]]) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = f"{sender_name} <{sender_email}>"
    msg["To"] = to
    msg["Subject"] = subject
    msg["Reply-To"] = sender_email
    msg.set_content(body)
    for path, label in attachments:
        data = Path(path).read_bytes()
        msg.add_attachment(data, maintype="application", subtype="pdf", filename=label)
    return msg


def _archive_to_sent(cfg: dict, msg: EmailMessage) -> str | None:
    """APPEND a copy to the first Sent folder that exists. Returns folder or None."""
    host = cfg.get("BLUEWIN_IMAP_HOST"); user = cfg.get("BLUEWIN_IMAP_USER")
    pwd = cfg.get("BLUEWIN_IMAP_PASSWORD")
    if not (host and user and pwd):
        return None
    conn = imaplib.IMAP4_SSL(host, int(cfg.get("BLUEWIN_IMAP_PORT", "993")))
    try:
        conn.login(user, pwd)
        typ, folders = conn.list()
        available = b"\n".join(folders or []).decode(errors="ignore")
        for folder in _SENT_FOLDERS:
            name = folder.strip('"')
            if name in available:
                conn.append(folder, "\\Seen", imaplib.Time2Internaldate(time.time()),
                            msg.as_bytes())
                return name
        return None
    finally:
        conn.logout()


def send_application(*, to: str, subject: str, body: str,
                     cv_pdf: str, letter_pdf: str | None = None,
                     sender_name: str = "Cédrick Vanel Tchinda Feze",
                     provider: str = "bluewin", archive: bool = True) -> dict:
    """Send an application email and (by default) archive a verifiable copy in
    the Sent folder. Returns {'sent': bool, 'archived': str|None, 'to': str}."""
    cfg = _cfg()
    prefix = provider.upper()
    host = cfg[f"{prefix}_SMTP_HOST"]; port = int(cfg[f"{prefix}_SMTP_PORT"])
    user = cfg[f"{prefix}_IMAP_USER"]; pwd = cfg[f"{prefix}_IMAP_PASSWORD"]

    attachments = [(cv_pdf, "CV_Cedrick_Tchinda_Feze.pdf")]
    if letter_pdf:
        attachments.append((letter_pdf, "Lettre_motivation_Cedrick_Tchinda_Feze.pdf"))
    msg = build_message(sender_name=sender_name, sender_email=user, to=to,
                        subject=subject, body=body, attachments=attachments)

    with smtplib.SMTP_SSL(host, port, timeout=30) as s:
        s.login(user, pwd)
        s.send_message(msg)

    archived = _archive_to_sent(cfg, msg) if archive else None
    return {"sent": True, "archived": archived, "to": to}
