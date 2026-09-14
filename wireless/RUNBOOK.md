# RUNBOOK — bring the spare Android device online (cable-free) with ARTEMIS + Hermes

Everything on the host side is already installed and verified. You only need to
do the on-phone steps and supply two things (pairing code + LLM key).

- Host: `hermesagent` = **192.168.1.10/24** (gateway 192.168.1.1)
- Device must join the **192.168.1.0/24** network — mDNS discovery does **not**
  cross subnets/VLANs, so a guest Wi-Fi or the other building's SSID will fail.

---

## Step 0 — Host side: DONE (nothing to do)

The LLM backend is already wired to the **OpenCode Go subscription** — no new API
key purchase needed. Verified working:

```bash
~/projects/artemis-wireless/switch-llm-backend.sh status
# -> active config: provider openai, model deepseek-v4-flash-vision-exp
# -> bridge service: active     bridge health: OK
```

How it works: OpenCode Go requires an `x-opencode-session` routing header and a
client user-agent, which ARTEMIS cannot send — so `zen_go_proxy.py` runs as the
`zen-go-bridge` systemd service on `127.0.0.1:8787`, injects those headers, and
forwards to `https://opencode.ai/zen/go/v1`. ARTEMIS points at it through
`OPENAI_BASE_URL`. Verified end to end with ARTEMIS' own config loader and model
factory (`check_artemis_llm_route.py`): screenshot read correctly and a real
`tap` tool call emitted at the right coordinates.

Switch backends any time (reversible, both configs kept):

```bash
~/projects/artemis-wireless/switch-llm-backend.sh gemini        # stock Gemini
~/projects/artemis-wireless/switch-llm-backend.sh opencode-go   # Go subscription
```

Notes:
* `config/artemis.gemini.jsonc` is the untouched original;
  `config/artemis.opencode-go.jsonc` is the generated variant.
* Vision nodes ride `deepseek-v4-flash-vision-exp`; text-only nodes
  (summaries, entity extraction) ride `mimo-v2.5`, which is ~20x cheaper.
* Screen-recording analysis is switched off in the Go variant: Go serves images,
  and scrcpy isn't installed.
* Leave `ARTEMIS_KEEP_DEVICE_AWAKE=true` (default) — ARTEMIS wakes the display and
  dismisses a *non-secure* keyguard by itself.
* `adb` 37 lives at `~/.local/bin/adb` (in PATH for the artemis launcher).
* A Google AI Studio / OpenAI / Anthropic key still works if you ever prefer it —
  `switch-llm-backend.sh gemini` plus a key in `~/projects/artemis/.env`.

### Cost of the Go route (measured, not estimated from docs)

* Measured conversion: 1 image token per ~1,280 px, so a 1080x2400 screenshot ≈
  2,000 tokens.
* A 30-step task ≈ 180k input + 4.5k output ≈ **$0.03 off-peak**, ~$0.006 with
  prompt-cache hits (cached reads are $0.003/1M).
* The vision model's limits are $15/month, $3 per 5h: roughly **500 tasks/month**
  (250 at peak pricing), ~100 per 5-hour window.

### Caveat worth knowing

OpenCode Go's docs describe it as *"designed for OpenCode and other coding agents
that produce similar types of requests"*, with traffic monitored for abuse. Hermes
itself is on their validated-clients list; ARTEMIS driving a phone through this
bridge is not coding-agent traffic and is therefore a grey area **on your own
subscription**. Volume here is small (cents per task), but it is your account.
The vision model is also experimental (`-vision-exp`) and can change without notice.


## Step 1 — Phone prerequisites (on the device)

1. Same Wi-Fi as the host: **192.168.1.x** (2.4 or 5 GHz both fine).
2. Developer options: Settings → About phone → tap **Build number** 7×.
3. Developer options → **Wireless debugging: ON**.
   * Android 11+ only. For Android ≤10 see "Legacy path" at the bottom.
4. **Keep the phone on an unmetered/trusted network** — wireless debugging turns
   itself off when Android decides the network is untrusted.
5. **Disable the secure lock screen** (PIN/pattern/password) for unattended use:
   `wm dismiss-keyguard` cannot get past a *secure* keyguard, and ARTEMIS's task
   gate refuses to submit to a locked device. Step 3 (`prep`) does this via adb if
   your ROM allows it; otherwise set Screen lock to **None** manually.
