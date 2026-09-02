# tests/test_backup.py
from pipeline.backup import backup_db


def test_backup_copies_db_and_prunes_old(tmp_path):
    db = tmp_path / "tracker.db"
    db.write_bytes(b"data")
    bdir = tmp_path / "backups"
    for old in ["2026-06-01", "2026-06-02", "2026-06-03", "2026-06-04",
                "2026-06-05", "2026-06-06", "2026-06-07"]:
        bdir.mkdir(exist_ok=True)
        (bdir / f"tracker_{old}.db").write_bytes(b"old")
    written = backup_db(db, bdir, today="2026-06-11", keep=7)
    assert written.name == "tracker_2026-06-11.db"
    remaining = sorted(p.name for p in bdir.glob("tracker_*.db"))
    assert len(remaining) == 7
    assert "tracker_2026-06-01.db" not in remaining


def test_backup_same_day_is_idempotent(tmp_path):
    db = tmp_path / "tracker.db"
    db.write_bytes(b"data")
    bdir = tmp_path / "backups"
    backup_db(db, bdir, today="2026-06-11")
    backup_db(db, bdir, today="2026-06-11")
    assert len(list(bdir.glob("tracker_*.db"))) == 1
