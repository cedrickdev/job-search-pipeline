"""Private manager API; this application is never exposed to the browser."""
import asyncio
import json
from collections.abc import Callable
from secrets import compare_digest

from fastapi import FastAPI, Header, HTTPException, Response, WebSocket, WebSocketDisconnect, status

from terminal_manager.config import TerminalManagerSettings
from terminal_manager.docker_gateway import DockerSdkGateway
from terminal_manager.protocol import (
    CreateTerminalRuntimeRequest,
    CreateTerminalRuntimeResponse,
)
from terminal_manager.runtime import (
    TerminalCapacityReached,
    TerminalRuntimeManager,
    TerminalRuntimeUnavailable,
)


def _require_token(expected: str, supplied: str | None) -> None:
    if supplied is None or not compare_digest(expected, supplied):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="terminal manager authentication failed")


def create_app(
        *, manager: TerminalRuntimeManager | None = None,
        manager_token: str | None = None,
        manager_factory: Callable[[TerminalManagerSettings], TerminalRuntimeManager] | None = None,
) -> FastAPI:
    """Build the internal service without accepting public/browser configuration."""
    if manager is None:
        settings = TerminalManagerSettings.from_env()
        if manager_factory is None:
            def manager_factory(config: TerminalManagerSettings) -> TerminalRuntimeManager:
                return TerminalRuntimeManager(
                    DockerSdkGateway.from_env(), image=config.runtime_image,
                    max_sessions=config.max_sessions, memory_limit=config.memory_limit,
                    nano_cpus=config.nano_cpus, pids_limit=config.pids_limit,
                    network=config.terminal_network,
                    anthropic_api_key=config.anthropic_api_key,
                    anthropic_base_url=config.anthropic_base_url,
                    anthropic_model=config.anthropic_model,
                )
        manager = manager_factory(settings)
        manager_token = settings.manager_token
    if not manager_token:
        raise ValueError("manager_token is required")

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/internal/sessions", status_code=status.HTTP_201_CREATED,
              response_model=CreateTerminalRuntimeResponse)
    async def create_session(
            body: CreateTerminalRuntimeRequest,
            x_terminal_manager_token: str | None = Header(default=None),
    ) -> CreateTerminalRuntimeResponse:
        _require_token(manager_token, x_terminal_manager_token)
        try:
            runtime = manager.create(body.session_id, expires_at=body.expires_at)
        except TerminalCapacityReached as exc:
            raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                                detail="TERMINAL_CAPACITY_REACHED") from exc
        except TerminalRuntimeUnavailable as exc:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                                detail="TERMINAL_START_FAILED") from exc
        return CreateTerminalRuntimeResponse(runtime_reference=runtime.reference)

    @app.delete("/internal/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
    async def stop_session(
            session_id: str,
            x_terminal_manager_token: str | None = Header(default=None),
    ) -> Response:
        _require_token(manager_token, x_terminal_manager_token)
        from uuid import UUID
        try:
            manager.stop_session(UUID(session_id))
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                                detail="invalid session id") from exc
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.websocket("/internal/sessions/{session_id}/stream")
    async def stream_session(websocket: WebSocket, session_id: str) -> None:
        supplied = websocket.headers.get("x-terminal-manager-token")
        if supplied is None or not compare_digest(manager_token, supplied):
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return
        from uuid import UUID
        try:
            runtime = manager.get(UUID(session_id))
        except ValueError:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return
        if runtime is None:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return

        attachment = manager.attach(runtime)
        raw_socket = attachment.socket
        await websocket.accept()

        async def incoming() -> None:
            while True:
                message = await websocket.receive_text()
                try:
                    event = json.loads(message)
                except json.JSONDecodeError:
                    await websocket.close(code=status.WS_1003_UNSUPPORTED_DATA)
                    return
                if not isinstance(event, dict):
                    await websocket.close(code=status.WS_1003_UNSUPPORTED_DATA)
                    return
                kind = event.get("type")
                if kind == "input" and isinstance(event.get("data"), str):
                    data = event["data"].encode()
                    if len(data) > 32_768:
                        await websocket.close(code=status.WS_1009_MESSAGE_TOO_BIG)
                        return
                    await asyncio.to_thread(raw_socket.sendall, data)
                elif (kind == "resize" and isinstance(event.get("cols"), int)
                      and isinstance(event.get("rows"), int)):
                    try:
                        manager.resize(attachment, cols=event["cols"], rows=event["rows"])
                    except ValueError:
                        await websocket.close(code=status.WS_1003_UNSUPPORTED_DATA)
                        return
                elif kind == "ping":
                    continue
                else:
                    await websocket.close(code=status.WS_1003_UNSUPPORTED_DATA)
                    return

        async def outgoing() -> None:
            while True:
                try:
                    data = await asyncio.to_thread(raw_socket.recv, 32_768)
                except TimeoutError:
                    # Docker's HTTP socket keeps a read timeout after upgrade.
                    # An idle prompt is not a dead terminal or an expired session.
                    continue
                if not data:
                    await websocket.close(code=status.WS_1000_NORMAL_CLOSURE)
                    return
                await websocket.send_bytes(data)

        try:
            await asyncio.gather(incoming(), outgoing())
        except WebSocketDisconnect:
            return
        finally:
            close = getattr(raw_socket, "close", None)
            if callable(close):
                await asyncio.to_thread(close)

    return app
