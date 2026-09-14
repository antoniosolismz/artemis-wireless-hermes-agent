#!/usr/bin/env python3
"""OpenCode Go (Zen) bridge for ARTEMIS.

Why this exists: ARTEMIS talks to any OpenAI-compatible endpoint via
`OPENAI_BASE_URL`, but OpenCode Go requires an `x-opencode-session` routing
header that no ARTEMIS code path can send, and it wants a client user-agent.
This is a ~1-file local shim: it accepts the OpenAI-shaped requests ARTEMIS
makes, injects the Go headers, and forwards them to the Go tier endpoint.

    ARTEMIS ──OpenAI API──▶ 127.0.0.1:8787 ──+headers──▶ opencode.ai/zen/go/v1

Config it expects (env):
  OPENCODE_GO_API_KEY   Go subscription key (required)
  ZEN_BASE_URL          default https://opencode.ai/zen/go/v1
  ZEN_DEFAULT_MODEL     fallback when a requested model is not a Go model id
  ZEN_PORT              default 8787
  ZEN_LOG               default ~/.config/artemis-wireless/zen-bridge.log

Run:  uvicorn --app-dir . zen_go_proxy:app --host 127.0.0.1 --port 8787
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

ZEN_BASE_URL = os.environ.get("ZEN_BASE_URL", "https://opencode.ai/zen/go/v1").rstrip("/")
ZEN_DEFAULT_MODEL = os.environ.get("ZEN_DEFAULT_MODEL", "deepseek-v4-flash-vision-exp")
ZEN_PORT = int(os.environ.get("ZEN_PORT", "8787"))
ZEN_LOG = Path(
    os.environ.get("ZEN_LOG", str(Path.home() / ".config" / "artemis-wireless" / "zen-bridge.log"))
)
ZEN_KEY = os.environ.get("OPENCODE_GO_API_KEY") or os.environ.get("OPENCODE_API_KEY") or ""

# One stable session id per process: the Go docs ask for a stable session per
# conversation so routing and prompt caching work. Overridable per request via
# an incoming `x-session-id` header.
SESSION_ID = os.environ.get("ZEN_SESSION_ID") or f"hermes-artemis-{uuid.uuid4().hex[:12]}"

# Models the Go tier serves (from `opencode models | grep opencode-go` and the
# Go docs). Anything else is remapped so a leftover gemini-* name still works.
GO_MODELS = {
    "deepseek-v4-flash",
    "deepseek-v4-flash-vision-exp",
    "deepseek-v4-pro",
    "glm-5.1", "glm-5.2", "glm-5.3",
    "gpt-5.6-luna",
    "grok-4.5",
    "hy3", "hy4-preview",
    "kimi-k2.6", "kimi-k2.7-code", "kimi-k3",
    "longcat-2.0",
    "mimo-v2.5", "mimo-v2.5-pro",
    "minimax-m2.5", "minimax-m2.7", "minimax-m3",
    "muse-spark-1.2-contributor", "muse-spark-1.3-contributor",
    "qwen3.6-plus", "qwen3.7-max", "qwen3.7-plus", "qwen3.8-max", "qwen3.8-flash",
}

app = FastAPI(title="ARTEMIS ⟷ OpenCode Go bridge")


def _log(event: str, **fields: Any) -> None:
    try:
        ZEN_LOG.parent.mkdir(parents=True, exist_ok=True)
        record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "event": event, **fields}
        with ZEN_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except OSError:
        pass


def _resolve_model(requested: str | None) -> tuple[str, bool]:
    """Map a requested model id onto a Go model id. Returns (model, remapped)."""
    if not requested:
        return ZEN_DEFAULT_MODEL, True
    clean = requested.split("/")[-1].strip()
    if clean in GO_MODELS:
        return clean, False
    return ZEN_DEFAULT_MODEL, True


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "ok": bool(ZEN_KEY),
        "upstream": ZEN_BASE_URL,
        "default_model": ZEN_DEFAULT_MODEL,
        "session": SESSION_ID,
    }


@app.get("/v1/models")
async def models() -> dict[str, Any]:
    return {
        "object": "list",
        "data": [{"id": m, "object": "model", "owned_by": "opencode-go"} for m in sorted(GO_MODELS)],
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: Request) -> Response:
    if not ZEN_KEY:
        return JSONResponse({"error": "OPENCODE_GO_API_KEY is not set for the bridge"}, status_code=500)

    try:
        payload = await request.json()
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": f"invalid JSON body: {exc}"}, status_code=400)

    requested = payload.get("model")
    model, remapped = _resolve_model(requested)
    payload["model"] = model

    # Params the Go tier rejects or ignores; drop them rather than 400.
    for unsupported in ("thinking", "thinking_budget", "thinking_level", "include_thoughts"):
        payload.pop(unsupported, None)

    headers = {
        "Authorization": f"Bearer {ZEN_KEY}",
        "Content-Type": "application/json",
        # The Go docs ask each client to identify itself with its own user agent.
        "User-Agent": "hermes-artemis-bridge/1.0",
        # Routing/prompt-cache session (the reason this bridge exists).
        "x-opencode-session": request.headers.get("x-session-id") or SESSION_ID,
        "Accept": "application/json",
    }

    images = 0
    for message in payload.get("messages", []) or []:
        content = message.get("content")
        if isinstance(content, list):
            images += sum(1 for part in content if part.get("type") == "image_url")

    started = time.time()
    stream = bool(payload.get("stream"))

    if not stream:
        async with httpx.AsyncClient(timeout=600.0) as client:
            try:
                upstream = await client.post(
                    f"{ZEN_BASE_URL}/chat/completions", headers=headers, json=payload
                )
            except httpx.HTTPError as exc:
                _log("error", model=model, error=str(exc))
                return JSONResponse({"error": f"upstream error: {exc}"}, status_code=502)
        elapsed = round(time.time() - started, 2)
        ok = upstream.status_code == 200
        usage: dict[str, Any] = {}
        if ok:
            try:
                usage = upstream.json().get("usage", {}) or {}
            except ValueError:
                usage = {}
        _log(
            "chat",
            model=model,
            requested=requested,
            remapped=remapped,
            images=images,
            status=upstream.status_code,
            seconds=elapsed,
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
        )
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type", "application/json"),
        )

    # Streaming passthrough (the ARTEMIS agent loop streams tokens).
    async def _stream() -> Any:
        async with httpx.AsyncClient(timeout=600.0) as client:
            async with client.stream(
                "POST", f"{ZEN_BASE_URL}/chat/completions", headers=headers, json=payload
            ) as upstream:
                _log("chat_stream", model=model, requested=requested, images=images, status=upstream.status_code)
                async for chunk in upstream.aiter_raw():
                    yield chunk

    return StreamingResponse(_stream(), media_type="text/event-stream")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=ZEN_PORT, log_level="info")
