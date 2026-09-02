---
name: agentic-apply
description: Codex personally drives Playwright to submit job applications (not the automated Python/VLM worker) and confirms each one via Gmail before marking it applied. Opt-in only - invoke when the user explicitly asks Codex to apply itself (e.g. "use the agentic apply skill", "apply yourself to these jobs"). Never runs automatically as part of a normal morning run.
---

# Agentic Apply

You personally fill out and submit job applications using the Playwright MCP
browser tools, one job at a time, then confirm each submission by checking
Gmail before marking it done. This replaces the automated Python/VLM applier
(`pipeline.apply_one` / `pipeline.auto_approve`) for this run only — do not
invoke those alongside this skill, and do not use this skill unless it was
explicitly requested.

Always use `.venv/bin/python` from the project root (the folder containing
`pyproject.toml`) for any Python invocation.

> **Not implemented yet.** `pipeline/apply_claude.py` does not exist in this
> repo, so steps 1 and 7 below fail with
> `No module named pipeline.apply_claude`. `pipeline/apply_requests.py` has the
> claim/resolve half but does not stage the CV and cover letter. Check the
> module exists before starting; if it does not, say so instead of retrying,
> and either build it first or drive the run manually from
> `data/cv_versions/` plus `data/cover_letters/`.

## Before you start

1. Load `data/settings.json` for `auto_apply_min_score` and
   `auto_apply_daily_cap`.
2. Count today's already-`applied` requests (via `apply_requests`/events) so
   you know how much of the daily cap is left. If it's already at/over cap,
   stop and tell the user.
3. Build the candidate list: tailored jobs with `score >= auto_apply_min_score`,
   descending by score, excluding any job that already has a non-terminal or
   terminal `apply_requests` row. Also exclude jobs already handled by the
   older pipeline tracking: `applications.submitted_at IS NOT NULL` (already
   submitted) or `applications.status IN ('Rejected', 'Ghosted', 'Applied',
   'Phone Screen', 'Interview', 'Offer')` — these predate the `apply_requests`
   table and won't show up there, but re-applying to them would be a duplicate
   application to a job already decided.

If there are no candidates, say so and stop — do not lower the bar or widen
the criteria on your own judgment.

## The loop — strictly one job at a time

Do not start job N+1 until job N has reached a terminal status. For each
candidate, in order:

### 1. Start

```
.venv/bin/python -c "
from pipeline.db import connect, init_db
from pipeline import apply_claude, paths
conn = connect(paths.DB_PATH); init_db(conn)
print(apply_claude.start(conn, JOB_ID))
"
```

This claims the `apply_requests` row (`channel='claude_agentic'`), stages
the CV and cover letter under human-readable filenames
(`<contact.name from cv/base_cv.yaml> - <Role>.pdf` convention), and loads
`data/answers.yaml`. Keep the `request_id`, `cv_path`, `cover_letter_path`,
and `answers` it returns — you'll need them for the rest of this job.

If `start` raises (in-flight request already exists, or no CV staged for
this job), skip the job and move to the next candidate.

### 2. Navigate and read the page

`browser_navigate` to the job's URL. Take a `browser_snapshot` to read the
page structure before touching anything. Identify the ATS from the URL/page
(LinkedIn, Workday, Greenhouse, Lever, Ashby, WTJ, Talentsoft, Oracle ORC, or
generic company site). If login is required, use the saved account from
memory for that platform; LinkedIn and Gmail are already signed into this
browser session.

Company-specific quirks worth remembering as you go (from memory):
Workday's submit button is often overlaid by a `click_filter` div — click
that div, not the button underneath it; Talentsoft's cookie banner can wipe
programmatically-set input values, so dismiss it before filling anything;
Oracle ORC's date-of-birth fields are comboboxes that must be opened and
clicked from the listbox, not value-set directly.

### 3. Fill the form yourself

Work field by field from the snapshot:
- Personal info and known screening answers → `answers.yaml`.
- CV / cover letter → `browser_file_upload` with the staged paths from step 1.
- Free-text screening questions not covered by `answers.yaml` → answer from
  judgment against the job description and `cv/base_cv.yaml` content. Never
  invent a fact, number, technology, or piece of experience that isn't
  already there.
- If a question asks for something genuinely unknowable (salary expectation
  not in `answers.yaml`, a reference's contact details, an undecided
  availability date, anything sensitive/legal) — stop this job and resolve
  it `needs_you` (step 6). Do not guess.

### 4. Screenshot before submitting

`browser_take_screenshot`, save to
`data/screenshots/{job_id}_claude_pre_submit.png`. This is your last chance
to catch a mistake before it's irreversible.

### 5. Submit and screenshot the result

Click submit. Take another screenshot:
`data/screenshots/{job_id}_claude_done.png` on an apparent success screen,
or `data/screenshots/{job_id}_claude_error.png` if something went wrong.

### 6. Confirm via Gmail (only reached on apparent success)

Search the Gmail inbox (already signed in as the address in
`data/answers.yaml` → `personal.email`) for a new message from the
company/ATS domain sent after you clicked submit.

- Found immediately → confirmed.
- Not found → wait about 2 minutes, check again. Up to 3 checks total.

### 7. Resolve

```
.venv/bin/python -c "
from pipeline.db import connect, init_db
from pipeline import apply_claude, paths
conn = connect(paths.DB_PATH); init_db(conn)
apply_claude.finish(conn, REQUEST_ID, STATUS, DETAIL, SCREENSHOT_PATH)
"
```

Pick `STATUS`:
- **`applied`** — success screen + confirmation email found. Detail includes
  the email's subject/snippet.
- **`applied`** — success screen, no email after 3 checks. Detail notes "no
  confirmation email found after 3 checks".
- **`needs_you`** — blocked: CAPTCHA, unexpected 2FA, an unanswerable or
  sensitive question, login failure. Detail explains exactly where and why.
- **`failed`** — unrecoverable error (crash, exception). Detail has the
  error.

Only after this call do you move to the next candidate job.

## Guardrails

- **Consecutive-failure brake**: if 2 jobs in a row resolve to
  `needs_you`/`failed`, stop the run early. Tell the user which jobs and why,
  and leave the rest of today's cap untouched rather than continuing to burn
  through it on what might be a systemic issue.
- **No parallelism**: never have two jobs in flight at once.
- **Don't touch the automated worker**: do not invoke `pipeline.apply_one`
  or `pipeline.auto_approve` during this run — you are the only applier.
- **First-ever use**: if this is the first time this skill is being run,
  do a single-job dry run first — narrate each step, show the pre-submit
  screenshot before clicking, and confirm with the user that it went cleanly
  (including the Gmail check) before continuing automatically through the
  rest of the candidates.

## End of run

Report: how many jobs reached `applied`, how many `needs_you` (with reasons,
so the user can finish them manually), how many `failed`, and how much of
today's cap remains.
