# Installation

## Prerequisites

Install these once on your machine before anything else.

| Tool | Version | Download |
|------|---------|----------|
| Python | 3.12 or later | https://python.org/downloads |
| Node.js | 18 or later | https://nodejs.org |
| Claude Code | latest | https://claude.ai/code |
| Git | any | https://git-scm.com |

> **Windows note:** during Python install, tick "Add Python to PATH".
> During Node install, accept the default options.

---

## 1. Get the project

Copy or clone the project folder to your machine. The folder can be anywhere
— the scripts use relative paths.

---

## 2. Create the Python environment

**Mac / Linux**

```bash
cd "/path/to/job search"
python3.12 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/playwright install chromium
```

**Windows (PowerShell)**

```powershell
cd "C:\path\to\job search"
python -m venv .venv
.venv\Scripts\pip install -e .
.venv\Scripts\playwright install chromium
```

**Windows (Command Prompt)**

```cmd
cd "C:\path\to\job search"
python -m venv .venv
.venv\Scripts\pip install -e .
.venv\Scripts\playwright install chromium
```

---

## 3. Install frontend dependencies

```bash
# Mac / Linux
cd frontend && npm ci && cd ..
```

```powershell
# Windows
cd frontend; npm ci; cd ..
```

---

## 4. Run onboarding

Open the project folder in Claude Code:

```bash
claude "/path/to/job search"
```

Then type:

```
/onboard
```

Claude will interview you to collect your personal information, read your CV,
ask domain-relevant questions, and write the three config files the pipeline
needs (`data/answers.yaml`, `data/settings.json`, `config/searches.yaml`).

This takes about 10 minutes and only needs to be done once.

---

## 5. Start the app

**Mac / Linux**

```bash
./start.sh
```

**Windows (PowerShell)**

```powershell
.\start.ps1
```

**Windows (Command Prompt)**

```cmd
start.bat
```

Open **http://127.0.0.1:8765** in your browser.

---

## 6. Run the pipeline for the first time

In the Claude Code terminal (same session as step 4, or reopen with `claude`):

```
/morning-run
```

This discovers jobs matching your searches, scores them against your profile,
generates tailored CVs, and populates the web UI.

---

## Troubleshooting

**`.venv/bin/python` not found (Mac/Linux)** — you are in the wrong directory.
Run `pwd` and make sure you are in the project root (the folder that contains
`pyproject.toml`).

**`.venv\Scripts\python.exe` not found (Windows)** — same cause. Check that
you ran `python -m venv .venv` from the project root.

**`playwright install` fails** — run it again; the first run sometimes hits a
network timeout. If it keeps failing, run `.venv/bin/playwright install
--with-deps chromium` (adds OS-level browser dependencies on Linux).

**`npm install` fails** — make sure Node 18+ is installed: `node --version`.

**Port 8765 already in use** — another process is using the port. Either stop
it or change the port in `start.sh` / `start.bat` / `start.ps1` and in
`frontend/nuxt.config.ts` (`nitro.devProxy`, the dev-mode proxy target).
