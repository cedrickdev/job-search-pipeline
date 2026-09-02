"""Daily DB safety copy with 7-day retention.

Usage: .venv/bin/python -m pipeline.backup
"""
import shutil
from datetime import date
from pathlib import Path

from pipeline import paths


def backup_db(db_path: str | Path, backup_dir: str | Path,
              today: str | None = None, keep: int = 7) -> Path:
    db_path, backup_dir = Path(db_path), Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    today = today or date.today().isoformat()
    target = backup_dir / f"{db_path.stem}_{today}{db_path.suffix}"
    shutil.copy2(db_path, target)
    backups = sorted(backup_dir.glob(f"{db_path.stem}_*{db_path.suffix}"))
    for old in backups[:-keep]:
        old.unlink()
    return target


if __name__ == "__main__":
    print(f"wrote {backup_db(paths.DB_PATH, paths.BACKUP_DIR)}")
