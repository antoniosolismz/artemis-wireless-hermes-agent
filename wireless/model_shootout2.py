#!/usr/bin/env python3
"""Shootout v2 — coordinate-space safe.

v1 asked for raw pixel coordinates on a 1080x2400 image and every model "missed"
by 450-1260px, including the model that had just hit a target 1px off in an
800x600 probe and driven 22 real taps on a device. That is the signature of a
scale mismatch: providers downscale large images, so the model answers in ITS
coordinate space, and ARTEMIS maps that back itself.

v2 therefore measures what actually matters, robustly:
  * normalised taps (0-100% of width/height)  -> the usable metric
  * the implied scale factor of pixel answers -> explains v1
Answers are judged on whether the point lands inside the known target box.

Usage: .venv/bin/python model_shootout2.py [model ...]
"""

from __future__ import annotations

import base64
import io
import json
import os
import random
import statistics
import sys
import time
import uuid
from pathlib import Path

import requests
from PIL import Image, ImageDraw

BASE = os.environ.get("ZEN_BASE_URL", "http://127.0.0.1:8787/v1")
KEY = os.environ.get("OPENCODE_GO_API_KEY", "bridge-supplies-key")
OUT = Path.home() / "projects" / "artemis-wireless" / "model-shootout-v2.json"
SESSION = f"shootout2-{uuid.uuid4().hex[:8]}"

MODELS = [
    "deepseek-v4-flash-vision-exp",
    "mimo-v2.5",
    "qwen3.8-flash",
    "glm-5.3-flash",
]

W, H = 1080, 2400
LABELS = ["BATTERY 42%", "INBOX 17", "WIFI ON", "STEP 3 OF 8"]


def make_screen(seed: int):
    img = Image.new("RGB", (W, H), (18, 18, 22))
    d = ImageDraw.Draw(img)
    rng = random.Random(seed)
    label = LABELS[seed % len(LABELS)]
    d.rectangle([0, 0, W, 120], fill=(38, 38, 46))
    d.text((40, 50), label, fill=(235, 235, 235))
    for i in range(6):
        y = 300 + i * 190
        d.rounded_rectangle([60, y, W - 60, y + 140], radius=24, fill=(44, 44, 54))
        d.text((110, y + 55), f"Row {i + 1}", fill=(200, 200, 210))
    bx1 = rng.randint(120, 620)
    by1 = rng.randint(1500, 2100)
    box = (bx1, by1, bx1 + 380, by1 + 150)
    d.rounded_rectangle(box, radius=28, fill=(30, 120, 240))
    d.text((box[0] + 90, box[1] + 62), "CONTINUE", fill=(255, 255, 255))
    return img, box, label


def ask(model: str, image_b64: str, prompt: str):
    t0 = time.time()
    r = requests.post(
        f"{BASE}/chat/completions",
        headers={
            "Authorization": f"Bearer {KEY}",
            "Content-Type": "application/json",
            "x-opencode-session": SESSION,
            "User-Agent": "artemis-vision-shootout/2.0",
        },
        json={
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_b64}"}},
                    ],
                }
            ],
        },
        timeout=240,
    )
    dt = time.time() - t0
    try:
        body = r.json()
    except ValueError:
        return f"HTTP {r.status_code}", {}, dt
    if "choices" not in body:
        return f"ERROR {r.status_code}: {json.dumps(body)[:160]}", {}, dt
    return body["choices"][0]["message"].get("content") or "", body.get("usage", {}), dt


def png_b64(img):
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def parse_json(text: str):
    cleaned = (text or "").strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        return json.loads(cleaned[start : end + 1])
    except ValueError:
        return None


def main() -> int:
    models = sys.argv[1:] or MODELS
    results = []
    layouts = [make_screen(i) for i in range(4)]

    for model in models:
        print(f"\n{'=' * 74}\n{model}\n{'=' * 74}")
        ocr = norm_hits = 0
        norm_errs, ratios, lat, tins, touts = [], [], [], 0, 0
        notes = []

        for idx, (img, box, label) in enumerate(layouts):
            b64 = png_b64(img)
            cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
            fit_pct, fiy_pct = cx / W * 100, cy / H * 100

            text, usage, dt = ask(model, b64, "Read the text in the top status bar of this phone screenshot. Reply with only that text.")
            lat.append(dt); tins += usage.get("prompt_tokens") or 0; touts += usage.get("completion_tokens") or 0
            if label.split()[0].lower() in (text or "").lower():
                ocr += 1

            # normalised question (the usable convention)
            text, usage, dt = ask(
                model, b64,
                'Where is the blue CONTINUE button? Answer with ONLY JSON {"x_pct":<0-100>,"y_pct":<0-100>} '
                "giving the centre of that button as a percentage of the image width and height.",
            )
            lat.append(dt); tins += usage.get("prompt_tokens") or 0; touts += usage.get("completion_tokens") or 0
            pt = parse_json(text) or {}
            xp, yp = pt.get("x_pct"), pt.get("y_pct")
            hit = False
            if isinstance(xp, (int, float)) and isinstance(yp, (int, float)):
                x, y = float(xp) / 100 * W, float(yp) / 100 * H
                hit = box[0] <= x <= box[2] and box[1] <= y <= box[3]
                norm_errs.append(((x - cx) ** 2 + (y - cy) ** 2) ** 0.5)
            norm_hits += hit

            # pixel question (to expose the scale factor)
            text, usage, dt = ask(
                model, b64,
                'Where is the blue CONTINUE button? Answer with ONLY JSON {"x":int,"y":int} in image pixel coordinates.',
            )
            lat.append(dt); tins += usage.get("prompt_tokens") or 0; touts += usage.get("completion_tokens") or 0
            pt = parse_json(text) or {}
            if isinstance(pt.get("y"), (int, float)) and cy:
                ratios.append(float(pt["y"]) / cy)

            print(f"  layout{idx}: ocr={'ok' if label.split()[0].lower() in ('ok') else '?'} norm={'HIT' if hit else 'miss'} "
                  f"err={round(norm_errs[-1]) if norm_errs else None}px pix_ratio={round(ratios[-1], 2) if ratios else None}")

        rec = {
            "model": model,
            "ocr_rate": round(ocr / len(layouts), 3),
            "normalised_tap_hit_rate": round(norm_hits / len(layouts), 3),
            "mean_norm_error_px": round(statistics.mean(norm_errs), 1) if norm_errs else None,
            "median_pixel_answer_scale": round(statistics.median(ratios), 3) if ratios else None,
            "mean_latency_s": round(statistics.mean(lat), 2),
            "prompt_tokens": tins,
            "completion_tokens": touts,
            "notes": notes,
        }
        results.append(rec)
        print(f"  -> OCR {ocr}/4  normalised tap {norm_hits}/4  mean err {rec['mean_norm_error_px']}px  "
              f"pixel-answer scale x{rec['median_pixel_answer_scale']}  {rec['mean_latency_s']}s")

    prev = json.loads(OUT.read_text()) if OUT.exists() else []
    OUT.write_text(json.dumps(prev + results, indent=2) + "\n")
    print(f"\nappended to {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
