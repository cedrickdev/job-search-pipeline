"""`python -m backend.app.cli.rotate_credentials` — re-encrypt stored credentials (§22).

The operational half of secret rotation. An operator who is retiring a master key sets
the new key in `JOBSEARCH_LLM_SECRET_KEY`, bumps `JOBSEARCH_LLM_SECRET_KEY_VERSION`, and
keeps the old key available as `JOBSEARCH_LLM_SECRET_KEY_V<old-version>`. This command
then walks every stored credential and re-encrypts the ones still on an old version under
the new one, verifying each round trip before it writes.

It never prints a key or a credential: the report is counts and, for anything it could not
rotate, the connection id and version and a fixed reason. Run it with `--dry-run` first to
confirm the configured keys can decrypt every stale row; a clean dry run means the real run
will not leave a credential behind. Once the applied run reports zero failures, the old
`JOBSEARCH_LLM_SECRET_KEY_V<n>` can be removed from the environment.

Exit codes mirror the other CLIs: 0 clean, 1 misconfigured (no active key), 3 finished
with failures (some credential could not be rotated — supply its previous key and re-run).
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from typing import Final

from backend.app.core.settings import DatabaseSettings, LLMSecretSettings
from backend.app.infrastructure.database.engine import (
    create_async_database_engine,
    create_session_factory,
    session_scope,
)
from backend.app.llm.rotation import (
    CredentialRotationReport,
    CredentialRotationService,
    format_rotation_report,
)
from backend.app.llm.secrets import FernetSecretCipher
from backend.app.repositories.sqlalchemy_credential_rotation import (
    SqlAlchemyCredentialRotationStore,
)

EXIT_OK: Final[int] = 0
EXIT_UNUSABLE: Final[int] = 1
EXIT_INCOMPLETE: Final[int] = 3


class RotationCompositionError(ValueError):
    """The command cannot build the cipher the rotation needs."""


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m backend.app.cli.rotate_credentials",
        description="Re-encrypt every stored credential under the active master key "
                    "version. Set the new key in JOBSEARCH_LLM_SECRET_KEY, bump "
                    "JOBSEARCH_LLM_SECRET_KEY_VERSION, and keep the old key as "
                    "JOBSEARCH_LLM_SECRET_KEY_V<old-version>.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="verify every stale credential can be re-encrypted, but write nothing",
    )
    return parser.parse_args(argv)


def _build_cipher(settings: LLMSecretSettings) -> FernetSecretCipher:
    """The cipher the rotation decrypts old rows and writes new ones with.

    Fails closed: with no active key there is nothing to rotate *to*, so the command
    refuses rather than running a no-op that looks like success.
    """
    if not settings.master_key:
        raise RotationCompositionError(
            "no active credential encryption key is configured "
            "(set JOBSEARCH_LLM_SECRET_KEY)")
    try:
        return FernetSecretCipher(
            settings.master_key, version=settings.active_version,
            older=dict(settings.previous_keys))
    except (ValueError, TypeError) as exc:
        # A malformed Fernet key. The message names no key material.
        raise RotationCompositionError(
            "a configured key is not a valid Fernet key") from exc


async def _run_rotation(cipher: FernetSecretCipher, *,
                        dry_run: bool) -> CredentialRotationReport:
    engine = create_async_database_engine(DatabaseSettings.from_env())
    factory = create_session_factory(engine)
    try:
        async with session_scope(factory) as session:
            store = SqlAlchemyCredentialRotationStore(session)
            service = CredentialRotationService(store, cipher)
            return await service.rotate(dry_run=dry_run)
    finally:
        await engine.dispose()


def _run(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        cipher = _build_cipher(LLMSecretSettings.from_env())
    except RotationCompositionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_UNUSABLE

    try:
        report = asyncio.run(_run_rotation(cipher, dry_run=args.dry_run))
    except Exception as exc:  # a CLI reports a failure, it does not traceback
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_INCOMPLETE

    print(format_rotation_report(report))
    return EXIT_OK if report.is_clean else EXIT_INCOMPLETE


if __name__ == "__main__":  # pragma: no cover - exercised through _run()
    raise SystemExit(_run())
