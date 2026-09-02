---
name: onboard
description: Onboard a new user to the job-search pipeline. Interviews the user conversationally to collect everything needed for agentical Playwright applying — personal info, CV, target roles, and settings. Writes data/answers.yaml, data/settings.json, config/searches.yaml and cv/base_cv.yaml.
---

# Onboarding

You are onboarding a new user to the job-search pipeline. Your goal is to
collect everything the pipeline needs to discover, score, tailor, and
agentically apply to jobs — without the user ever having to edit a YAML file
by hand.

**You ARE the interviewer.** Ask questions conversationally, one section at a
time. Confirm each section before moving on. At the end, show the user a
preview of all three output files and ask for a final confirmation before
writing anything.

Never call any Anthropic API. You ARE the LLM.
Always use `.venv/bin/python` from the project root.

---

## Before you start

Check whether output files already exist:

```bash
ls "data/answers.yaml" "data/settings.json" "config/searches.yaml" "cv/base_cv.yaml" 2>/dev/null
```

If any exist, tell the user: "I found existing config files. Onboarding will
overwrite them. Should I continue?" Wait for confirmation.

`cv/base_cv.yaml` matters most here: it is the CV library every tailored PDF is
built from, and the truth gate rejects anything it does not contain. Leaving a
previous user's file in place is the one mistake that silently breaks scoring
and tailoring for everything downstream, so it must be rewritten (Step 10b),
never inherited.

---

## Step 1: Personal information

Ask for (accept any order, extract from free-form text):

- Full name
- Email address
- Phone number (with country code)
- City, postal code, country
- Home address (street line) — explain it is used to pre-fill ATS forms
- Date of birth (DD/MM/YYYY)
- Place of birth (city, country)
- Current employer (or "unemployed" / "student")
- LinkedIn profile URL (optional — press Enter to skip)
- Portfolio or personal site URL (optional)

Confirm: "Here's what I have for personal info — looks right?" then show a
clean bullet list before continuing.

---

## Step 2: CV upload

Ask: "Please give me your CV. You can either:
  a) Type the path to your CV PDF (e.g. ~/Desktop/my_cv.pdf)
  b) Paste your CV text directly here"

**If they give a PDF path:** run:

```bash
.venv/bin/python -c "
import sys, pathlib
p = pathlib.Path(sys.argv[1]).expanduser()
print('exists' if p.exists() else 'not found')
" "<path>"
```

If the file exists, try to extract text:

```bash
.venv/bin/python -c "
try:
    import pypdf
    r = pypdf.PdfReader('<path>')
    print('\n'.join(p.extract_text() or '' for p in r.pages))
except ImportError:
    print('NO_PYPDF')
"
```

If `NO_PYPDF`, ask the user to paste the CV text instead.

**Read the CV carefully.** Everything you pull out here becomes
`cv/base_cv.yaml` in Step 10b, so keep the raw wording: company names, exact
titles, start/end months, and each achievement as its own line. Extract:

1. Work experience entries (company, title, start/end, key bullet points)
2. Education (degree, school, location, years)
3. Skills and technologies mentioned, grouped as the CV groups them
4. Languages spoken, with a level for each
5. **Domain** — classify as one of:
   - `data_science` (ML, DS, AI, statistics)
   - `finance` (investment, banking, trading, DCF, modeling, Bloomberg)
   - `engineering` (software, backend, frontend, infra, DevOps)
   - `marketing` (growth, CRM, brand, digital, SEO)
   - `consulting` (strategy, ops, management consulting)
   - `other` — describe what you see

Tell the user: "I've read your CV. I see you are in **[domain]** with
experience in [2-3 key areas]. I'll ask follow-up questions based on that."

---

## Step 3: Domain-specific follow-up

Ask follow-up questions that are relevant to the detected domain.
Do NOT ask generic tech questions for non-tech domains.

### data_science
- Years of experience total / in ML / in Python?
- Which frameworks do you use most: scikit-learn, PyTorch, TensorFlow, HuggingFace?
- Have you worked with LLMs, NLP, computer vision, time series?
- Have you used Databricks, Spark, MLflow, cloud platforms (AWS/GCP/Azure)?
- Experimentation experience (A/B testing, causal inference)?

