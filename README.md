# Job Search Pipeline

> An AI-assisted job discovery, matching, application, and career automation platform built to reduce the repetitive work of finding and pursuing relevant opportunities.

[![Python](https://img.shields.io/badge/Python-3.12%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-Backend-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Playwright](https://img.shields.io/badge/Playwright-Automation-2EAD33?logo=playwright&logoColor=white)](https://playwright.dev/)
[![TypeScript](https://img.shields.io/badge/TypeScript-Frontend-3178C6?logo=typescript&logoColor=white)](https://www.typescriptlang.org/)
[![Status](https://img.shields.io/badge/status-active%20development-orange)](#project-status)

## Overview

Job Search Pipeline is an evolving career automation system designed to help candidates discover relevant opportunities, evaluate compatibility, prepare tailored application materials, track applications, and automate supported parts of the application workflow.

The current version already includes job discovery, scoring, tailored CV generation, application tracking, browser-assisted application flows, interview preparation, a web dashboard, and an AI copilot.

The V2 architecture is being developed as a broader multi-user career platform with geographic opportunity exploration, multi-country support, eligibility-aware matching, provider-neutral LLM integration, autonomous workflows, and a Nuxt-based frontend.

## Why this project exists

Searching for work is fragmented across job boards, company career pages, ATS platforms, email, CV versions, interview notes, and follow-up reminders.

Job Search Pipeline aims to turn that fragmented process into one coordinated workflow:

```text
Discover opportunities
        ↓
Normalize and deduplicate
        ↓
Evaluate fit and eligibility
        ↓
Generate tailored application material
        ↓
Review or auto-apply through supported channels
        ↓
Track outcomes
        ↓
Prepare interviews and follow-ups
        ↓
Learn from results
```

## Current capabilities

### Opportunity discovery

- Aggregates opportunities from multiple ATS providers and job sources.
- Normalizes listings into a consistent internal representation.
- Deduplicates repeated opportunities.
- Tracks source health and failures without stopping the entire discovery run.

### Candidate matching

- Scores discovered opportunities against the candidate profile.
- Stores reasoning and red flags alongside scores.
- Supports automated filtering and approval workflows.

### ATS-oriented CV tailoring

- Generates opportunity-specific CV versions.
- Supports French and English output.
- Preserves candidate truth through deterministic validation rules.
- Prevents unsupported technologies, numbers, employers, dates, and qualifications from being introduced.
- Produces versioned PDF artifacts.

### Application automation

- Includes adapters for multiple ATS/application flows.
- Uses Playwright for supported browser workflows.
- Supports email-based applications where configured.
- Tracks submission status and application history.

### AI copilot

- Provides job-aware and global conversational context.
- Supports structured action proposals rather than unrestricted state mutation.
- Currently supports Claude-oriented workflows and OpenAI-compatible local endpoints such as Ollama and LM Studio.

### Interview preparation

- Generates likely interview questions.
- Produces company research and talking points.
- Stores interview preparation notes and history.

### Web dashboard

The current application includes views for:

- overview;
- jobs;
- analytics;
- settings;
- job details;
- scoring;
- application actions;
- follow-ups;
- interview preparation;
- AI copilot interactions.

## Architecture

### Current V1

```text
Web application
      │
      ▼
   FastAPI
      │
      ├── Discovery engine
      ├── Matching / scoring
      ├── CV generation
      ├── Application adapters
      ├── Interview preparation
      ├── AI copilot
      └── Scheduler
      │
      ▼
    SQLite

Browser workflows → Playwright
LLM workflows     → Claude / OpenAI-compatible endpoints
```

### V2 direction

V2 is being implemented incrementally rather than through a full rewrite.

```text
Nuxt 4 + Vue 3 + TypeScript
            │
            ▼
         FastAPI
            │
   ┌────────┼─────────┐
   ▼        ▼         ▼
PostgreSQL Redis   Workers
+ PostGIS             │
                      ├── Discovery
                      ├── Matching
                      ├── CV / documents
                      ├── LLM workflows
                      └── Application automation

LLM Router
├── Claude Code adapter
├── Codex adapter
├── OpenAI-compatible API
├── Ollama / LM Studio
└── additional providers

Browser Worker
└── Playwright
```

The migration follows a compatibility-first approach: existing working modules are preserved until their V2 replacements reach functional parity and pass tests.

## Target V2 capabilities

The V2 roadmap includes:

- multi-user authentication and data isolation;
- `Opportunity` as the universal domain model instead of job-only semantics;
- student jobs, internships, apprenticeships, work-study, graduate and temporary opportunities;
- Country Packs for country-specific terminology, sources and rules;
- company and career-page discovery;
- PostgreSQL + PostGIS;
- interactive geographic opportunity explorer;
- radius and country-wide search;
- multidimensional compatibility and eligibility scoring;
- canonical Candidate Career Twin with evidence-backed claims;
- ATS resume and cover-letter generation;
- autonomous and approval-based application policies;
- provider-neutral LLM routing;
- Claude Code, Codex and custom `base_url + api_key + model` gateways;
- adaptive interview simulation;
- application outcome analytics;
- career intelligence and strategy recommendations;
- Docker-based local and production infrastructure.

## Technology stack

### Current application

| Area | Technology |
| --- | --- |
| Backend | Python 3.12+, FastAPI |
| Automation | Playwright |
| Database | SQLite |
| Frontend | React, TypeScript, Vite |
| Data / configuration | YAML, JSON |
| Testing | pytest, Vitest, Playwright |
| AI | Claude workflows, OpenAI-compatible endpoints |

### V2 target

| Area | Technology |
| --- | --- |
| Backend | Python 3.12+, FastAPI, Pydantic v2 |
| ORM / migrations | SQLAlchemy 2, Alembic |
| Database | PostgreSQL + PostGIS |
| Frontend | Nuxt 4, Vue 3, TypeScript |
| UI | Tailwind CSS, Nuxt UI |
| State | Pinia |
| Maps | MapLibre GL JS |
| Background execution | Redis + worker abstraction |
| Browser automation | Playwright |
| Containers | Docker, Docker Compose |
| LLM layer | Provider-neutral adapters |
| Quality | pytest, Ruff, mypy, Vitest, Playwright |

## Repository structure

```text
.
├── .agents/           # Agent skills and workflows
├── .claude/           # Claude Code skills
├── config/            # Search and application configuration
├── cv/                # CV templates, styles and keyword configuration
├── dashboard/         # Dashboard/query logic
├── pipeline/          # Discovery, scoring, tailoring and application engine
├── scripts/           # Utility and maintenance scripts
├── server/            # FastAPI application and routes
├── tests/             # Python test suite
├── webapp/            # Current React + TypeScript frontend
├── INSTALL.md         # Detailed installation guide
├── START.md           # Startup instructions
└── pyproject.toml      # Python package configuration
```

As V2 progresses, the frontend will migrate to Nuxt 4 and the backend will gradually adopt clearer domain and infrastructure boundaries.

## Getting started

### Prerequisites

- Python 3.12+
- Node.js 18+
- Git
- Playwright browser dependencies
- Claude Code for the current onboarding/agent workflow

### 1. Clone the repository

```bash
git clone https://github.com/cedrickdev/job-search-pipeline.git
cd job-search-pipeline
```

### 2. Create the Python environment

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e .
playwright install chromium
```

On Windows, activate the environment with:

```powershell
.venv\Scripts\Activate.ps1
```

### 3. Install frontend dependencies

```bash
cd webapp
npm install
cd ..
```

### 4. Configure the project

Copy the environment example and fill in the values required by your setup:

```bash
cp .env.example .env
```

For the current Claude Code onboarding workflow, open the project with Claude Code and run:

```text
/onboard
```

### 5. Start the application

macOS / Linux:

```bash
./start.sh
```

Windows PowerShell:

```powershell
.\start.ps1
```

Then open:

```text
http://127.0.0.1:8765
```

For the complete installation procedure and troubleshooting notes, see [INSTALL.md](./INSTALL.md).

## Running tests

Backend:

```bash
pytest
```

Frontend:

```bash
cd webapp
npm test
```

End-to-end tests:

```bash
cd webapp
npm run e2e
```

> Some commands may evolve as the V2 migration introduces Ruff, strict typing, Docker, PostgreSQL/PostGIS, and Nuxt.

## Documentation

The V2 engineering documentation defines the target architecture and gated implementation process.

Key documents include:

- `docs/V2_SPECIFICATION.md`
- `docs/ARCHITECTURE.md`
- `docs/FRONTEND_ARCHITECTURE.md`
- `docs/LLM_PROVIDER_ARCHITECTURE.md`
- `docs/ENGINEERING_STANDARDS.md`
- `docs/IMPLEMENTATION_PLAN.md`
- `CLAUDE.md`
- `AGENTS.md`

The implementation plan is intentionally phase-gated: each phase must be completed, tested, reviewed, and explicitly approved before the next one begins.

## LLM provider philosophy

The V2 application must not depend on a single model vendor.

The target provider layer supports multiple execution modes:

```text
LLM Provider Layer
├── Claude Code
├── Codex
├── OpenAI-compatible API
│   ├── base_url
│   ├── api_key
│   └── model
├── Ollama / LM Studio
└── future adapters
```

Provider-specific CLI flags, SDK calls, and session details remain isolated inside adapters. Domain and business logic communicate only through provider-neutral interfaces and typed outputs.

## Safety and automation principles

This project automates repetitive career-search workflows, but automation must remain controlled and auditable.

The system is designed to:

- preserve candidate truth;
- respect explicit user application policies;
- prevent duplicate submissions;
- record application decisions and artifacts;
- require human intervention for unsupported or sensitive flows;
- avoid CAPTCHA/MFA bypass and intentional anti-bot evasion;
- keep irreversible actions behind validated application services.

## Roadmap

Development is currently organized into gated phases, including:

1. baseline, quality gates, and CI;
2. V2 domain foundation;
3. PostgreSQL/PostGIS and Docker foundation;
4. React/Vite to Nuxt 4 migration;
5. authentication and onboarding;
6. Country Packs and source plugins;
7. company discovery;
8. geographic opportunity search;
9. advanced matching and eligibility;
10. ATS resume and cover-letter V2;
11. provider-neutral LLM platform;
12. autonomous application engine;
13. AI career control plane;
14. adaptive interview simulator;
15. outcome tracking and career intelligence;
16. SaaS hardening and country expansion.

See `docs/IMPLEMENTATION_PLAN.md` for the detailed implementation gates and acceptance criteria.

## Project status

**Active development / architectural migration.**

The repository contains a working V1 personal job-search automation system while the V2 architecture is being introduced incrementally.

Interfaces, schemas, and internal module boundaries may change while V2 is under development.

## Contributing

The project is currently evolving rapidly. Before contributing:

1. read the architecture and engineering documentation;
2. keep changes scoped to the currently approved implementation phase;
3. preserve backward compatibility unless a migration explicitly permits breaking changes;
4. add or update tests;
5. never commit secrets or personal candidate data.

For AI coding agents, repository-specific instructions are defined in `CLAUDE.md`, `AGENTS.md`, and `.claude/skills/`.

## License

No open-source license has been declared yet. Until a license is added, all rights remain reserved by the repository owner.

---

Built as an experiment in combining software engineering, browser automation, geospatial search, and provider-neutral AI into a unified career platform.
