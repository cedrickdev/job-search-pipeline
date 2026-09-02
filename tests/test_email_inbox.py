"""pipeline/email_inbox.py against a fake IMAP connection (no real mailbox)."""
import email
from email.mime.text import MIMEText

from pipeline import email_inbox


class FakeConn:
    def __init__(self, messages):
        # messages: list of (subject, body) newest last, like a real mailbox
        self._raw = []
        for i, (subject, body) in enumerate(messages):
            msg = MIMEText(body)
            msg["Subject"] = subject
            msg["From"] = "no-reply@jobup.ch"
            self._raw.append(msg.as_bytes())
        self.logged_out = False

    def select(self, mailbox):
        pass

    def search(self, charset, criteria):
        ids = [str(i + 1).encode() for i in range(len(self._raw))]
        return "OK", [b" ".join(ids)]

    def fetch(self, msg_id, parts):
        idx = int(msg_id) - 1
        return "OK", [(b"1", self._raw[idx])]

    def logout(self):
        self.logged_out = True

    def login(self, user, password):
        pass


def test_find_latest_match_returns_none_when_no_messages(monkeypatch):
    monkeypatch.setattr(email_inbox, "_connect", lambda *a, **k: FakeConn([]))
    assert email_inbox.find_latest_match("jobup.ch", email_inbox.LINK_PATTERN) is None


def test_find_latest_match_finds_confirmation_link(monkeypatch):
    body = "Confirmez votre compte: https://www.jobup.ch/confirm/abc123 merci."
    monkeypatch.setattr(email_inbox, "_connect",
                        lambda *a, **k: FakeConn([("Confirmez votre inscription", body)]))
    link = email_inbox.find_latest_match("jobup.ch", email_inbox.LINK_PATTERN)
    assert link == "https://www.jobup.ch/confirm/abc123"


def test_find_latest_match_prefers_newest_email(monkeypatch):
    old_body = "https://www.jobup.ch/confirm/old"
    new_body = "https://www.jobup.ch/confirm/new"
    monkeypatch.setattr(email_inbox, "_connect", lambda *a, **k: FakeConn([
        ("Confirmez", old_body), ("Confirmez", new_body),
    ]))
    link = email_inbox.find_latest_match("jobup.ch", email_inbox.LINK_PATTERN)
    assert link == "https://www.jobup.ch/confirm/new"


def test_find_latest_match_filters_by_subject(monkeypatch):
    monkeypatch.setattr(email_inbox, "_connect", lambda *a, **k: FakeConn([
        ("Newsletter", "https://www.jobup.ch/promo"),
        ("Bienvenue", "https://www.jobup.ch/other-topic"),
    ]))
    link = email_inbox.find_latest_match("jobup.ch", email_inbox.LINK_PATTERN,
                                         subject_contains="bienvenue")
    assert link == "https://www.jobup.ch/other-topic"


def test_find_latest_match_extracts_security_code(monkeypatch):
    monkeypatch.setattr(email_inbox, "_connect", lambda *a, **k: FakeConn([
        ("Your verification code", "Your code is 482913. It expires in 10 minutes."),
    ]))
    code = email_inbox.find_latest_match("linkedin.com", email_inbox.CODE_PATTERN)
    assert code == "482913"


def test_wait_for_match_raises_after_timeout(monkeypatch):
    monkeypatch.setattr(email_inbox, "_connect", lambda *a, **k: FakeConn([]))
    try:
        email_inbox.wait_for_match("jobup.ch", email_inbox.LINK_PATTERN,
                                   timeout=0, poll_interval=0)
        assert False, "expected InboxError"
    except email_inbox.InboxError:
        pass


def test_connect_requires_env(monkeypatch):
    for key in ("INFOMANIAK_IMAP_HOST", "INFOMANIAK_IMAP_USER", "INFOMANIAK_IMAP_PASSWORD"):
        monkeypatch.delenv(key, raising=False)
    try:
        email_inbox._connect()
        assert False, "expected InboxError"
    except email_inbox.InboxError:
        pass


def test_connect_supports_multiple_providers(monkeypatch):
    for key in ("BLUEWIN_IMAP_HOST", "BLUEWIN_IMAP_USER", "BLUEWIN_IMAP_PASSWORD"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("BLUEWIN_IMAP_HOST", "imaps.bluewin.ch")
    monkeypatch.setenv("BLUEWIN_IMAP_USER", "someone@bluewin.ch")
    monkeypatch.setenv("BLUEWIN_IMAP_PASSWORD", "x")
    monkeypatch.setattr(email_inbox.imaplib, "IMAP4_SSL", lambda host, port: FakeConn([]))
    conn = email_inbox._connect("bluewin")
    assert isinstance(conn, FakeConn)