### finance
- Years of experience in finance?
- Which area: investment banking, asset management, corporate finance, trading, VC/PE, fintech?
- Tools: Bloomberg, Refinitiv, FactSet, VBA, Excel modeling, Python for finance?
- CFA, ACCA, or other certifications?
- Have you built financial models (DCF, LBO, merger models)?

### engineering
- Primary language(s): Python, TypeScript, Go, Java, Rust, other?
- Backend / frontend / fullstack / infra / DevOps?
- Cloud: AWS, GCP, Azure? Kubernetes, Docker?
- Have you built APIs, data pipelines, distributed systems?

### marketing
- Digital or brand marketing?
- Tools: Google Analytics, HubSpot, Salesforce, Meta Ads, Google Ads?
- Have you run paid acquisition, SEO, CRM, or influencer campaigns?
- Experience with A/B testing or attribution?

### consulting
- Which type: strategy, ops, IT consulting, financial advisory?
- Have you worked at a Big 4, MBB, or boutique?
- Industry verticals: energy, financial services, retail, tech, public sector?

### other
Ask 3-5 open questions based on what you see in the CV.

---

## Step 4: Target roles and search preferences

Ask:

- What job titles are you targeting? (list as many as you want; draw the
  examples from the domain you detected in Step 2, not from another one)
- What level? Choose one or more: **internship / junior / confirmed / senior / lead**
- Which country/city are you looking in? Ask, never assume: there is no default,
  and a stale one silently sends discovery to the wrong country.
- Are you open to remote, hybrid, on-site, or all three?
- Do you want to exclude certain titles (e.g. exclude "intern", "freelance")?
  The pipeline already excludes internships and freelance by default — confirm
  if additional exclusions are needed.
- CV language: French, English, or both?

---

## Step 5: Work authorisation and salary

Ask:

- Are you authorized to work in [country from Step 1] without sponsorship?
  (yes / no)
- Do you have EU right to work? (yes / no)
- What is your target salary range? (min / max, currency, annual gross)
- What is your notice period? (e.g. "1 month", "immediately")

---

## Step 6: Pipeline settings

Tell the user: "Almost done — just a few pipeline settings."

Ask:

- Auto-apply: should the pipeline apply automatically to jobs above the score
  threshold, or should you approve each one manually?
  (default: manual approval — recommended for first-time users)
- If auto-apply yes: what minimum score (0-100) to auto-apply? (default: 80)
- Daily cap on automated applications (default: 5)
- What time should the daily pipeline run? (24h HH:MM, e.g. 08:00)
- Weekdays only, or every day? (default: daily)

Note: the LLM backend is always Codex CLI (no API key needed — Codex
IS the AI). No need to ask about this.

---

## Step 7: Generate motivations

Based on the CV and the target roles, write:

- 5 motivation variants in the user's primary language(s) — one per paragraph,
  varied in opening, all concrete (mention one actual achievement from the CV)
- If both EN and FR: write 5 in each language

Show them to the user. Ask: "Do these feel like you, or should I adjust the
tone / the achievements highlighted?"

Apply any feedback, then finalize.

---

## Step 8: Generate screening answers

From everything collected, auto-fill the screening_answers section:

- "years of experience": use total years
- "do you have experience with [primary skill]": "Yes"
- "do you speak french": Yes/No based on languages
- "do you speak english": Yes/No based on languages
- "are you authorized to work": Yes/No
- "requires sponsorship" → "visa" answer
- "salary expectation": "MIN-MAX CURRENCY gross annual"
- "notice period": value from Step 5
- "when can you start": derived from notice period
- "remote": derived from remote preference

---

## Step 9: Preview and confirm

Show the full content of all four files that will be written:

1. `data/answers.yaml` — full personal info, motivations, screening answers
2. `data/settings.json` — auto-apply, score threshold, cap, schedule
3. `config/searches.yaml` — queries, locations, title_keywords, exclude_keywords
4. `cv/base_cv.yaml` — the bilingual CV library (Step 10b)

For the CV library, show the bullets in full and say plainly: "Every tailored
CV can only ever use these facts. If an achievement is missing here, it can
never appear in an application." Let the user add or correct bullets now.

Ask: "Ready to write these files? (yes / no)"

If no: ask what to change and go back to the relevant step.

---

## Step 10: Write files

Only write after explicit confirmation.

Write `data/answers.yaml`:

```bash
.venv/bin/python -c "
import yaml, pathlib
data = <answers_dict>
pathlib.Path('data/answers.yaml').write_text(
    yaml.dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False)
)
print('written')
"
```

