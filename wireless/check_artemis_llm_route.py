#!/usr/bin/env python3
"""Integration test: ARTEMIS' real LLM path -> local bridge -> OpenCode Go.

This does NOT hand-roll HTTP like vision_probe.py. It uses ARTEMIS' own config
loader (`get_default_llm_config`) and model factory (`ModelFactory.create_model`),
so it proves the thing that actually matters: that ARTEMIS' agent nodes will
talk to the Go subscription when `switch-llm-backend.sh opencode-go` is active.

Verifies: active config resolves provider=openai + a Go model id, the factory
builds a live client, an image round-trips, and tool calling works (ARTEMIS
acts through tool calls).

Run:  ~/projects/artemis/.venv/bin/python check_artemis_llm_route.py
"""

from __future__ import annotations

import base64
import io
import sys
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage

from artemis.config.llm import get_default_llm_config
from artemis.llm.router import ModelFactory, ModelProvider
from artemis.services.llm import _resolve_endpoint

sys.path.insert(0, str(Path.home() / "projects" / "artemis-wireless"))

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f" -> {detail}" if detail else ""))
    if not ok:
        failures.append(label)


class _Ctx:
    """Minimal stand-in: _resolve_endpoint only reads ctx.llm_config."""

    def __init__(self) -> None:
        self.llm_config = get_default_llm_config()


ctx = _Ctx()

print("=" * 72)
print("1. ARTEMIS config resolves to the Go route")
print("=" * 72)
endpoint = _resolve_endpoint(ctx, "operator")
print(f"  operator endpoint -> provider={endpoint.provider.value} model={endpoint.model_name}")
check("provider is openai (bridge-compatible)", endpoint.provider == ModelProvider.OPENAI,
      endpoint.provider.value)
check("model is an OpenCode Go id", "gemini" not in endpoint.model_name,
      endpoint.model_name)

summary = _resolve_endpoint(ctx, "summarizer")
print(f"  summarizer endpoint -> provider={summary.provider.value} model={summary.model_name}")

print()
print("=" * 72)
print("2. ARTEMIS' model factory builds a live client")
print("=" * 72)
model = ModelFactory.create_model(endpoint)
check("client constructed", model is not None, type(model).__name__)
base_url = getattr(model, "openai_api_base", None) or getattr(model, "base_url", None)
print(f"  base_url = {base_url}")
check("client points at the local bridge", bool(base_url) and "127.0.0.1" in str(base_url), str(base_url))

print()
print("=" * 72)
print("3. A screenshot round-trips through ARTEMIS' client")
print("=" * 72)
img = Path.home() / "projects" / "artemis-wireless" / "vision-probe-image.png"
if img.exists():
    b64 = base64.b64encode(img.read_bytes()).decode()
else:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (400, 300), "white").save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode()

message = HumanMessage(
    content=[
        {"type": "text", "text": "What text is written in the top bar of this screenshot? One line."},
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
    ]
)
try:
    reply = model.invoke([message])
    text = reply.content if isinstance(reply, AIMessage) else str(reply)
    print(f"  model said: {text[:200]}")
    check("vision reply received", "BATTERY" in text.upper() or "42" in text, text[:80])
except Exception as exc:  # noqa: BLE001
    check("vision reply received", False, f"{type(exc).__name__}: {exc}")

print()
print("=" * 72)
print("4. Tool calling (how ARTEMIS performs actions)")
print("=" * 72)
tools = [
    {
        "type": "function",
        "function": {
            "name": "tap",
            "description": "Tap the screen at pixel coordinates",
            "parameters": {
                "type": "object",
                "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}},
                "required": ["x", "y"],
            },
        },
    }
]
try:
    bound = model.bind_tools(tools)
    reply = bound.invoke(
        [
            HumanMessage(
                content=[
                    {
                        "type": "text",
                        "text": "Tap the blue CONTINUE button in this screenshot. Use the tap tool.",
                    },
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                ]
            )
        ]
    )
    calls = getattr(reply, "tool_calls", None) or []
    print(f"  tool_calls: {calls}")
    check("model emitted a tool call", len(calls) > 0, f"{len(calls)} call(s)")
    if calls:
        args = calls[0].get("args", {})
        check("tool call targets the tap tool", calls[0].get("name") == "tap", str(calls[0].get("name")))
        x, y = int(args.get("x", -1)), int(args.get("y", -1))
        inside = 470 <= x <= 700 and 120 <= y <= 250
        print(f"  tapped ({x},{y}); target box is x:470-700 y:120-250 -> inside={inside}")
        check("coordinates land on the button", inside, f"({x},{y})")
except Exception as exc:  # noqa: BLE001
    check("model emitted a tool call", False, f"{type(exc).__name__}: {exc}")

print()
print("=" * 72)
if failures:
    print(f"RESULT: {len(failures)} FAILED -> {failures}")
else:
    print("RESULT: ARTEMIS drives the OpenCode Go subscription end to end")
print("=" * 72)
sys.exit(1 if failures else 0)
