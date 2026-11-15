"""HTTP 層: POST /v2/rpc と GET /health。"""

from __future__ import annotations

import hmac
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError

from . import SERVICE_VERSION
from .config import Config
from .core import Core, QueueFull, error_body
from .jsonutil import StrictJSONError, canonical, strict_loads
from .models import UUID_RE, Envelope, error_details

JSON_TYPE = "application/json; charset=utf-8"


def _http_error(status: int, request_id: str | None, code: str, message: str,
                details: dict[str, Any] | None = None, retryable: bool = False,
                headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse(error_body(request_id, code, message, details, retryable),
                        status_code=status, headers=headers)


def _recover_request_id(obj: Any) -> str | None:
    if isinstance(obj, dict):
        rid = obj.get("request_id")
        if isinstance(rid, str) and UUID_RE.match(rid):
            return rid
    return None


def create_app(config: Config) -> FastAPI:
    core = Core(config)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await core.start()
        try:
            yield
        finally:
            await core.stop()

    app = FastAPI(title="Score IO Service", version=SERVICE_VERSION, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.core = core

    if config.allowed_origins:  # CORS は既定で無効。config.json の allowed_origins で明示許可する。
        from fastapi.middleware.cors import CORSMiddleware

        app.add_middleware(CORSMiddleware, allow_origins=config.allowed_origins,
                           allow_methods=["POST", "GET"], allow_headers=["Authorization", "Content-Type"])

    expected_auth = ("Bearer " + config.token).encode("utf-8")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v2/rpc")
    async def rpc(request: Request) -> Response:
        auth = request.headers.get("authorization", "").encode("utf-8", errors="replace")
        if not hmac.compare_digest(auth, expected_auth):
            return _http_error(401, None, "UNAUTHORIZED", "認証に失敗しました。",
                               headers={"WWW-Authenticate": "Bearer"})

        limit = config.limits.request_bytes
        declared = request.headers.get("content-length")
        too_large = {"limit": limit, "unit": "bytes"}
        if declared is not None and declared.isdigit() and int(declared) > limit:
            return _http_error(413, None, "LIMIT_EXCEEDED", "要求本文が大きすぎます。", too_large)
        chunks: list[bytes] = []
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > limit:
                return _http_error(413, None, "LIMIT_EXCEEDED", "要求本文が大きすぎます。", too_large)
            chunks.append(chunk)

        try:
            obj = strict_loads(b"".join(chunks))
            canonical(obj).encode("utf-8")  # 孤立サロゲート等、UTF-8 にできない文字列を拒否
        except (StrictJSONError, UnicodeEncodeError, ValueError) as e:
            return _http_error(400, None, "INVALID_REQUEST", "JSON として解釈できません。", {"reason": str(e)[:200]})
        try:
            Envelope.model_validate(obj)
        except ValidationError as e:
            return _http_error(400, _recover_request_id(obj), "INVALID_REQUEST", "要求の外枠が不正です。",
                               error_details(e))

        try:
            body = await core.handle(obj)
        except QueueFull:
            return _http_error(503, obj["request_id"], "QUEUE_FULL",
                               "待機中の要求が多すぎます。要求は受け付けられず、実行されていません。",
                               {"limit": config.limits.max_waiting}, retryable=True)
        return Response(content=body, media_type=JSON_TYPE)

    return app
