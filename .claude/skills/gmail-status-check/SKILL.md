---
name: gmail-status-check
description: Search Gmail for status-relevant emails about jobs already in the applications tracker, classify each by judgment, and update the DB on confident matches. Use when asked to check Gmail for application status updates, or to run the Gmail status-check.
---

# Gmail Status-Check

You are the classification layer. The Python module handles IMAP access and
DB writes; you read each candidate email and decide what it means. Always use
`.venv/bin/python` from the project root (the folder containing
`pyproject.toml`).

> **Not implemented yet.** `pipeline/gmail_status_check.py` does not exist in
> this repo, so every command below fails with
> `No module named pipeline.gmail_status_check`. The IMAP plumbing that does
> exist is `pipeline/email_inbox.py` (login codes only) and
> `pipeline/gmail_recruiter.py` (sends follow-ups). Before running this skill,
> check the module is there; if it is not, tell the user it needs building
> instead of retrying the commands, and update statuses by hand through the
> webapp or `pipeline.statuses`.

**First-ever run only:** before anything else, run
`.venv/bin/python -m pipeline.gmail_status_check --selftest` and show the
user the 5 subjects it returns. Do not proceed to `--fetch`/`--apply` until
the user has seen this succeed once. Skip this step on every subsequent run.

## Step 1: Fetch candidates

```bash
.venv/bin/python -m pipeline.gmail_status_check --fetch
```

Returns JSON: `{query, date_from, candidates: [...], tracked_jobs: [...]}`.
If `candidates` and `tracked_jobs` are both empty, report "nothing to check"
and stop.

If a candidate's snippet doesn't give you enough to judge sender identity or
intent, fetch its full body before deciding:

```bash
.venv/bin/python -m pipeline.gmail_status_check --body --uid <uid>
```

## Step 2: Classify each candidate

For each candidate email, using `tracked_jobs` (company, title, current
status) as your reference set, decide:

- **Confident match** — the sending company/role clearly maps to exactly one
  tracked job, and the status signal is unambiguous: rejection, interview
  invite, phone screen invite, offer, or recruiter reply. Standard
  French rejection phrasing that opens with something like "malgré la
  qualité de votre profil" is still a confident `Rejected` — that pattern is
  well understood, not a source of real ambiguity.
- **Uncertain** — sender identity is unclear, the email could plausibly map
  to more than one tracked job, or the status signal is genuinely ambiguous.
  No DB write; list it in the report instead.
- **Untracked** — the company/role doesn't match any `tracked_jobs` entry at
  all. No DB write; list it in the report instead.

If more than one candidate maps to the same job, sort them chronologically
and process each one's implied transition in order (oldest first) rather
than jumping straight to the most recent signal — this lets intermediate
lifecycle timestamps (`phone_screen_at`, `interview_at`, etc.) get stamped
correctly.

Never decide `Ghosted` from an email — Ghosted comes from the absence of a
reply, not from any email content, and is out of scope for this skill.

## Step 3: Write confirmed changes

For each confident, actually-changed match:

```bash
.venv/bin/python -m pipeline.gmail_status_check --apply --job-id N \
  --status "Rejected" --detail "Email subject: ..."
```

`--status` must be one of the values in `pipeline.statuses.STATUSES`. The
command itself refuses (prints a "skipping" message, makes no write) if the
job is already at that status, or if applying it would regress the job's
lifecycle stage (e.g. `Interview scheduled` → `Recruiter reply` is refused;
`Interview scheduled` → `Rejected` is always allowed). Trust these guards —
if the command reports a skip, don't retry or override it.

## Step 4: Refresh exports

After all applies for the run (skip entirely if there were none):

```bash
.venv/bin/python -m pipeline.excel_export
.venv/bin/python -m pipeline.digest
```

## Step 5: Report

One chat summary with three parts:

1. **Status changes made** — table of job, old status → new status, and the
   source email's subject/snippet.
2. **Uncertain matches** — sender, subject, snippet, and why it's uncertain.
3. **Untracked-company emails** — sender, subject, snippet.
