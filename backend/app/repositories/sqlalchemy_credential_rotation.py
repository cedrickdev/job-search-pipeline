"""Operator-scope access to every stored credential, for a key rotation (Phase 16 §22).

The one credential path that reaches across owners, and deliberately so: rotating the
master key means re-encrypting every account's stored credential under the new version,
which no user-scoped repository can do. It is reachable only from
`backend.app.cli.rotate_credentials`, never from a request handler, and its surface is
exactly two operations — stream the rows that carry a credential, and rewrite one row's
ciphertext and version. It never decrypts: the plaintext exists only inside the rotation
service, for the instant between decrypting under the old key and re-encrypting under the
new one, and never touches this store.

The rewrite is a targeted `UPDATE` of two columns, so it cannot disturb the single-default
unique index or any other field, and it is conditioned on the row still carrying a
credential so a key cleared out from under a running rotation is a no-op rather than a
resurrection.
"""
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.domain.identifiers import LLMConnectionId
from backend.app.infrastructure.database.mappers import llm_connection_to_domain
from backend.app.infrastructure.database.models import LLMConnectionRow
from backend.app.llm.connection import LLMConnection
from backend.app.repositories.contracts import DEFAULT_LIMIT
from backend.app.repositories.sqlalchemy_repositories import _rows_affected


class SqlAlchemyCredentialRotationStore:
    """`CredentialRotationStore` over an `AsyncSession` — all owners, credentials only."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_credentialed(
        self, *, after_id: LLMConnectionId | None = None,
        limit: int = DEFAULT_LIMIT,
    ) -> tuple[LLMConnection, ...]:
        statement = select(LLMConnectionRow).where(
            LLMConnectionRow.encrypted_api_key.is_not(None))
        if after_id is not None:
            # Keyset paging: strictly after the last id a batch returned. Re-encryption
            # never changes the id, so a page cannot re-serve a row it already rewrote.
            statement = statement.where(LLMConnectionRow.id > after_id)
        result = await self._session.execute(
            statement.order_by(LLMConnectionRow.id).limit(limit))
        return tuple(llm_connection_to_domain(row) for row in result.scalars())

    async def reencrypt(self, connection_id: LLMConnectionId, *,
                        ciphertext: str, secret_version: int) -> bool:
        result = await self._session.execute(
            update(LLMConnectionRow)
            .where(LLMConnectionRow.id == connection_id,
                   LLMConnectionRow.encrypted_api_key.is_not(None))
            .values(encrypted_api_key=ciphertext, secret_version=secret_version))
        return _rows_affected(result) > 0
