"""Load the Swiss pack. Six lines of behaviour; everything else is YAML.

`lru_cache` is not premature optimization: `load()` reads and validates five files,
and the orchestrator asks for the pack once per source per request. Caching a
frozen `CountryPack` is safe precisely because it is frozen — no caller can mutate
the shared instance, so there is no copy to make and no staleness to reason about
within a process. A pack file edited on disk needs a restart, which is the same
contract `config/searches.yaml` already has in V1.
"""
from functools import lru_cache
from pathlib import Path
from typing import Final

from country_packs.contracts import CountryPack
from country_packs.loader import load_pack

COUNTRY: Final = "CH"
PACK_DIR: Final = Path(__file__).resolve().parent


@lru_cache(maxsize=1)
def load() -> CountryPack:
    """The Swiss pack, validated. Raises `CountryPackError` if the files are wrong.

    `expected_country="CH"` is the directory asserting what it contains: a
    `metadata.yaml` that said `FR` would otherwise register a French pack under
    `country_packs/ch`, and the symptom — "Swiss searches find nothing" — points
    nowhere near the cause.
    """
    return load_pack(PACK_DIR, expected_country=COUNTRY)
