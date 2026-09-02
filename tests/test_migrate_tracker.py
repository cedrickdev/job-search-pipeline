import openpyxl

from pipeline.migrate_tracker import migrate


def make_fixture(path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Tracker"
    ws.append(["Job Search Tracker  •  Generated 2026-05-15"])
    ws.append(["Date Received", "Company", "Role / Position", "Status", "Category",
               "Source / Channel", "Recruiter / Contact", "Contact Email", "Location",
               "Salary / Comp", "Interview Round", "Next Step / Action",
               "Follow-up Date", "Email Subject", "Notes"])
    ws.append(["2026-05-01", "Northwind", "Sales Assistant - Fresh Produce",
               "Interview Scheduled", "Interview", "Direct (Ashby)", "Ana S.",
               "ana@example.com", "Lausanne", "", "Next round", "Choose slot", "",
               "subject", "notes"])
    ws.append(["2026-05-02", "Initech", "Shift Supervisor", "Recruiter Outreach",
               "Outreach", "LinkedIn", "", "", "Lausanne", "", "", "", "", "", ""])
    wb.save(path)


def test_migrate_imports_rows_with_mapped_statuses(conn, tmp_path):
    xlsx = tmp_path / "fixture.xlsx"
    make_fixture(xlsx)
    report = migrate(conn, xlsx)
    assert report["imported"] == 2
    rows = conn.execute(
        "SELECT j.company, j.title, a.status FROM applications a JOIN jobs j ON j.id = a.job_id"
        " ORDER BY j.company"
    ).fetchall()
    # titles prove the spaced header "Role / Position" was normalized correctly,
    # and the order follows ORDER BY j.company, not the row order in the sheet
    assert [(r["company"], r["title"], r["status"]) for r in rows] == [
        ("Initech", "Shift Supervisor", "Recruiter reply"),
        ("Northwind", "Sales Assistant - Fresh Produce", "Interview scheduled"),
    ]


def test_migrate_is_idempotent(conn, tmp_path):
    xlsx = tmp_path / "fixture.xlsx"
    make_fixture(xlsx)
    migrate(conn, xlsx)
    report = migrate(conn, xlsx)
    assert report["imported"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM applications").fetchone()["c"] == 2
