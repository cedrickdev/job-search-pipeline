---
name: morning-run
description: Execute the daily job-search morning run - discover, score, regenerate, tailor, auto-approve, export, digest. Use when asked to run the morning job search pipeline.
---

# Morning Run

You are the judgment layer of this job-search pipeline. The Python package does
everything deterministic; you supply scoring and tailoring.
**Never call any Anthropic API: you ARE the LLM.** Always use
`.venv/bin/python` from the project root (the folder containing
`pyproject.toml`) — every path below is relative to it.

Work through the steps in order. A step failing does not abort the run:
note it, continue, and make sure the failure appears in your final summary.

## Step 0: Load the profile

Never assume whose pipeline this is. Read the three files that define the
current profile before scoring anything:

```bash
.venv/bin/python -c "
import json, yaml
answers = yaml.safe_load(open('data/answers.yaml'))
searches = yaml.safe_load(open('config/searches.yaml'))
cv = yaml.safe_load(open('cv/base_cv.yaml'))
print(json.dumps({
    'personal': answers.get('personal'),
    'screening': answers.get('screening_answers'),
    'queries': searches.get('queries'),
    'locations': searches.get('locations'),
    'title_keywords': searches.get('title_keywords'),
    'exclude_keywords': searches.get('exclude_keywords'),
    'cv_title': cv['contact'].get('title'),
    'cv_summary': cv.get('summary'),
    'experience': [{'company': e['company'], 'title': e['title']} for e in cv['experience']],
    'skills': cv.get('skills'),
    'languages': cv.get('languages'),
}, indent=2, ensure_ascii=False))"
```

Everything you judge in Steps 3 and 5 comes from this output — the target
roles, the commutable area, the hours, the salary band, the real skills. If it
contradicts anything you remember about this project, the files win.

## Step 1: Backup

```bash
.venv/bin/python -m pipeline.backup
```

## Step 2: Discover

```bash
.venv/bin/python -m pipeline.discover
```

Read the JSON summary it prints. Note any source with `"ok": false` for the
digest. linkedin and indeed failing is common and acceptable; greenhouse,
lever, ashby, or wtj failing is worth flagging prominently.

## Step 3: Score

You ARE the LLM — score every job yourself.

> The `lmstudio` / `ollama` value of `llm_backend` only affects the webapp
> copilot (`server/chat.py`). There is no `pipeline.scoring_io score-with-llm`
> and no `pipeline.tailor_llm`: local-model scoring and tailoring are not
> implemented. Whatever the setting says, score and tailor manually here.

```bash
.venv/bin/python -m pipeline.scoring_io list
```

For each job in the JSON array: if `description` is null, fetch the `url`
with WebFetch and read the posting. Score 0-100 against the Step 0 profile —
never against a role you assume the user wants:

- **Role fit (40):** how close the title and duties are to the `queries` and
  `title_keywords` from `config/searches.yaml`, read at the seniority the CV
  supports. An exact match on a target title = full points; an adjacent role
  the profile could plausibly do = partial; a role that needs a diploma,
  licence, or years of experience the profile lacks = weak. Anything matching
  `exclude_keywords` = 0 for this component.
- **Skills overlap (30):** match against the skills, bullets and languages the
  base CV actually claims. The more the posting leans on what the profile
  already demonstrates, the higher. Requirements the profile does not have
  (a language, a certification, a licence) pull this down.
- **Scope fit (20):** commutable from the `locations` in `searches.yaml`, or
  remote; contract type, hours and pay compatible with the
  `screening_answers` in `data/answers.yaml` (availability, hours per week,
  salary expectation, work authorisation). A hard conflict — unreachable
  location, full-time when only part-time is possible, a permit the profile
  does not hold — is 0 for this component.
- **Red flags (10):** full points if none. Deduct for: vague descriptions,
  no named employer, pay well under the stated expectation, "rockstar/ninja"
  language, suspiciously broad roles, anything that reads like a scam.

State the profile-specific reason in `--reasoning`, not the rubric weights:
"explicitly a student job, 20-30%, in Yverdon" is useful next week; "role fit
32/40" is not.

Record each score (use --red-flags only when there are any):

```bash
.venv/bin/python -m pipeline.scoring_io record --job-id N --score 85 \
  --reasoning "One or two sentences in plain language." \
  --red-flags "optional short note"
```

The CLI applies thresholds itself: 70+ becomes Scored (tailor next),
50-69 Borderline, below 50 Archived.

## Step 4: Process regeneration requests