Write `data/settings.json`:

```bash
.venv/bin/python -m pipeline.settings --show
```

Then save with:

```bash
.venv/bin/python -c "
import json, pathlib
settings = <settings_dict>
pathlib.Path('data/settings.json').write_text(json.dumps(settings, indent=2) + '\n')
print('written')
"
```

Write `config/searches.yaml`:

```bash
.venv/bin/python -c "
import yaml, pathlib
searches = <searches_dict>
pathlib.Path('config/searches.yaml').write_text(
    yaml.dump(searches, allow_unicode=True, sort_keys=False)
)
print('written')
"
```

---

## Step 10b: Write the CV library — do not skip this

`cv/base_cv.yaml` is the single source of truth for every tailored CV. The
truth gate in `pipeline.tailor_io` refuses any bullet, number, tool,
certification or language that is not already in this file, and
`pipeline.gap_analysis` measures JD coverage against it. Skip this step and the
pipeline keeps applying with the previous occupant's CV.

Build it from the Step 2 extraction, following `cv/base_cv.template.yaml`:

- `contact`: name, `title: {en, fr}`, phone, email, `location: {en, fr}` — from
  Step 1, not from the PDF header.
- `summary`: 2-3 sentences, in both `en` and `fr`.
- `experience`: one entry per job, newest first, each with a slug `id`,
  `company`, `title: {en, fr}`, `start: "YYYY-MM"`, `end: "YYYY-MM"` or
  `present`, and `bullets`.
- Each bullet needs a unique slug `id`, `priority` 1-3 (1 = always keep,
  3 = first to drop), `tags`, and both `en` and `fr`. Write the two languages
  as translations of each other: the glossary gate checks that the terms in
  `cv/glossary.yaml` stay in English on both sides.
- Aim for **12 to 25 bullets total** — a superset of one page, because tailoring
  selects a relevant subset per offer. Under 8 fails `tests/test_base_library.py`
  and starves the fill floor.
- `skills`, `education`, `languages`: bilingual, exactly as the template shows.

Never invent a number, employer, tool or certification the user did not state.
An empty CV that is true beats a rich one that collapses in an interview.

Write it:

```bash
.venv/bin/python -c "
import yaml, pathlib
cv = <base_cv_dict>
pathlib.Path('cv/base_cv.yaml').write_text(
    yaml.dump(cv, allow_unicode=True, sort_keys=False), encoding='utf-8')
print('written')
"
```

Then extend `cv/keywords.yaml` for this user's field: it holds the job-posting
vocabulary `gap_analysis` scans for, per category. If the target roles are in a
domain its categories do not cover, add a category with the 10-20 terms real
postings in that field use. Add the user's genuine hard skills, certifications
and spoken languages to the categories listed under `hard_skill_categories`:
that list is what `truth_violations` will let a tailored bullet claim.

Leave `cv/glossary.yaml` (terms kept in English inside French text) and
`cv/style_rules.yaml` (banned AI phrasing) alone unless the user asks.

---

## Step 11: Setup verification

Run:

```bash
.venv/bin/python -m pipeline.settings --show
```

Confirm settings loaded with status `ok`.

Then prove the CV library actually works, before the first real application
depends on it:

```bash
.venv/bin/python -m pipeline.cv_render
.venv/bin/python -m pytest tests/test_base_library.py -q
```

`cv_render` prints one line per language with `pages=` and `fill=`; both must
render at `pages=1`, and a `fill` near 1.0 means the page is full. The test
checks bullet ids are unique, priorities are 1-3, both languages are present,
and the style and glossary gates pass. Fix the file and rerun until both are
clean: everything downstream, from gap analysis to the truth gate, reads this
file.

Then tell the user:

"Onboarding complete. Here's what to do next:

1. **Start the app**: run `./start.sh` (Mac/Linux) or `start.bat` (Windows)
   then open http://127.0.0.1:8765
2. **Run the pipeline**: in the Codex terminal, type `/morning-run`
   to discover and score jobs for the first time
3. **Review CVs**: jobs above your score threshold will have tailored CVs
   generated — approve them in the webapp before the applier fires
4. **Browser login**: the first time the applier runs for LinkedIn or
   Welcome to the Jungle, it will pause and ask you to log in manually —
   that session is then saved for future runs"