6. Charging: a **USB** power source keeps artemis's own stay-awake policy
   (`stayon usb` = USB only, not AC) effective. On a wall brick/wireless pad the
   screen can sleep; step 3's `stayon true` plus a long screen timeout covers that.

## Step 2 — Pair + connect (one time per device)

On the phone: **Wireless debugging → "Pair device with pairing code"** — leave
that dialog open; it shows `IP:PORT` and a 6-digit code that expires quickly.

On the host:

```bash
export PATH="$HOME/.local/bin:$PATH"
cd ~/projects/artemis-wireless

./artemis-wifi.sh discover                       # confirms the phone is visible (mDNS)
./artemis-wifi.sh pair 192.168.0.<phone>:<pairing-port> <6-digit-code>
./artemis-wifi.sh status                         # expect: [Wi-Fi] 192.168.0.<phone>:<port>  device
```

Pairing happens once and survives reboots; the endpoint is remembered in
`~/.config/artemis-wireless/endpoints` and the already-running
`artemis-wifi-watch` service reconnects it from then on
(`systemctl --user status artemis-wifi-watch`).

## Step 3 — Prep the device for unattended use

```bash
./artemis-wifi.sh prep 192.168.0.<phone>:<port>
```

Runs: `svc power stayon true`, screen timeout → max, `locksettings set-disabled true`,
wake the display. Falls back to advice if the ROM refuses the lock change.

## Step 4 — Verify through ARTEMIS

```bash
cd ~/projects/artemis
.venv/bin/artemis doctor                                  # environment
.venv/bin/artemis helper install -s 192.168.0.<phone>:<port>
.venv/bin/artemis helper status  -s 192.168.0.<phone>:<port>
```

`helper install` pushes the bundled Artemis Accessibility Helper APK
(`packages/artemis-accessibility-helper/ArtemisAccessibilityHelper.apk`) and
enables the service. If the ROM rejects the programmatic enable, artemis opens
the Accessibility settings screen on the phone and asks you to flip the switch:
**Settings → Accessibility → Installed apps → Artemis Accessibility Helper → ON**.

## Step 5 — Drive it from Hermes

In a **new Hermes session** (MCP tools load at startup):

* `mobile_diagnose` — expect `verdict: ready`
* `mobile_get_device_state` with `device_serial="192.168.0.<phone>:<port>"`, `view_type="screenshot"`
* `mobile_run_task` with the same `device_serial`, e.g.
  *"Open Settings, find Battery and tell me the current level"*, `profile=flash`

---

## Verified working (2026-09-13)

First successful autonomous task on the physical device, cable-free:

```bash
cd ~/projects/artemis
S="adb-XXXXXXX-XXXXXX._adb-tls-connect._tcp"      # see note below about the serial
.venv/bin/artemis run "Open the Settings app, go to Battery, and tell me the current battery percentage" \
  --profile flash -s "$S" --standalone
```

Result: `✅ Automation ... is success`, trace `..._PASS_...`, 4 steps. Device-verified:
the phone's foreground activity was `com.miui.powercenter.PowerMainActivity` (Battery
screen open, 74% charging) — confirmed independently by `dumpsys`, not just by the agent.
Cost: ~**$0.004** for the run (43.7k prompt tokens, 51% prompt-cache hits).

### Device specifics (Redmi Note 10 Pro / "sweet")

| Fact | Value |
|---|---|
| Model | M2101K6G, `sweet_global`, Xiaomi |
| OS | Android 13 (SDK 33), MIUI V140 |
| Power | AC charger — ARTEMIS re-asserts `stayon usb` (USB-only) each run, which does not hold here, so it falls back to a 5-second host heartbeat; `prep`'s `stayon true` is overwritten |
| Lock screen | `locksettings set-disabled true` accepted by MIUI |
| Accessibility helper | installed + **enabled over Wi-Fi**, no manual Settings tap needed |

### Serial format gotcha (ADB Wi-Fi 2.0)

With adb 37 and a paired device, the phone shows up with its **mDNS service name**
as the serial — `adb-XXXXXXX-XXXXXX._adb-tls-connect._tcp` — not `ip:port`. That
name is stable across reboots (it derives from the device serial), so it is the
value to pass to `-s` / `device_serial`. Do **not** additionally run
`adb connect <ip>:<port>`: that registers a second transport for the same phone
and ARTEMIS would see two candidate devices. The keepalive service is therefore
unnecessary for this device — adb reconnects it itself.

