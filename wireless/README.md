# Cable-free ARTEMIS: driving an Android phone over Wi-Fi

This directory holds everything needed to run [Google's ARTEMIS](https://github.com/google/artemis)
against a real Android device **with no USB cable**, including the parts that
ARTEMIS itself does not provide yet.

ARTEMIS is transport-agnostic: nothing in it filters on USB, and
`adb connect <ip>:<port>` is enough for a device to become a normal task target
(its serial is literally `192.168.1.50:5555`). The quick start in the upstream
README only *asks* for USB, it does not require it. What is missing is the
surrounding plumbing — pairing, mDNS port discovery, reconnection, and
unattended-device preparation — which is what this folder supplies.

The full write-up, including the four things that broke and the model
comparison with numbers, is on my blog:
**[Driving a Spare Phone with Google's ARTEMIS, No Cable Required](https://blog.antoniosolismz.com/posts/artemis-no-cable-required/)**

## What is in here

| File | Purpose |
|---|---|
| `artemis-wifi.sh` | Wireless toolkit: `pair`, `discover`, `connect`, `watch`, `reconnect`, `prep`/`unpark`, `park`, `status` |
| `build_opencode_go_config.py` | Generates a non-Google `artemis.jsonc` variant from the stock one (provider + model rewrite, video analysis off) |
| `zen_go_proxy.py` | Small OpenAI-compatible bridge that injects the headers an OpenCode Go endpoint requires |
| `run_ab.sh` | One-arm runner for controlled A/B testing of a vision model on identical device state |
| `model_shootout2.py` | Synthetic grounding benchmark (OCR + normalised tap accuracy) |
| `vision_probe.py` | Minimal single-model vision/grounding check |
| `check_artemis_llm_route.py` | Drives ARTEMIS' own config loader and model factory to prove the LLM route works |
| `mcp_probe.py`, `mcp_device_check.py` | Spawn the ARTEMIS MCP server the way a client does, and pull a device screenshot through it |
| `patches/` | The one patch ARTEMIS needs to run without a Google key (see below) |
| `systemd/` | User units for the reconnection loop and the model bridge |
| `RUNBOOK.md` | Step-by-step: pairing, prep, first task, parking, resuming, troubleshooting |

The wireless test suite lives where it belongs, in ARTEMIS' own tree:
`tests/unit/runtime/test_wireless_serial.py`. It pins the behaviour that makes
this possible — the device pool keeps a `host:port` serial verbatim, adbutils
accepts a colon serial, and the task-submission gate passes a Wi-Fi target.

## Quick start

```bash
# Phone: Developer options -> Wireless debugging -> ON,
#        then "Pair device with pairing code" (keep that dialog open)

export PATH="$HOME/.local/bin:$PATH"          # adb 37, for ADB Wi-Fi 2.0
./artemis-wifi.sh discover                    # shows the pairing endpoint over mDNS
./artemis-wifi.sh pair 192.168.1.50:37419 123456
./artemis-wifi.sh status                      # expect: [Wi-Fi] <serial>  device
./artemis-wifi.sh prep "$(adb devices | awk 'NR>1 && $2=="device"{print $1; exit}')"

cd ../                                     # ARTEMIS root
.venv/bin/artemis helper install -s <serial>   # pushes the accessibility helper over Wi-Fi
.venv/bin/artemis run "Open Settings, find Battery and tell me the current level" \
  --profile flash -s <serial> --standalone
```

## The four gotchas

1. **The serial is not `ip:port`.** With adb 37 and a paired device you get the
   mDNS service name, e.g. `adb-XXXXXXX-XXXXXX._adb-tls-connect._tcp`, and that is
   what goes to `-s` / `device_serial`. Do not also `adb connect ip:port`, or the
   same phone appears as two candidate devices.
2. **No Google key means three internal helpers fail at construction.** The visual
   step summarizer, the memory capsule chunker and the image processor all call
   `get_google_llm()` unconditionally and raise `API key required for Gemini
   Developer API` before the first action. `patches/0001-*.patch` guards that one
   chokepoint and routes it to your OpenAI-compatible endpoint instead; it is a
   no-op whenever a Gemini key is present.
   ```bash
   git apply wireless/patches/0001-route-gemini-only-helpers-to-openai-endpoint.patch
   ```
3. **A node with a `model` and no `provider` silently means Gemini.**
   `flash.step_summarizer` has no `provider` field in its schema at all, so
   configuration alone cannot move it. `build_opencode_go_config.py` makes every
   provider explicit and reports how many it had to add.
4. **Wireless transports drop.** Expect `DeviceOfflineError: ... adb does not list
   it` eventually; recover with `adb connect` to whatever `adb mdns services`
   reports, and run `systemd/artemis-wifi-watch.service` so it heals itself.

## Verified, with numbers

Everything here was measured on a spare Redmi Note 10 Pro (Android 13 / MIUI 14).
The first real task — *"open Settings, go to Battery, tell me the level"* —
completed in 4 steps, and the phone's foreground activity was confirmed
independently with `dumpsys` rather than trusting the agent's self-report.

A controlled A/B of two vision models on the identical task, from a
force-stopped app on the home screen:

| | glm-5.3-flash | deepseek-v4-flash-vision-exp |
|---|---|---|
| Wall clock | 102 s | 100 s |
| Steps | 11 | 10 |
| Prompt tokens | 150,841 | 135,836 |
| Cache hit ratio | 55.7% | 56.6% |
| Cost | $0.0153 | $0.0120 |
| Answer correct | yes | yes |

Cost per screenshot-heavy task lands around **$0.012–0.015**, so a monthly
allowance goes a long way. Screenshots convert at roughly one image token per
1,280 pixels (a 1080x2400 screen is about 2,000 tokens).

## Notes

* The accessibility helper (`com.artemis.helper`) binds a command server to
  `127.0.0.1` only and requires a session token delivered over a
  `WRITE_SECURE_SETTINGS`-protected broadcast, so nothing off-device can reach it.
* ARTEMIS' submission gate refuses a locked device by design, which is why
  `artemis-wifi.sh prep` exists.
* `zen_go_proxy.py` is a personal bridge for a subscription I already pay for, not
  a supported integration. Check your provider's terms before pointing an agent at
  any subscription endpoint.
* The scripts are written for Linux with adb 37+ and were developed against
  ARTEMIS 1.29.0.

## Credits and license

ARTEMIS is Google's project, licensed Apache-2.0; this fork keeps that license and
adds the files listed above under the same terms. The patch in `patches/` is a
local modification for running ARTEMIS against non-Google providers.
