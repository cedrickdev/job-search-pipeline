from dashboard import queries
from tests.helpers import seed_job
from pipeline.statuses import set_status


def _seed_run(conn):
    conn.execute(
        "INSERT INTO runs (kind, started_at, finished_at, summary)"
        " VALUES ('discovery', '2026-06-11T08:00:00', '2026-06-11T08:05:00',"
        " '{\"health\": {\"wtj\": {\"ok\": true, \"found\": 4, \"new\": 2}}}')")
    conn.commit()


def test_digest_header_empty_db(conn):
    header = queries.digest_header(conn)
    assert header == {"last_run": None, "health": {}, "counts": {}}


def test_digest_header_with_run_and_counts(conn):
    _seed_run(conn)
    seed_job(conn)
    header = queries.digest_header(conn)
    assert header["last_run"] == "2026-06-11T08:05:00"
    assert header["health"]["wtj"]["ok"] is True
    assert header["counts"] == {"Ready to apply": 1}


def test_queue_cards_filters_and_orders(conn):
    seed_job(conn, company="Lower", score=80)
    seed_job(conn, company="Higher", score=95)
    seed_job(conn, company="NotReady", status="Borderline", score=99)
    cards = queries.queue_cards(conn)
    assert [c["company"] for c in cards] == ["Higher", "Lower"]
    assert cards[0]["score"] == 95


def test_borderline_cards(conn):
    seed_job(conn, company="Edge", status="Borderline", score=60)
    cards = queries.borderline_cards(conn)
    assert len(cards) == 1
    assert cards[0]["company"] == "Edge"


def test_auto_approved_today_filters_by_source(conn):
    _, app_auto = seed_job(conn, company="AutoCo")
    set_status(conn, app_auto, "Approved", source="auto-approve")
    _, app_manual = seed_job(conn, company="ManualCo")
    set_status(conn, app_manual, "Approved", source="dashboard")
    rows = queries.auto_approved_today(conn)
    assert [r["company"] for r in rows] == ["AutoCo"]


def test_board_hides_empty_statuses(conn):
    seed_job(conn, company="ReadyCo")
    seed_job(conn, company="EdgeCo", status="Borderline")
    columns = queries.board(conn)
    assert list(columns.keys()) == ["Borderline", "Ready to apply"]
    assert columns["Ready to apply"][0]["company"] == "ReadyCo"


def test_job_detail_unknown_job(conn):
    assert queries.job_detail(conn, 999) is None


def test_job_detail_assembles_everything(conn):
    job_id, app_id = seed_job(conn, score=91)
    conn.execute(
        "INSERT INTO cv_versions (job_id, language, pdf_path, content_hash,"
        " diff_summary, created_at) VALUES (?, 'en', '/tmp/x.pdf', 'h',"
        " 'Emphasized service', '2026-06-11T08:00:00')", (job_id,))
    conn.commit()
    detail = queries.job_detail(conn, job_id)
    assert detail["job"]["company"] == "Acme"
    assert detail["application"]["id"] == app_id
    assert detail["score"]["score"] == 91
    assert detail["cv_versions"]["en"]["diff_summary"] == "Emphasized service"
    assert detail["cv_versions"]["fr"] is None