### Required local patch (OpenCode Go / non-Google backends)

ARTEMIS hardcodes Gemini in several internal helpers — the visual step
summarizer, the memory capsule chunker and the image processor all call
`get_google_llm()`, which raises `API key required for Gemini Developer API`
before the first action when no Google key exists. One guarded patch at that
single chokepoint fixes all of them; it is a no-op when a Gemini key is present.

Applied here, kept as a file so it survives a fresh clone:

```bash
cd ~/projects/artemis
git apply ~/projects/artemis-wireless/patches/0001-route-gemini-only-helpers-to-openai-endpoint.patch
```

Also note: a node that names a `model` without a `provider` inherits **Google** at
runtime, and `flash.step_summarizer`'s schema has no `provider` field at all — so
that knob is Gemini-only by construction. `build_opencode_go_config.py` adds
explicit providers to every model-bearing node and reports how many it fixed.

## Parking the device / resuming testing

**When you're done (finished testing):**

```bash
~/projects/artemis-wireless/artemis-wifi.sh park <serial>
```

Restores normal phone behaviour: screen timeout back to 30s, stay-awake released.
It deliberately **leaves in place** the adb pairing, the accessibility helper,
wireless debugging, the OpenCode Go wiring and the remembered endpoint — they cost
nothing while parked and save redoing setup. Then just press power / unplug.

**When you come back:**

1. If the phone **rebooted**, re-enable Developer options → **Wireless debugging**
   (Android 11–16 turns the toggle off on reboot). The pairing itself persists.
2. Unlock the phone if a lock screen is set (ARTEMIS refuses a locked device).
3. `./artemis-wifi.sh discover` — if the port rotated, mDNS has the new one; the
   keepalive service also reconnects automatically.
4. `./artemis-wifi.sh unpark <serial>` (alias of `prep`) — screen awake, timeout
   raised, lock screen disabled for the session.
5. `./artemis-wifi.sh status`, then run a task.

**What survives everything:** adb pairing (pair once, ever), the helper APK +
its enabled service, `~/projects/artemis/.env`, the bridge and its systemd service,
the `artemis` MCP registration, and the keepalive endpoint list.

**Lock screens:** the ARTEMIS submission gate requires `is_locked == False`, so a
locked device is refused by design — unlock before a run. Note this spare device
currently has **no lock credential at all** (`locksettings get-disabled` fails with
"Credential can't be null or empty"), so the power button only blanks the screen
and a swipe opens it. Set a PIN/pattern in Settings → Security if you want a real
lock; that cannot be done over adb (it needs the owner's secret).

## Troubleshooting

| Symptom | Fix |
|---|---|
| `discover` finds nothing | Phone not on 192.168.1.0/24, wireless debugging off, or screen locked; re-toggle wireless debugging. |
| Pairing code rejected | The dialog's code expires — reopen "Pair device with pairing code" and retry immediately. |
| Connected then drops | Expected on Android 11–16 when the phone sleeps/roams. The watch service reconnects; on Android 17 + adb 37 pairing auto-reconnects. |
| Port changed after reboot | `./artemis-wifi.sh reconnect` — mDNS re-resolves the new port. |
| Task refused, "Device Locked" | Secure lock screen still on. Run `prep` or set Screen lock to None. |
| `mobile_run_task` fails on credentials | Missing key in `~/projects/artemis/.env`; re-run `mobile_diagnose`. |
| Screen sleeps while unplugged | Put it on a **USB** charger, or rely on `prep`'s `stayon true`. |

## Legacy path (Android ≤10, no Wireless-debugging toggle)

Needs a cable **once**: `adb -s <usb-serial> tcpip 5555` then
`./artemis-wifi.sh connect <phone-ip>` (port 5555 stays stable until reboot).

## Removal / undo

```bash
cd ~/projects/artemis
.venv/bin/artemis helper uninstall -s <serial>     # removes the on-device helper
cd ~/projects/artemis-wireless
./artemis-wifi.sh forget <phone-ip>                # stop managing the endpoint
systemctl --user disable --now artemis-wifi-watch  # stop the keepalive
```
