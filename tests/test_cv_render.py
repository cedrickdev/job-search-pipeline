# tests/test_cv_render.py
import copy

from pipeline.cv_render import load_base_cv, render_pdf


def test_base_cv_renders_to_one_page_en_and_fr(tmp_path):
    cv = load_base_cv()
    for lang in ("en", "fr"):
        out = tmp_path / f"cv_{lang}.pdf"
        result = render_pdf(cv, lang, out)
        assert out.exists()
        assert result["pages"] == 1


def test_overstuffed_cv_drops_low_priority_bullets_to_fit(tmp_path):
    cv = load_base_cv()
    fat = copy.deepcopy(cv)
    filler = {"id": "filler", "priority": 3, "en": "Filler bullet " * 12,
              "fr": "Puce de remplissage " * 12}
    for i in range(15):
        fat["experience"][0]["bullets"].append({**filler, "id": f"filler-{i}"})
    result = render_pdf(fat, "en", tmp_path / "fat.pdf")
    assert result["pages"] == 1
    dropped = set(result["dropped"])
    assert {f"filler-{i}" for i in range(15)} <= dropped
    priority_one = {b["id"] for exp in cv["experience"]
                    for b in exp["bullets"] if b["priority"] == 1}
    assert not priority_one & dropped


def test_render_pdf_reports_high_fill_for_dense_base(tmp_path):
    result = render_pdf(load_base_cv(), "en", tmp_path / "cv.pdf")
    assert 0.92 < result["fill"] <= 1.0


def test_fill_grows_with_content(tmp_path):
    cv = load_base_cv()
    sparse = copy.deepcopy(cv)
    for exp in sparse["experience"]:
        exp["bullets"] = exp["bullets"][:1]
    denser = copy.deepcopy(sparse)
    denser["experience"][0]["bullets"] = copy.deepcopy(
        cv["experience"][0]["bullets"][:3])
    sparse_fill = render_pdf(sparse, "en", tmp_path / "sparse.pdf")["fill"]
    denser_fill = render_pdf(denser, "en", tmp_path / "denser.pdf")["fill"]
    assert sparse_fill < 0.92
    assert sparse_fill < denser_fill
