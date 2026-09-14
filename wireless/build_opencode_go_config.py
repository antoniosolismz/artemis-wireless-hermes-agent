#!/usr/bin/env python3
"""Generate the OpenCode-Go variant of ARTEMIS's artemis.jsonc.

ARTEMIS routes every agent node through a (provider, model) pair. The factory
config points all of them at Google/Gemini, so an OpenCode Go user needs the
same graph with:

  * provider "google" -> "openai"   (the bridge speaks the OpenAI protocol)
  * gemini model ids  -> OpenCode Go model ids
  * vision nodes      -> the Go vision model (screenshots)
  * text-only nodes   -> a cheap Go text model (summaries, extraction)
  * video analysis    -> off (Go serves images; scrcpy is not installed)

Writes config/artemis.opencode-go.jsonc next to the original. Nothing is
overwritten: the switcher script decides which file is active.

Usage: .venv/bin/python build_opencode_go_config.py [--write]
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO = Path.home() / "projects" / "artemis"
CONFIG_DIR = REPO / "config"
# Always generate FROM the pristine Gemini config: reading the live
# config/artemis.jsonc would re-convert an already-converted file (counts go to
# zero and drift creeps in). The switcher keeps this backup.
SRC = CONFIG_DIR / "artemis.gemini.jsonc"
if not SRC.exists():
    SRC = CONFIG_DIR / "artemis.jsonc"
DST = CONFIG_DIR / "artemis.opencode-go.jsonc"

VISION_MODEL = os.environ.get("ARTEMIS_VISION_MODEL", "deepseek-v4-flash-vision-exp")
TEXT_MODEL = os.environ.get("ARTEMIS_TEXT_MODEL", "mimo-v2.5")  # cheap Go tier: $0.14/$0.28 per 1M
VISION = {"gemini-3.8-flash", "gemini-3.7-flash", "gemini-2.5-flash", "gemini-2.0-flash",
          "gemini-robotics-er-2-preview", "gemini-3.6-flash", "gemini-3.5-flash"}
TEXT = {"gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-3-flash-lite"}


def strip_jsonc(raw: str) -> str:
    """Remove // and /* */ comments plus trailing commas (JSONC -> JSON)."""
    out: list[str] = []
    i, n = 0, len(raw)
    in_string = False
    while i < n:
        ch = raw[i]
        if in_string:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(raw[i + 1])
                i += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue
        if raw.startswith("//", i):
            while i < n and raw[i] != "\n":
                i += 1
            continue
        if raw.startswith("/*", i):
            i = raw.find("*/", i + 2)
            i = n if i == -1 else i + 2
            continue
        out.append(ch)
        i += 1
    text = "".join(out)
    # strip trailing commas before } or ]
    cleaned, i = [], 0
    while i < len(text):
        if text[i] == ",":
            j = i + 1
            while j < len(text) and text[j] in " \t\r\n":
                j += 1
            if j < len(text) and text[j] in "}]":
                i += 1
                continue
        cleaned.append(text[i])
        i += 1
    return "".join(cleaned)


def convert(node: object, stats: dict[str, int]) -> object:
    """Recursively rewrite provider/model pairs and disable video analysis."""
    if isinstance(node, list):
        return [convert(item, stats) for item in node]
    if not isinstance(node, dict):
        return node

    out: dict[str, object] = {}
    for key, value in node.items():
        if key == "provider" and value in ("google", "gemini"):
            out[key] = "openai"
            stats["providers"] += 1
            continue
        if key == "model" and isinstance(value, str):
            if value in VISION:
                out[key] = VISION_MODEL
                stats["vision"] += 1
            elif value in TEXT:
                out[key] = TEXT_MODEL
                stats["text"] += 1
            else:
                out[key] = value
            continue
        if key == "enabled" and value is True and "video_analyzer" in str(stats.get("_path", "")):
            out[key] = False
            stats["video_disabled"] += 1
            continue
        out[key] = convert(value, stats)

    # Nodes that name a model without naming a provider inherit the *default*
    # provider (Google) at runtime — which is how `step_summarizer` and
    # `memory.chunking` broke on the first real run. Make it explicit.
    if "model" in out and "provider" not in out:
        out["provider"] = "openai"
        stats["implicit_provider"] += 1
    return out


def main() -> int:
    raw = SRC.read_text(encoding="utf-8")
    data = json.loads(strip_jsonc(raw))
    stats = {"providers": 0, "vision": 0, "text": 0, "video_disabled": 0, "implicit_provider": 0}

    converted = convert(data, stats)

    # Turn off screen-recording analysis in both profiles: Go serves images,
    # and scrcpy is not installed on this host.
    for scope in ("agent",):
        va = converted.get(scope, {}).get("video_analyzer") if isinstance(converted.get(scope), dict) else None
        if isinstance(va, dict):
            va["enabled"] = False
            stats["video_disabled"] += 1
        pro = converted.get(scope, {}).get("pro") if isinstance(converted.get(scope), dict) else None
        if isinstance(pro, dict) and isinstance(pro.get("video_analyzer"), dict):
            pro["video_analyzer"]["enabled"] = False
            stats["video_disabled"] += 1

    # Make the default explicit: Go vision model for the main loop.
    default = converted.setdefault("default", {})
    default["provider"] = "openai"
    default["model"] = VISION_MODEL
    default["fallback"] = {"provider": "openai", "model": VISION_MODEL}

    header = (
        "// GENERATED by ~/projects/artemis-wireless/build_opencode_go_config.py\n"
        "// Variant of artemis.jsonc that routes every agent node through the local\n"
        "// OpenCode Go bridge (zen_go_proxy.py). Activate/deactivate with\n"
        "// ~/projects/artemis-wireless/switch-llm-backend.sh {opencode-go|gemini}\n"
    )
    DST.write_text(header + json.dumps(converted, indent=2) + "\n", encoding="utf-8")

    print(f"wrote {DST}")
    print(f"  providers rewritten : {stats['providers']}")
    print(f"  gemini->vision      : {stats['vision']}  ({VISION_MODEL})")
    print(f"  gemini->text        : {stats['text']}  ({TEXT_MODEL})")
    print(f"  video analysis off  : {stats['video_disabled']}")
    print(f"  implicit providers  : {stats['implicit_provider']}  (model named without provider)")

    # Sanity: the file must re-parse and every node must be openai.
    reparsed = json.loads(strip_jsonc(DST.read_text(encoding="utf-8")))
    leftovers = []

    def walk(node: object, path: str = "") -> None:
        if isinstance(node, dict):
            if node.get("provider") not in (None, "openai"):
                leftovers.append(f"{path}.provider={node.get('provider')}")
            # A model with no provider inherits Google at runtime — the bug that
            # broke the first real run via step_summarizer / memory.chunking.
            if "model" in node and "provider" not in node:
                leftovers.append(f"{path}: model={node['model']} has no provider")
            if isinstance(node.get("model"), str) and node["model"].startswith("gemini"):
                leftovers.append(f"{path}.model={node['model']}")
            for k, v in node.items():
                walk(v, f"{path}.{k}")
        elif isinstance(node, list):
            for idx, v in enumerate(node):
                walk(v, f"{path}[{idx}]")

    walk(reparsed)
    if leftovers:
        print("REMAINING GEMINI REFERENCES:")
        for line in leftovers:
            print("  ", line)
        return 1
    print("  verification: no gemini providers or model ids remain")
    return 0


if __name__ == "__main__":
    sys.exit(main())
