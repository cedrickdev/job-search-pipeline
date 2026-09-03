"""Central path definitions. All other modules import paths from here."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
BACKUP_DIR = DATA_DIR / "backups"
DB_PATH = DATA_DIR / "tracker.db"
CV_DIR = ROOT / "cv"
# The operator's own CV library. Gitignored (real personal data), so it does not
# exist in a clean clone: the test suite points this constant at the synthetic
# tests/fixtures/base_cv.yaml instead. Read through pipeline.paths at call time,
# never captured at import, so that redirection works.
BASE_CV_PATH = CV_DIR / "base_cv.yaml"
CV_VERSIONS_DIR = ROOT / "cv_versions"
EXPORT_DIR = ROOT / "Rechercher d’emploi"  # apostrophe is U+2019
EXPORT_PATH = EXPORT_DIR / "Job_Search_Tracker.xlsx"
CONFIG_DIR = ROOT / "config"
SEARCHES_PATH = CONFIG_DIR / "searches.yaml"
COMPANIES_PATH = CONFIG_DIR / "companies.yaml"
STYLE_RULES_PATH = CV_DIR / "style_rules.yaml"
TAILORED_DIR = DATA_DIR / "tailored"
COVER_LETTERS_DIR = DATA_DIR / "cover_letters"
DIGEST_PATH = DATA_DIR / "digest_latest.md"
SETTINGS_PATH = DATA_DIR / "settings.json"
LOG_DIR = DATA_DIR / "logs"
MANDATE_CONFIG = DATA_DIR / "mandate_redactions.json"  # gitignored: {"forbidden": [...], "aliases": {...}}
WEBAPP_DIST = ROOT / "webapp" / "dist"  # built SPA served by server.app
WHISPER_MODEL_PATH = DATA_DIR / "models" / "ggml-small.bin"  # gitignored; fetched by scripts/fetch_whisper_model.sh
