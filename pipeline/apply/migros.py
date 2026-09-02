"""Migros (jobs.migros.ch) job postings. The posting page itself has no
application form — "Postuler maintenant" is a target="_blank" link straight
to that employer's Umantis recruiting instance. Follow it and delegate."""
from __future__ import annotations

from pipeline.apply._common import ApplyResult, detect_platform, screenshot_path


async def apply(page, job: dict, answers: dict) -> ApplyResult:
    job_id = job["job_id"]
    await page.goto(job["url"], wait_until="domcontentloaded", timeout=30000)
    try:
        await page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass

    apply_link = page.locator("a:has-text('Postuler maintenant'), a:has-text('Postuler')").first
    if not await apply_link.count():
        shot = screenshot_path(job_id, "migros_no_apply_link")
        await page.screenshot(path=str(shot))
        return ApplyResult("Needs you", "migros: no 'Postuler maintenant' link found", str(shot))

    href = await apply_link.get_attribute("href")
    if not href:
        shot = screenshot_path(job_id, "migros_no_apply_href")
        await page.screenshot(path=str(shot))
        return ApplyResult("Needs you", "migros: apply link has no href", str(shot))

    await page.goto(href, wait_until="domcontentloaded", timeout=30000)
    platform = detect_platform(page.url)
    if platform == "umantis":
        from pipeline.apply import umantis
        job_redirected = {**job, "url": page.url}
        return await umantis.apply(page, job_redirected, answers)

    shot = screenshot_path(job_id, "migros_unknown_ats")
    await page.screenshot(path=str(shot))
    return ApplyResult(
        "Needs you",
        f"migros: apply link redirected to an unrecognized platform ({page.url})",
        str(shot),
    )