```bash
.venv/bin/python -m pipeline.regen --pending
```

For each pending request: re-tailor that job exactly as in Step 5, applying the request `notes` as additional guidance (write a fresh `data/tailored/<job_id>.yaml`, fresh cover letters, then run `tailor_io` with a `--diff` that mentions the notes). Honor the request's `creativity` level (`conservative` | `balanced` | `bold`, from the `--pending` JSON): `conservative` stays close to the base CV and tones down rephrasing; `balanced` is the normal Step 5 behavior; `bold` pushes harder for JD coverage — surface every defensibly-relevant keyword and lead with the strongest matches — while still obeying the §truth rules (never invent numbers, technologies, or experience). When tailor_io succeeds, resolve:

```bash
.venv/bin/python -m pipeline.regen --resolve REQUEST_ID
```

Mention every resolved regeneration in your final summary. If tailoring fails after 3 attempts, mark the request failed so the dashboard stops showing it as queued and surfaces the reason:

```bash
.venv/bin/python -m pipeline.regen --fail REQUEST_ID --reason "<short reason>"
```

Still report it under "needs attention".

## Step 5: Tailor every job now in status "Scored"

**Objective: maximum interview conversion rate.** The CV must pass ATS
keyword scan, recruiter 6-second review, and hiring manager review.
Every word earns its place by increasing probability of a callback.

You tailor manually — `pipeline.tailor_llm` does not exist, so the
`llm_backend` setting has no effect here either (see the note in Step 3).

Find jobs to tailor:

```bash
.venv/bin/python -c "
from pipeline import paths
from pipeline.db import connect, init_db
conn = connect(paths.DB_PATH); init_db(conn)
import json
rows = conn.execute(\"SELECT j.id, j.company, j.title, j.description, j.language FROM jobs j JOIN applications a ON a.job_id = j.id WHERE a.status = 'Scored'\").fetchall()
print(json.dumps([dict(r) for r in rows], indent=2))"
```

**For each job, follow this sequence:**

### 5a. Gap analysis

```bash
.venv/bin/python -m pipeline.gap_analysis --job-id N
```

This shows keyword coverage (GREEN ≥80%, YELLOW 60-79%, RED <60%) and
which JD keywords are missing from the base library. The vocabulary it knows
lives in `cv/keywords.yaml`; a term the JD uses that is missing from BOTH the
base CV and that file will not be reported, so read the JD yourself too.

- **GREEN/YELLOW**: proceed to tailoring.
- **RED**: flag in digest as "low coverage — review before applying". Tailor
  anyway — the best possible CV is better than no CV.
- If the same keyword comes back missing across several offers, say so in the
  final summary: that is a base-CV gap worth fixing once, not per job.

Read the JD carefully. Extract:
1. **Required hard requirements** (exact names as written: tools, systems,
   certifications, licences, spoken languages)
2. **Required skills** (must-have vs nice-to-have)
3. **Domain vocabulary** (words that recur: "encaissement", "mise en rayon",
   "service en salle", "polyvalent", etc.)
4. **Seniority and availability signals** (years of experience, autonomy,
   percentage of a full-time post, shifts, weekends)

### 5b. ATS-first tailoring

Read `cv/base_cv.yaml`. Write `data/tailored/<job_id>.yaml`. Hard rules:

- **Summary (2-3 lines)**: The first sentence must state the candidate's exact
  identity as the JD frames it, using the wording of `contact.title` and
  `summary` in the base CV as the source of truth. Use a number only if a base
  bullet contains it. Mirror the JD's key vocabulary — ATS scans for exact
  phrases.
- **Bullet selection is relevance-first, not fill-first.** Pick bullets
  that directly hit the JD's required skills. A CV with 8 targeted bullets
  beats a padded CV with 12 bullets where half are irrelevant. The fill
  floor is a soft guideline; relevance is the hard constraint.
- **Required JD keywords must appear verbatim in the CV — when the base
  library already supports them.** If a base bullet says "encaissement" and
  the JD says "caisse", use the JD's word in the rephrased bullet. If the
  base library never mentions the thing at all, leave it out: the truth gate
  will reject it and it would be a lie in an interview.
- **Bullets are rephrased in the JD's vocabulary.** Keep `id`, `priority`,
  `tags` exactly as in the base. Reword `en` and `fr` together. NEVER add
  a number, employer, tool, certification, or language the base bullet does
  not contain. Never attach a skill to an achievement the base text does not
  tie it to.
