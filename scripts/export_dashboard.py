"""Export job tracker DB to a polished Excel dashboard."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import openpyxl
from openpyxl.styles import Alignment, Font, PatternFill, Border, Side
from openpyxl.utils import get_column_letter

DB_PATH = Path(__file__).parent.parent / "data" / "tracker.db"
OUT_PATH = Path(__file__).parent.parent / "data" / "dashboard.xlsx"

# ── Palette ───────────────────────────────────────────────────────────────────
P = {
    "page_bg":    "F0F4F8",
    "banner":     "1B2A3B",
    "banner2":    "243447",
    "white":      "FFFFFF",
    "card_border":"D8E2EE",
    "row_alt":    "F7F9FC",
    "text_dark":  "1B2A3B",
    "text_mid":   "4A5568",
    "text_light": "8FA0B5",
    "divider":    "E2E8F0",
    # Accent colors
    "blue":       "2E86DE",
    "green":      "27AE60",
    "teal":       "17A589",
    "orange":     "E67E22",
    "purple":     "7D3C98",
    "red":        "C0392B",
    "grey":       "7F8C8D",
    # Card header tints (20% opacity simulation)
    "blue_tint":  "D6EAFB",
    "green_tint": "D5F5E3",
    "teal_tint":  "D1F2EB",
    "orange_tint":"FDEBD0",
    "purple_tint":"E8DAEF",
    "grey_tint":  "EAEDED",
}

STATUS_COLOR = {
    "Applied":         "green",
    "Recruiter reply": "teal",
    "Needs you":       "orange",
    "Ready to apply":  "blue",
    "Borderline":      "purple",
    "Archived":        "grey",
    "Rejected":        "red",
}


# ── Micro helpers ─────────────────────────────────────────────────────────────

def F(hex_color: str) -> PatternFill:
    return PatternFill("solid", fgColor=hex_color)


def fnt(color=P["text_dark"], size=10, bold=False, italic=False) -> Font:
    return Font(name="Calibri", color=color, size=size, bold=bold, italic=italic)


def aln(h="left", v="center", wrap=False) -> Alignment:
    return Alignment(horizontal=h, vertical=v, wrap_text=wrap)


def side(color, style="thin") -> Side:
    return Side(style=style, color=color)


def outer_border(color: str, style="thin") -> Border:
    s = side(color, style)
    return Border(left=s, right=s, top=s, bottom=s)


def apply_outer_box(ws, r1, c1, r2, c2, color: str, style="thin"):
    """Draw a border box around a cell range."""
    for r in range(r1, r2 + 1):
        for c in range(c1, c2 + 1):
            top    = side(color, style) if r == r1 else Side(style=None)
            bottom = side(color, style) if r == r2 else Side(style=None)
            left   = side(color, style) if c == c1 else Side(style=None)
            right  = side(color, style) if c == c2 else Side(style=None)
            existing = ws.cell(r, c).border
            ws.cell(r, c).border = Border(
                top=top or existing.top,
                bottom=bottom or existing.bottom,
                left=left or existing.left,
                right=right or existing.right,
            )


def fill_block(ws, r1, c1, r2, c2, hex_color: str):
    for r in range(r1, r2 + 1):
        for c in range(c1, c2 + 1):
            ws.cell(r, c).fill = F(hex_color)


def set_row_heights(ws, heights: dict[int, float]):
    for r, h in heights.items():
        ws.row_dimensions[r].height = h


def merge(ws, r1, c1, r2, c2):
    ws.merge_cells(
        f"{get_column_letter(c1)}{r1}:{get_column_letter(c2)}{r2}"
    )
    return ws.cell(r1, c1)


# ── Fetch ─────────────────────────────────────────────────────────────────────

def fetch_stats(conn) -> dict:
    counts = {r[0]: r[1] for r in conn.execute(
        "SELECT status, COUNT(*) FROM applications GROUP BY status"
    ).fetchall()}
    applied   = counts.get("Applied", 0)
    recruiter = counts.get("Recruiter reply", 0)
    total     = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    cv_pairs  = conn.execute(
        "SELECT COUNT(*) FROM cv_versions WHERE language='en'"
    ).fetchone()[0]
    response_rate = round(recruiter / applied * 100) if applied else 0
    return {
        "total": total, "applied": applied, "recruiter": recruiter,
        "needs_you": counts.get("Needs you", 0),
        "borderline": counts.get("Borderline", 0),
        "ready": counts.get("Ready to apply", 0),
        "archived": counts.get("Archived", 0),
        "rejected": counts.get("Rejected", 0),
        "cv_pairs": cv_pairs, "response_rate": response_rate, "counts": counts,
    }


def fetch_jobs(conn) -> list[dict]:
    rows = conn.execute("""
        SELECT j.id, j.company, j.title, COALESCE(j.language,'fr') AS lang,
               a.status, s.score, a.submitted_at,
               (SELECT pdf_path FROM cv_versions WHERE job_id=j.id AND language='en'
                ORDER BY id DESC LIMIT 1) AS cv_en,
               (SELECT pdf_path FROM cv_versions WHERE job_id=j.id AND language='fr'
                ORDER BY id DESC LIMIT 1) AS cv_fr,
               j.url
        FROM jobs j
        LEFT JOIN applications a ON a.job_id=j.id
        LEFT JOIN scores s ON s.job_id=j.id
        ORDER BY
            CASE a.status
                WHEN 'Recruiter reply' THEN 1 WHEN 'Applied' THEN 2
                WHEN 'Needs you' THEN 3 WHEN 'Ready to apply' THEN 4
                WHEN 'Borderline' THEN 5 WHEN 'Archived' THEN 6
                WHEN 'Rejected' THEN 7 ELSE 8
            END, j.company, j.id
    """).fetchall()
    return [dict(r) for r in rows]


# ── Dashboard sheet ───────────────────────────────────────────────────────────
#
# Column layout (16 cols):
#  A(1)  B-E(2-5)  F(6)  G-J(7-10)  K(11)  L-O(12-15)  P(16)
#  marg  CARD      gap   CARD        gap    CARD          marg
#
# Card columns span 4 cols each.  Gap cols = 1.5 wide.

CARD_COLS = [(2, 5), (7, 10), (12, 15)]  # (start, end) col for each card column
COL_WIDTHS = {
    1: 1.5, 2: 2.5, 3: 11, 4: 11, 5: 2.5,
    6: 1.5,
    7: 2.5, 8: 11, 9: 11, 10: 2.5,
    11: 1.5,
    12: 2.5, 13: 11, 14: 11, 15: 2.5,
    16: 1.5,
}

# Row layout:
#  1      : top margin
#  2-5    : banner
#  6      : gap
#  7      : cards row-1 — header strip
#  8      : cards row-1 — header strip
#  9      : cards row-1 — big number
#  10     : cards row-1 — big number
#  11     : cards row-1 — sublabel
#  12     : cards row-1 — bottom pad
#  13     : gap between card rows
#  14     : cards row-2 — header strip
#  15     : cards row-2 — header strip
#  16     : cards row-2 — big number
#  17     : cards row-2 — big number
#  18     : cards row-2 — sublabel
#  19     : cards row-2 — bottom pad
#  20     : gap
#  21     : pipeline section header
#  22-35  : pipeline rows (2 rows per status + spacers)
#  36     : bottom margin

ROW_HEIGHTS = {
    1: 6,
    2: 8, 3: 30, 4: 18, 5: 8,
    6: 10,
    7: 8, 8: 24, 9: 10, 10: 34, 11: 18, 12: 10,
    13: 8,
    14: 8, 15: 24, 16: 10, 17: 34, 18: 18, 19: 10,
    20: 12,
    21: 24,
}

CARD_DEFS = [
    # row=1 of cards
    ("JOBS DISCOVERED",  lambda s: s["total"],         "total in pipeline",            "blue",   "blue_tint"),
    ("APPLICATIONS SENT",lambda s: s["applied"],        "forms submitted",              "green",  "green_tint"),
    ("REPLY RATE",       lambda s: f"{s['response_rate']}%", f"{{recruiter}} recruiter replies", "teal", "teal_tint"),
    # row=2 of cards
    ("ACTION REQUIRED",  lambda s: s["needs_you"],      "CAPTCHA / manual submit",      "orange", "orange_tint"),
    ("BORDERLINE",       lambda s: s["borderline"],     "pending CV tailoring",         "purple", "purple_tint"),
    ("CVs GENERATED",    lambda s: s["cv_pairs"],       "EN + FR pairs ready",          "blue",   "blue_tint"),
]

CARD_ROW_OFFSETS = [
    (7, 8, 9, 10, 11, 12),   # first row of cards
    (14, 15, 16, 17, 18, 19), # second row of cards
]


def draw_card(ws, col_start, col_end, rows, label, value, sublabel, accent, tint):
    r_strip1, r_strip2, r_num1, r_num2, r_sub, r_pad = rows
    accent_hex = P[accent]
    tint_hex   = P[tint]

    # Top colored strip (2 rows)
    fill_block(ws, r_strip1, col_start, r_strip2, col_end, accent_hex)
    c = merge(ws, r_strip1, col_start, r_strip2, col_end)
    c.value     = label
    c.font      = fnt(P["white"], size=9, bold=True)
    c.alignment = aln("center", "center")

    # Number rows (2 rows, tint bg)
    fill_block(ws, r_num1, col_start, r_num2, col_end, P["white"])
    c = merge(ws, r_num1, col_start, r_num2, col_end)
    c.value     = value
    c.font      = Font(name="Calibri", size=30, bold=True, color=accent_hex)
    c.alignment = aln("center", "center")

    # Sub-label
    fill_block(ws, r_sub, col_start, r_sub, col_end, P["white"])
    c = merge(ws, r_sub, col_start, r_sub, col_end)
    c.value     = sublabel
    c.font      = fnt(P["text_light"], size=9, italic=True)
    c.alignment = aln("center", "center")

    # Bottom pad
    fill_block(ws, r_pad, col_start, r_pad, col_end, P["white"])

    # Outer border
    apply_outer_box(ws, r_strip1, col_start, r_pad, col_end, P["card_border"])
    # Accent top border (thicker)
    for c in range(col_start, col_end + 1):
        ws.cell(r_strip1, c).border = Border(
            top=side(accent_hex, "medium"),
            bottom=ws.cell(r_strip1, c).border.bottom,
            left=ws.cell(r_strip1, c).border.left,
            right=ws.cell(r_strip1, c).border.right,
        )


def write_dashboard(ws, s: dict):
    ws.sheet_view.showGridLines   = False
    ws.sheet_view.showRowColHeaders = False

    # Set column widths
    for col, w in COL_WIDTHS.items():
        ws.column_dimensions[get_column_letter(col)].width = w

    # Set row heights
    for r, h in ROW_HEIGHTS.items():
        ws.row_dimensions[r].height = h

    # ── Fill entire canvas with page background ──────────────────────────────
    for r in range(1, 40):
        for c in range(1, 17):
            ws.cell(r, c).fill = F(P["page_bg"])

    # ── Banner ───────────────────────────────────────────────────────────────
    fill_block(ws, 1, 1, 5, 16, P["banner"])

    # Title
    c = merge(ws, 3, 2, 3, 10)
    c.value     = "JOB SEARCH TRACKER"
    c.font      = Font(name="Calibri", bold=True, size=20, color=P["white"])
    c.alignment = aln("left", "center")

    # Date
    c = merge(ws, 4, 2, 4, 8)
    c.value     = datetime.today().strftime("Updated %d %B %Y")
    c.font      = fnt(P["text_light"], size=9, italic=True)
    c.fill      = F(P["banner"])
    c.alignment = aln("left", "center")

    # Right side stats
    c = merge(ws, 3, 11, 3, 15)
    c.value     = f"{s['applied']} sent  ·  {s['recruiter']} replies  ·  {s['cv_pairs']} CVs"
    c.font      = fnt("95A5A6", size=10)
    c.fill      = F(P["banner"])
    c.alignment = aln("right", "center")

    # Blue accent line at bottom of banner
    for col in range(1, 17):
        ws.cell(5, col).fill = F("2E86DE")
        ws.cell(5, col).border = Border(bottom=side("1A6DB5", "thick"))
    ws.row_dimensions[5].height = 4

    # ── KPI Cards ────────────────────────────────────────────────────────────
    for card_idx, card_def in enumerate(CARD_DEFS):
        label, val_fn, sublabel_tmpl, accent, tint = card_def
        row_group  = card_idx // 3
        col_group  = card_idx % 3
        col_start, col_end = CARD_COLS[col_group]
        rows = CARD_ROW_OFFSETS[row_group]

        raw_val = val_fn(s)
        # Substitute {recruiter} in sublabel
        sublabel = sublabel_tmpl.replace("{recruiter}", str(s["recruiter"]))

        draw_card(ws, col_start, col_end, rows, label, raw_val, sublabel, accent, tint)

    # ── Section header: Pipeline ──────────────────────────────────────────────
    fill_block(ws, 21, 1, 21, 16, P["banner"])
    c = merge(ws, 21, 2, 21, 15)
    c.value     = "  APPLICATION PIPELINE"
    c.font      = Font(name="Calibri", bold=True, size=10, color=P["white"])
    c.alignment = aln("left", "center")
    ws.row_dimensions[21].height = 24

    # ── Pipeline rows ─────────────────────────────────────────────────────────
    pipeline = [
        ("Recruiter reply", P["teal"],   s["recruiter"]),
        ("Applied",         P["green"],  s["applied"]),
        ("Needs you",       P["orange"], s["needs_you"]),
        ("Ready to apply",  P["blue"],   s["ready"]),
        ("Borderline",      P["purple"], s["borderline"]),
        ("Archived",        P["grey"],   s["archived"]),
        ("Rejected",        P["red"],    s["rejected"]),
    ]
    total_all = sum(x[2] for x in pipeline) or 1

    base_row = 22
    for i, (label, color, count) in enumerate(pipeline):
        r = base_row + i * 2
        ws.row_dimensions[r].height     = 20
        ws.row_dimensions[r + 1].height = 4

        fill_block(ws, r, 1, r + 1, 16, P["page_bg"])

        # Status label (colored badge)
        fill_block(ws, r, 2, r, 4, color)
        c = merge(ws, r, 2, r, 4)
        c.value     = f"  {label}"
        c.font      = fnt(P["white"], size=9, bold=True)
        c.alignment = aln("left", "center")

        # Count
        fill_block(ws, r, 5, r, 5, color)
        c = ws.cell(r, 5, value=count)
        c.font      = Font(name="Calibri", bold=True, size=10, color=P["white"])
        c.fill      = F(color)
        c.alignment = aln("center", "center")

        # Bar (cols 6-14 = 9 cols)
        bar_cols = 9
        filled   = round((count / total_all) * bar_cols) if count else 0
        for bc in range(bar_cols):
            col = 6 + bc
            if bc < filled:
                ws.cell(r, col).fill = F(color)
            else:
                ws.cell(r, col).fill = F(P["divider"])

        # Percentage
        pct = round(count / total_all * 100)
        c = ws.cell(r, 15, value=f"{pct}%")
        c.font      = fnt(P["text_mid"], size=9, bold=True)
        c.fill      = F(P["page_bg"])
        c.alignment = aln("left", "center")

    # Bottom margin
    ws.row_dimensions[base_row + 14].height = 12


# ── All Jobs sheet ────────────────────────────────────────────────────────────

def write_jobs(ws, rows: list[dict]):
    ws.sheet_view.showGridLines = False

    col_defs = [
        ("#",         5,   "center"),
        ("Company",   22,  "left"),
        ("Role",      45,  "left"),
        ("Status",    18,  "center"),
        ("Score",     7,   "center"),
        ("Lang",      6,   "center"),
        ("CV EN",     7,   "center"),
        ("CV FR",     7,   "center"),
        ("Submitted", 14,  "center"),
        ("URL",       50,  "left"),
    ]
    for col, (_, w, __) in enumerate(col_defs, 1):
        ws.column_dimensions[get_column_letter(col)].width = w

    # Header row
    ws.row_dimensions[1].height = 26
    for col, (h, _, __) in enumerate(col_defs, 1):
        c = ws.cell(row=1, column=col, value=h)
        c.font      = Font(name="Calibri", bold=True, color=P["white"], size=10)
        c.fill      = F(P["banner"])
        c.alignment = aln("center", "center")
        c.border    = Border(
            bottom=side(P["blue"], "medium"),
            left=side("2A3F5A", "thin"),
            right=side("2A3F5A", "thin"),
        )

    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(col_defs))}1"

    last_status = None
    for i, row in enumerate(rows, start=2):
        status  = row.get("status") or ""
        accent  = STATUS_COLOR.get(status, "grey")
        color   = P[accent]
        bg      = P["row_alt"] if i % 2 == 0 else P["white"]

        ws.row_dimensions[i].height = 16

        score = row.get("score")
        score_color = (
            P["green"] if score and score >= 75 else
            P["orange"] if score and score >= 60 else
            P["red"]   if score else P["text_light"]
        )

        values = [
            row["id"],
            row["company"],
            row["title"],
            status,
            score,
            (row.get("lang") or "").upper(),
            "✓" if row.get("cv_en") else "·",
            "✓" if row.get("cv_fr") else "·",
            (row.get("submitted_at") or "")[:10],
            row.get("url") or "",
        ]
        aligns = [c[2] for c in col_defs]
        thin = side("E2E8F0")
        none = Side(style=None)

        # Subtle divider when status group changes
        top_border_style = side(P["divider"], "thin") if status != last_status and last_status is not None else none
        last_status = status

        for col, (val, a) in enumerate(zip(values, aligns), 1):
            c = ws.cell(row=i, column=col, value=val)
            c.alignment = aln(a, "center")
            c.border    = Border(left=thin, right=thin,
                                 top=top_border_style if col == 1 else none,
                                 bottom=none)

            if col == 4:  # Status badge
                c.fill = F(color)
                c.font = Font(name="Calibri", bold=True, color=P["white"], size=9)
                c.alignment = aln("center", "center")
            elif col == 5:  # Score
                c.fill = F(bg)
                c.font = Font(name="Calibri", bold=bool(score), color=score_color, size=10)
            elif col in (7, 8):  # CV checkmarks
                cv_ok = val == "✓"
                c.fill = F(bg)
                c.font = Font(name="Calibri", color=P["green"] if cv_ok else P["text_light"],
                              size=10, bold=cv_ok)
            else:
                c.fill = F(bg)
                c.font = fnt(P["text_dark"] if col in (2, 3) else P["text_mid"], size=10)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    stats = fetch_stats(conn)
    jobs  = fetch_jobs(conn)

    wb = openpyxl.Workbook()
    ws_dash = wb.active
    ws_dash.title = "Dashboard"
    write_dashboard(ws_dash, stats)

    ws_jobs = wb.create_sheet("All Jobs")
    write_jobs(ws_jobs, jobs)

    wb.save(OUT_PATH)
    conn.close()

    print(f"✓  Saved → {OUT_PATH}")
    print(f"   {stats['total']} jobs  ·  {stats['applied']} applied  "
          f"·  {stats['recruiter']} replies  ·  {stats['response_rate']}% rate")


if __name__ == "__main__":
    main()
