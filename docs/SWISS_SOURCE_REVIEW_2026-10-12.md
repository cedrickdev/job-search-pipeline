# Swiss discovery source access review — 12 October 2026

## Decision

No live multi-jobboard collection is approved by this review. The current CH
configuration enables adapters whose transport and access conditions have not
been reconciled. Do not run the V2 morning scheduler against these live sources.
Fixture-only development may continue; the multi-jobboard acceptance gate stays
open until two independent, authorized CH job-board/aggregator feeds have been
verified in a bounded non-submitting dry-run.

This is an engineering access review, not a legal opinion. Recheck current terms
and obtain provider confirmation before changing a source decision. A job listing
URL is not permission to automate its application form.

| Source key | Official terms / API reference | Access decision | Rate limit | Review date | Reason |
| --- | --- | --- | --- | --- | --- |
| `jobup` | [JobCloud terms](https://www.jobcloud.ch/c/en/terms/) | hold | Not established | 2026-10-12 | Existing adapter reads the page's undocumented `__INIT__` blob; JobCloud terms cover jobup and prohibit crawlers/scrapers and automated access except official tools. No authorization for this use was verified. |
| `jobscout24` | [JobCloud terms](https://www.jobcloud.ch/c/en/terms/) | hold | Not established | 2026-10-12 | Existing adapter parses listing HTML. The same terms explicitly name jobscout24; no authorization for automated access was verified. The adapter docstring incorrectly calls this board independent of JobCloud. |
| `indeed_ch` | [Indeed terms](https://ch.indeed.com/legal) | hold | Not established | 2026-10-12 | Existing adapter parses an undocumented `mosaic` page blob. Indeed terms restrict automated access and scraping without express written permission. No such permission was verified. |
| `jooble` | [Jooble API information](https://jooble.org/api/about) | needs permission | Not established | 2026-10-12 | Existing adapter uses the documented key-based API shape, but the official information page returned HTTP 403 during this review; account entitlement, Swiss scope, quota, attribution and current terms were not verified. A private API key is required. |

## Current implementation and reconciliation needed

- `country_packs/ch/sources.yaml` currently enables all four keys. Enabled in
  code is not equivalent to approved for live collection. Scheduled execution
  needs an operator-reviewed source allow-list or disabled bindings before it can
  be enabled in a real deployment.
- `pipeline/sources/jobscout24.py` says JobScout24 is independent of JobCloud.
  JobCloud's current terms name it as a JobCloud platform. Correct this stale
  statement when touching the adapter; it cannot justify treating its terms as
  separate.
- `jooble` is the most plausible authorized candidate after obtaining and
  reviewing an API account, but one feed is not the agreed multi-jobboard bar.
  Other official feeds or written partner access must be reviewed separately.
- No live source request or employer submission was made as part of this
  review. Only public terms/information pages were inspected.

The V2 API, CLI, scheduler and general worker now enforce a default-empty
`JOBSEARCH_DISCOVERY_ALLOWED_SOURCES` operator list. This prevents an enabled
country-pack binding from becoming live access on its own. The review decision
is unchanged: no source in this table is approved by this document.

## Employer ATS feed prospects — not yet approved

The candidate has accepted a narrower next milestone: one Swiss posting from a
reviewed Greenhouse employer feed and one from a reviewed Lever employer feed.
These are two ATS feed families, **not** two independent Swiss job boards. The
official [Greenhouse Job Board API](https://developers.greenhouse.io/job-board.html)
and [Lever Postings API](https://github.com/lever/postings-api) document public
listing reads. Neither documentation, on its own, establishes that this project's
collection frequency, retention, redistribution or automated form submissions are
permitted for every employer board. Application-form permission must be reviewed
separately. No live feed was fetched for this review.

| ATS | Public employer board prospect | Swiss evidence | Decision | Daily request budget | Remaining review |
| --- | --- | --- | --- | --- | --- |
| Greenhouse | [SpotMe](https://job-boards.greenhouse.io/spotme) (`spotme`) | Public board advertises Lausanne roles; location and posting availability must be rechecked at activation. | hold | Not set | Confirm applicable collection/storage terms, employer provenance and current CH result with a bounded, non-submitting check. |
| Lever | [ANYbotics](https://jobs.lever.co/anybotics) (`anybotics`) | Public board advertises Zurich roles; location and posting availability must be rechecked at activation. | hold | Not set | Confirm applicable collection/storage terms, employer provenance and current CH result with a bounded, non-submitting check. |

The V2 board catalog in `config/ats_reviewed_boards.yaml` stays empty while these
decisions are `hold`. A public board token is not a credential and is not an
approval. The source-key allow-list must remain empty for real accounts until a
durable per-board request budget and an explicit approved catalog entry are in
place. If either employer has no CH posting at activation, select a different
reviewed employer; do not manufacture a Swiss result.

## Acceptance gate

The superseded multi-jobboard criterion remains unverified. For the candidate's
approved narrower milestone, record one eligible Greenhouse employer board and
one eligible Lever employer board, each with applicable collection conditions,
allowed query volume, Swiss coverage, attribution and storage rules, a
timestamped bounded non-submitting dry-run with a valid CH posting, and
source-health/warning codes. Until both pass, the interface must say that live
ATS multi-source coverage is unverified, not that no Swiss jobs exist. Passing
this gate never establishes permission to submit an application form.