- **Skills section**: reorder groups and items so the skills matching the
  JD appear first. Items only removed, never added or renamed.
- Education, languages, contact: copy unchanged.

Select enough bullets to fill one page: the renderer trims by priority, so
include one bullet too many rather than one too few. With a small base library
that means nearly all of it, ordered by relevance to this offer.

### 5c. Humanization pass

After writing the YAML, reread every bullet and the summary. Rewrite each
for natural human voice before running the validator. This is non-negotiable.

**What to fix:**
- Vary opening verbs. If 3+ bullets start with "Built": change some to
  "Designed", "Ran", "Shipped", "Wrote", "Created", "Set up", "Produced".
- Vary sentence length. Some bullets should be short and punchy (12 words);
  some can be longer with context. Uniform length reads as AI-generated.
- Eliminate formulaic patterns: "X to achieve Y, resulting in Z" is a
  template, not a sentence.
- Replace nominalisations: "implementation of" → "built"; "utilisation of"
  → "using"; "optimisation of" → "optimised".
- No "end-to-end" more than once per CV.
- No "responsible for" — use an active verb.
- Numbers should feel embedded, not inserted: "cut query time from 8 to 2
  seconds" not "achieved a 75% reduction in query latency".
- The summary should read like the candidate wrote it for this specific
  employer, not like a template angled at any offer in the sector.

**Do not** change the meaning, invent facts, or remove required keywords
during humanization. The truth gate runs after this.

### 5d. Validate and render

```bash
.venv/bin/python -m pipeline.tailor_io --job-id N \
  --content "data/tailored/N.yaml" \
  --diff "selected X/Y bullets; rewrote ids: a, b, c; summary rewritten"
```

In `--diff`, X is the number of bullets selected, Y the library total.

If it exits non-zero:
- `numbers not in base` or `qualification not in base library`: the named
  bullet invented a fact. Remove it; every figure, tool, certification and
  language must come from that same base bullet. The claimable-qualification
  list is `cv/glossary.yaml` plus the `hard_skill_categories` of
  `cv/keywords.yaml`.
- `fill ... below the 0.92 floor`: the page is too empty. Add the most
  relevant unused base bullets (the message lists them). If no relevant
  ones remain, add the next most relevant — but never add clearly off-topic
  bullets just to pad.
- Style violations: fix the named pattern in the text.

Max 3 attempts. Still failing: leave as Scored and report under "needs
attention".

### 5e. ATS simulation check (mental)

After tailor_io succeeds, scan the tailored YAML mentally: does every
required JD keyword the base library supports appear in the CV? If the offer
listed "encaissement", "service en salle" and "allemand" as requirements — are
the first two in the skills or bullets, in the offer's own wording? If one is
missing and a base bullet could carry it, go back and add it. If the base
library genuinely does not have it (German, here), leave it out and note the
gap in the summary.

### 5f. Cover letters

Write to `data/cover_letters/<job_id>_en.md` and `<job_id>_fr.md`.
Three short paragraphs:
1. Why this company specifically (use something concrete from the JD or
   company description — not generic "I am passionate about").
2. What the candidate brings, with one concrete example from the tailored CV.
3. Close with availability (from `screening_answers`) and one forward-looking
   sentence.

Same style rules as the CV: no em-dashes, no banned phrases, glossary
terms in English in the French version.

## Step 6: Auto-approve (reads the setting itself)

```bash
.venv/bin/python -m pipeline.auto_approve
```

With `auto_apply` off in `data/settings.json` (the default) this prints `[]` and changes nothing. When it approves jobs it prints them as JSON and they appear in the digest's "Auto-approved today" section. Never approve anything yourself: this command is the only auto-approval path.

## Step 7: Export

```bash
.venv/bin/python -m pipeline.excel_export
```

## Step 8: Digest

```bash
.venv/bin/python -m pipeline.digest
```

Then check for jobs left in "Scored" (tailoring failed after retries); the
digest cannot show these, so you must list them yourself:

```bash
.venv/bin/python -c "
from pipeline import paths
from pipeline.db import connect, init_db
conn = connect(paths.DB_PATH); init_db(conn)
rows = conn.execute(\"SELECT j.id, j.company, j.title FROM jobs j JOIN applications a ON a.job_id = j.id WHERE a.status = 'Scored'\").fetchall()
print([dict(r) for r in rows])"
```

Print the digest content (the markdown after the printed path) as your
final message, appending anything that needs the user's attention: failed
sources, errors, and every job the query above returned.
