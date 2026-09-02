"""Platform handler registry."""
from __future__ import annotations

from pipeline.apply._common import ApplyResult, detect_platform  # noqa: F401


def _get_handler(platform: str):
    if platform == "linkedin":
        from pipeline.apply import linkedin
        return linkedin
    if platform == "wtj":
        from pipeline.apply import wtj
        return wtj
    if platform == "jobup":
        from pipeline.apply import jobup
        return jobup
    if platform == "umantis":
        from pipeline.apply import umantis
        return umantis
    if platform == "migros":
        from pipeline.apply import migros
        return migros
    if platform in ("greenhouse",):
        from pipeline.apply import greenhouse
        return greenhouse
    if platform == "lever":
        from pipeline.apply import lever
        return lever
    if platform == "ashby":
        from pipeline.apply import ashby
        return ashby
    from pipeline.apply import generic
    return generic
