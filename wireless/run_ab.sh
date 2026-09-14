#!/usr/bin/env bash
# run_ab.sh <vision-model-id> — one condition of the ARTEMIS vision-model A/B.
#
# Standardises everything except the model:
#   * regenerates + activates the OpenCode Go config with ARTEMIS_VISION_MODEL
#   * force-stops the target app and returns to the home screen
#   * runs the SAME prompt as the original Pokemon GO run
#   * records steps, wall-clock, tokens and outcome to ab-results.jsonl
#
# Usage: ./run_ab.sh glm-5.3-flash
set -uo pipefail

MODEL="${1:?usage: run_ab.sh <vision-model-id>}"
REPO="$HOME/projects/artemis"
HERE="$HOME/projects/artemis-wireless"
APP="com.nianticlabs.pokemongo"
export PATH="$HOME/.local/bin:$PATH"

# The wireless transport can drop (and its adb serial changes shape: ip:port for
# an explicit connect, or the mDNS service name after auto-reconnect). Detect it
# rather than hardcoding, and reconnect from mDNS if it is missing.
detect_serial() {
  adb devices | awk 'NR>1 && $2=="device" && $1 !~ /^emulator/ {print $1; exit}'
}
SERIAL="${ARTEMIS_SERIAL:-$(detect_serial)}"
if [ -z "$SERIAL" ]; then
  EP=$(timeout 8 adb mdns services | awk '/_adb-tls-connect/{print $3; exit}')
  if [ -n "$EP" ]; then
    echo "  device missing from adb; reconnecting to $EP"
    adb connect "$EP" >/dev/null
    sleep 2
    SERIAL="$(detect_serial)"
  fi
fi
[ -n "$SERIAL" ] || { echo "no device available — aborting"; exit 1; }
echo "  serial: $SERIAL"
PROMPT="Open the Pokemon GO app and wait for it to finish loading. Dismiss any popups or announcements by tapping the X or OK button. Then open the Pokemon collection by tapping the Poke Ball icon at the bottom of the screen. The list is sorted by Recent, so the Pokemon shown in the top-left corner of the grid is the most recently caught one. Tell me the name and the CP of that Pokemon, and quote the exact text you see on its list entry."
export PATH="$HOME/.local/bin:$PATH"

LOG="$HERE/ab-${MODEL}.log"
echo "=== condition: $MODEL ==="

ARTEMIS_VISION_MODEL="$MODEL" "$REPO/.venv/bin/python" "$HERE/build_opencode_go_config.py" | sed 's/^/  /'
"$HERE/switch-llm-backend.sh" opencode-go >/dev/null
grep -m1 '"model"' "$REPO/config/artemis.jsonc" | sed 's/^/  active: /'

# Identical starting conditions for both arms.
adb -s "$SERIAL" shell am force-stop "$APP" >/dev/null 2>&1
adb -s "$SERIAL" shell input keyevent KEYCODE_HOME >/dev/null 2>&1
sleep 3
echo "  device reset: app force-stopped, home screen"

START=$(date +%s)
cd "$REPO" || exit 1
timeout 1500 .venv/bin/artemis run "$PROMPT" --profile flash -s "$SERIAL" --standalone > "$LOG" 2>&1
RC=$?
END=$(date +%s)
WALL=$((END - START))

STEPS=$(grep -c "Recorded step" "$LOG")
USAGE=$(grep -o "LLM usage for session.*" "$LOG" | tail -1)
CALLS=$(grep -o "[0-9]* calls" <<< "$USAGE" | head -1 | awk '{print $1}')
PROMPT_TOK=$(grep -o "[0-9]* prompt tokens" <<< "$USAGE" | head -1 | awk '{print $1}')
CACHED_TOK=$(grep -o "[0-9]* cached" <<< "$USAGE" | head -1 | awk '{print $1}')
CACHE_RATIO=$(grep -o "cached_ratio=[0-9.]*" <<< "$USAGE" | head -1 | cut -d= -f2)
SESSION=$(grep -o "Session ended: [a-f0-9-]* with status: [a-z]*" "$LOG" | tail -1 | awk '{print $3}')
STATUS=$(grep -o "Session ended: [a-f0-9-]* with status: [a-z]*" "$LOG" | tail -1 | awk '{print $NF}')
ANSWER=$(grep -o "report_task_status.*" "$LOG" | tail -1 | cut -c1-400)

echo "  wall clock : ${WALL}s"
echo "  steps      : $STEPS"
echo "  status     : $STATUS (rc=$RC)"
echo "  calls      : $CALLS  prompt=$PROMPT_TOK cached=$CACHED_TOK"
echo "  answer     : $ANSWER"

python3 - "$HERE/ab-results.jsonl" "$MODEL" "$WALL" "$STEPS" "$STATUS" "$CALLS" "$PROMPT_TOK" "$CACHED_TOK" "$SESSION" "$ANSWER" <<'PY'
import json, sys, time
path, model, wall, steps, status, calls, ptok, ctok, session, answer = sys.argv[1:11]
rec = {
    "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
    "model": model,
    "wall_clock_s": int(wall),
    "steps": int(steps),
    "status": status,
    "llm_calls": int(calls) if calls.isdigit() else None,
    "prompt_tokens": int(ptok) if ptok.isdigit() else None,
    "cached_tokens": int(ctok) if ctok.isdigit() else None,
    "session": session,
    "answer": answer,
}
with open(path, "a", encoding="utf-8") as fh:
    fh.write(json.dumps(rec) + "\n")
print(f"  recorded -> {path}")
PY
