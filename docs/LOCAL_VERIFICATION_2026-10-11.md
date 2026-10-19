# Local V2 verification — 11 October 2026

## Verdict

**Not approved as a complete end-to-end candidate workflow.** Passing unit tests
and a working interactive terminal do not establish model-backed tailoring or
submission readiness. No real application was submitted during this verification.

## Repairs verified in this work session

- Terminal viewport bounded to avoid runaway resizing; keyboard input tested in
  the browser. Docker socket read timeouts no longer terminate an idle terminal.
- Stale WebSocket callbacks cannot overwrite a replacement connection. Intentional
  stop clears a connection-close error occurring before the stop response.
- Provider authentication/configuration failures point to model settings, not
  candidate evidence.
- Deterministic documents use French headings and letter greetings/closings when
  requested. Evidence selection ranks relevance instead of selecting only the
  first imported fact. Evidence is not invented or silently translated.
- Strategy refresh updates pending recommendations and history, and history
  errors are visible.
- Legacy discovery uses the running Python interpreter rather than assuming a
  host virtual environment exists inside Docker.
- Settings display rejected manual-run requests instead of failing silently.
  Removed the incorrect claim that local Claude Code execution guarantees no
  external transmission.

## Test evidence

- Backend full suite: 4,163 passed, 1 skipped; two dependency deprecation warnings.
- Frontend full suite after final settings repair: 626 passed across 67 files.
  Node reports an experimental localStorage warning.
- Frontend full suite after audit-trail localization: 629 passed across 68 files;
  Nuxt TypeScript checking and `git diff --check` passed again.
- Nuxt TypeScript checking: passed.
- Ruff on Python files changed in this repair session: passed.
- `git diff --check`: passed.
- Live API: generated and downloaded both resume and cover-letter PDFs using a
  synthetic account and synthetic evidence. Both were one-page, text-extractable
  PDFs. Rendered pages inspected for clipping and overlap.

Synthetic PDFs establish generation/download plumbing, not the quality of the
user's real tailored application. Their evidence remains English even with French
headings, because the deterministic fallback does not translate candidate facts.

## Remaining blockers

1. The configured terminal gateway returned HTTP 401 during status-only probes.
   The operator must replace the invalid/exposed key privately. Never include its
   value in this report, logs, commits, or chat.
2. The terminal configuration and the backend generation provider are separate.
   No enabled backend LLM connection was present in the database at inspection.
   Documents therefore used deterministic generation. A successful terminal
   session does not enable model-backed CVs, letters, interviews, or chat.
3. Settings manual runs and scheduling still use the legacy pipeline. The V2 map
   discovery and per-opportunity preparation are separate paths; the complete V2
   morning-run workflow has not been validated or fully wired from this page.
4. The browser worker was not rebuilt or accepted end-to-end in this session.
   The current automated tests must not be described as a real-site submission
   acceptance test.

## Follow-up correction: Swiss country boundary

During a browser run from a synthetic CH saved search, the catalog showed 50+
French postings from an international search index. Their source locations had
French city strings but no verified country code. Country-only catalog admission
now requires a matching verified country. The saved-search geo read also hides
historical unverified non-remote entries; explicit remote entries keep their
separate list-only treatment. Thirty targeted catalog and geo tests passed after
the correction. The full backend suite passed with 4,164 passing tests, one
skip and two existing dependency warnings. API and worker images were rebuilt
and restarted. Reloading the same saved search in the browser showed Swiss
locations such as Lausanne, Nyon and Genève instead of the previously observed
Orléans, Angers and Paris. This is one page of results, not a proof that every
country edge case is covered.

The synthetic account then selected a Swiss posting and used “Evaluate and
prepare”. The API returned a `REQUIRES_HUMAN` application; the browser showed a
review prompt. Both a resume and a cover letter appeared in the documents list.
The route was `MANUAL` and did not queue or send a real application. The live
applications and documents screens received French labels for known states and
document metadata. The application trail now translates the known manual-channel
reason while preserving unrecognized audit details unchanged. This is not
exhaustive localization: free-form backend diagnostics can still be in English.

The `MANUAL` outcome has a concrete cause: application creation selects the
`BROWSER` channel only when the stored opportunity has an `application_url`.
This posting has a source link, but that is not the same as a verified application
form URL. The isolated browser worker currently registers Greenhouse, Lever and
Ashby drivers, not jobup or arbitrary Swiss job-board forms. Treating a listing
URL as a supported submission form would falsely claim that an automated path
exists. A dedicated, safety-reviewed source-to-form path is still needed.

## Map follow-up

The Swiss geocoder processed a further bounded batch of 50 unresolved locations:
48 matched and were stored, one was ambiguous, and one was not found. The live
saved-search map displayed OpenStreetMap tiles and many grouped markers after
reloading. Selecting a located list item opened the corresponding posting card.
Some Swiss postings still have a place name but no coordinates; they remain in
the list without a marker. This confirms a working map for the checked viewport,
not complete geocoding of every posting or mobile acceptance.

## Scope and database

This repair session introduced no new migration. Existing uncommitted terminal,
contact and reference-CV migrations predate these repairs and are not evidence of
a fresh deployment test. The working tree contains substantial earlier changes;
this report does not certify every one of them.
