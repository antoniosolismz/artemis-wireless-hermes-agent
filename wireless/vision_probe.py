#!/usr/bin/env python3
"""Prove (or disprove) that the OpenCode Go vision model can drive ARTEMIS.

ARTEMIS's loop is screenshot -> find target -> tap coordinates. So we test the
exact capability it needs: read a screen image and return usable coordinates.
Ground truth is a synthetic image we generate, so a hallucinated answer is
detectable.

Usage: OPENCODE_GO_API_KEY=... .venv/bin/python vision_probe.py
"""

from __future__ import annotations

import base64
import io
import json
import os
import sys
import time
import uuid

import requests
from PIL import Image, ImageDraw

BASE_URL = os.environ.get("ZEN_BASE_URL", "https://opencode.ai/zen/go/v1")
MODEL = os.environ.get("ZEN_MODEL", "deepseek-v4-flash-vision-exp")
KEY = os.environ.get("OPENCODE_GO_API_KEY") or os.environ.get("OPENCODE_API_KEY") or ""

# ---------------------------------------------------------------- test image
# 800x600 canvas: a blue button and a red circle at known coordinates, plus
# label text. Any correct answer must match these numbers.
W, H = 800, 600
BLUE_BOX = (470, 120, 700, 250)      # x1, y1, x2, y2
RED_CENTER = (220, 430)
RED_R = 60
LABEL_TEXT = "BATTERY 42%"


def build_image() -> Image.Image:
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W - 1, 90], fill=(240, 240, 245))
    d.text((30, 35), LABEL_TEXT, fill=(20, 20, 20))
    d.rectangle(BLUE_BOX, fill=(30, 90, 220))
    d.text((BLUE_BOX[0] + 40, BLUE_BOX[1] + 55), "CONTINUE", fill="white")
    d.ellipse(
        [RED_CENTER[0] - RED_R, RED_CENTER[1] - RED_R, RED_CENTER[0] + RED_R, RED_CENTER[1] + RED_R],
        fill=(220, 40, 40),
    )
    return img


def png_b64(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def ask(question: str, image_b64: str, model: str = MODEL) -> tuple[str, dict]:
    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": question},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{image_b64}"},
                    },
                ],
            }
        ],
    }
    resp = requests.post(
        f"{BASE_URL}/chat/completions",
        headers={
            "Authorization": f"Bearer {KEY}",
            "Content-Type": "application/json",
            "x-opencode-session": f"hermes-artemis-probe-{uuid.uuid4()}",
            "User-Agent": "artemis-vision-probe/1.0",
        },
        json=payload,
        timeout=180,
    )
    body = resp.json()
    if resp.status_code != 200 or "choices" not in body:
        return f"HTTP {resp.status_code}: {json.dumps(body)[:400]}", {}
    choice = body["choices"][0]["message"]
    return (choice.get("content") or ""), body.get("usage", {})


def main() -> int:
    if not KEY:
        print("no API key in env")
        return 2

    img = build_image()
    b64 = png_b64(img)
    img.save("%h/projects/artemis-wireless/vision-probe-image.png")
    print(f"test image: {W}x{H}, blue box {BLUE_BOX}, red circle centre {RED_CENTER}, "
          f"header text {LABEL_TEXT!r}")
    print(f"model: {MODEL}  endpoint: {BASE_URL}\n")

    failures = 0

    # 1. Plain vision: can it read the screen at all?
    print("=" * 70)
    print("1. read the screen (OCR)")
    print("=" * 70)
    t0 = time.time()
    content, usage = ask(
        "Look at this phone screenshot. List the exact text visible in the top bar "
        "and the text on the blue button. Answer in one line.",
        b64,
    )
    print(f"[{time.time() - t0:.1f}s] {content[:300]}")
    print(f"usage: {usage}")
    if LABEL_TEXT.split()[0].lower() not in content.lower():
        print("  -> FAIL: header text not read")
        failures += 1
    else:
        print("  -> PASS: header text read correctly")

    # 2. Spatial grounding: the capability ARTEMIS's Flash explorer needs.
    print()
    print("=" * 70)
    print("2. spatial grounding (tap coordinate)")
    print("=" * 70)
    content, usage = ask(
        "This is a phone screenshot. I want to tap the blue CONTINUE button. "
        "Give the tap point as JSON exactly like {\"x\": 123, \"y\": 456} where x and y "
        "are pixel coordinates in this image. Output ONLY that JSON.",
        b64,
    )
    print(f"answer: {content[:300]}")
    print(f"usage: {usage}")
    cx, cy = (BLUE_BOX[0] + BLUE_BOX[2]) / 2, (BLUE_BOX[1] + BLUE_BOX[3]) / 2
    hit = False
    try:
        cleaned = content.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        pt = json.loads(cleaned)
        x, y = float(pt["x"]), float(pt["y"])
        inside = BLUE_BOX[0] <= x <= BLUE_BOX[2] and BLUE_BOX[1] <= y <= BLUE_BOX[3]
        err = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5
        print(f"  parsed ({x:.0f},{y:.0f}) | box centre ({cx:.0f},{cy:.0f}) | error {err:.0f}px | inside={inside}")
        hit = inside
    except Exception as exc:  # noqa: BLE001
        print(f"  could not parse coordinates: {exc}")
    if hit:
        print("  -> PASS: coordinate lands inside the target widget")
    else:
        print("  -> FAIL: coordinate does not land on the target")
        failures += 1

    print()
    print("=" * 70)
    print("RESULT:", "vision capable" if failures == 0 else f"{failures} check(s) failed")
    print("=" * 70)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
