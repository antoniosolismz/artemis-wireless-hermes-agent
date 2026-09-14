#!/usr/bin/env bash
# artemis-wifi — cable-free ADB device management for ARTEMIS / Hermes.
#
# Android 11+ wireless debugging needs three things ARTEMIS itself does not do:
#   1. one-time pairing   (`adb pair host:port CODE` — pairing port is random)
#   2. mDNS discovery     (the connect port is also random, and changes on reboot)
#   3. reconnection       (adb drops TCP devices on sleep/network change)
# This script supplies all three, then hands the device to ARTEMIS as a normal
# serial (`ip:port`) so every MCP tool works unchanged.
#
# Usage:
#   artemis-wifi.sh status                      # adb + mdns + device summary
#   artemis-wifi.sh discover                    # list pairable / connectable endpoints
#   artemis-wifi.sh pair <host[:port]> <code>   # one-time pairing, then connect
#   artemis-wifi.sh connect <host[:port]>       # connect (port defaults to mDNS/5555)
#   artemis-wifi.sh watch [interval]            # keep connected (systemd-friendly)
#   artemis-wifi.sh prep <serial>               # unlock-gate prep for unattended use
#   artemis-wifi.sh endpoints | forget <host>   # manage the remembered device list
set -uo pipefail

ADB="${ADB_BIN:-$(command -v adb || echo "$HOME/.local/bin/adb")}"
STATE_DIR="${ARTEMIS_WIFI_STATE:-$HOME/.config/artemis-wireless}"
ENDPOINTS="$STATE_DIR/endpoints"
LOG="$STATE_DIR/watch.log"
DEFAULT_PORT=5555

# Make sure the ADB server runs mDNS discovery with the modern backend (adb 34+).
export ADB_MDNS="${ADB_MDNS:-1}"
export ADB_MDNS_OPENSCREEN="${ADB_MDNS_OPENSCREEN:-0}"

mkdir -p "$STATE_DIR"; touch "$ENDPOINTS"

die() { echo "artemis-wifi: $*" >&2; exit 1; }
info() { echo "[artemis-wifi] $*"; }

require_adb() {
  [[ -x "$ADB" || -n "$(command -v "$ADB" 2>/dev/null)" ]] || die "adb not found (set ADB_BIN=/path/to/adb)"
  "$ADB" version >/dev/null 2>&1 || die "adb at '$ADB' is not runnable"
}

adb_() { "$ADB" "$@"; }

# --- device state -----------------------------------------------------------
# `adb devices -l` prints: "<serial> <state> [model:.. product:..]"; a wireless
# serial is literally "192.168.1.50:5555", so nothing downstream needs to care.
device_table() { adb_ devices -l | tail -n +2 | sed '/^[[:space:]]*$/d'; }

state_of() { # state_of <host-or-serial> -> device|offline|unauthorized|""
  device_table | awk -v t="$1" '{split($1,a,":"); if ($1==t || a[1]==t) {print $2; exit}}'
}

# adb reports mDNS-discovered devices with the service name as the serial
# (ADB Wi-Fi 2.0), e.g. adb-XXXXXXX-XXXXXX._adb-tls-connect._tcp.
is_wireless() { [[ "$1" == *:* || "$1" == *"_adb-tls"* ]]; }

cmd_status() {
  require_adb
  echo "adb binary ...... $ADB"
  echo "adb version ..... $("$ADB" version | awk '/^Version/{print $2; exit}')"
  echo "server socket ... $(adb_ devices >/dev/null 2>&1 && echo "responsive (tcp:5037)" || echo "not running")"
  echo "mdns ............ $("$ADB" mdns check 2>&1 | tr -d '\n')"
  echo "mdns backend .... ADB_MDNS=$ADB_MDNS ADB_MDNS_OPENSCREEN=$ADB_MDNS_OPENSCREEN"
  echo
  echo "Devices:"
  if [[ -z "$(device_table)" ]]; then
    echo "  (none) — enable Wireless debugging on the device, then: artemis-wifi.sh discover"
  else
    device_table | while read -r serial state rest; do
      kind="USB"; is_wireless "$serial" && kind="Wi-Fi"
      echo "  [$kind] $serial  $state  ${rest:-}"
    done
  fi
  echo
  echo "Remembered endpoints ($ENDPOINTS):"
  [[ -s "$ENDPOINTS" ]] && sed 's/^/  /' "$ENDPOINTS" || echo "  (none)"
  echo
  echo "ARTEMIS readiness gate needs state=device AND an unlocked screen."
  echo "Run: artemis-wifi.sh prep <serial>   to make an unattended device cooperate."
}

# --- discovery --------------------------------------------------------------
# `adb mdns services` prints pairs like:
#   ADB-1A2B._adb-tls-pairing._tcp. 192.168.1.50:37231
#   ADB-1A2B._adb-tls-connect._tcp.  192.168.1.50:41283
cmd_discover() {
  require_adb
  local out
  out="$(timeout 8 "$ADB" mdns services 2>/dev/null || true)"
  # `adb mdns services` columns are TAB separated: <name> <service-type> <address>
  local pairing=() connect=()
  while read -r name svc addr; do
    [[ -z "${addr:-}" ]] && continue
    case "$svc" in
      *pairing*) pairing+=("$addr") ;;
      *connect*) connect+=("$addr") ;;
    esac
  done <<< "$out"

  if [[ ${#pairing[@]} -eq 0 && ${#connect[@]} -eq 0 ]]; then
    echo "No wireless-debugging services advertised on this network."
    echo "On the device: Settings > Developer options > Wireless debugging = ON,"
    echo "and the phone must be on the same LAN as this host."
    return 1
  fi
  (( ${#pairing[@]} )) && { echo "Pairable now (needs the 6-digit code from the device):"; printf '  %s\n' "${pairing[@]}"; }
  (( ${#connect[@]} )) && { echo "Already paired (connect directly):"; printf '  %s\n' "${connect[@]}"; }
  return 0
}

remember() { # remember <endpoint>
  grep -qxF "$1" "$ENDPOINTS" || echo "$1" >> "$ENDPOINTS"
}

# --- pairing / connecting ---------------------------------------------------
cmd_pair() {
  require_adb
  local target="${1:-}" code="${2:-}"
  [[ -n "$target" && -n "$code" ]] || die "usage: artemis-wifi.sh pair <host[:port]> <6-digit-code>"
  is_wireless "$target" || target="$target:$(mdns_endpoint_for "$target" pairing || echo 37231)"

  info "pairing with $target ..."
  local out; out="$(adb_ pair "$target" "$code" 2>&1)"
  echo "$out"
  grep -qi "successfully paired" <<< "$out" || die "pairing failed (code expired? re-open 'Pair device with pairing code' on the device)"

  # Pairing port != connect port. Resolve the connect port via mDNS, or fall
  # back to the legacy 5555 if the device exposes a stable tcpip port.
  local host="${target%%:*}" connect_target
  connect_target="$(mdns_endpoint_for "$host" connect || true)"
  [[ -z "$connect_target" ]] && connect_target="$host:$DEFAULT_PORT"
  cmd_connect "$connect_target"
}

mdns_endpoint_for() { # mdns_endpoint_for <host> <pairing|connect>
  local host="$1" kind="$2" out name svc addr
  out="$(timeout 8 "$ADB" mdns services 2>/dev/null || true)"
  while read -r name svc addr; do
    [[ -z "${addr:-}" ]] && continue
    [[ "${addr%%:*}" == "$host" ]] || continue
    case "$kind:$svc" in
      pairing:*pairing*) echo "$addr"; return 0 ;;
      connect:*connect*) echo "$addr"; return 0 ;;
    esac
  done <<< "$out"
  return 1
}

cmd_connect() {
  require_adb
  local target="${1:-}"
  [[ -n "$target" ]] || { cmd_discover; die "usage: artemis-wifi.sh connect <host[:port]>"; }
  is_wireless "$target" || target="$target:$DEFAULT_PORT"
  local host="${target%%:*}"

  # If mDNS knows a connect port for this host, prefer it (Android 11+ rotates ports).
  local mdns_target; mdns_target="$(mdns_endpoint_for "$host" connect || true)"
  [[ -n "$mdns_target" && "$mdns_target" != "$target" ]] && info "mDNS advertises $mdns_target (using it)" && target="$mdns_target"

  local out; out="$(adb_ connect "$target" 2>&1)"
  echo "$out"
  sleep 1
  local st; st="$(state_of "$target")"
  if [[ "$st" == "device" ]]; then
    remember "$target"
    info "connected: $target  (ARTEMIS device_serial=\"$target\")"
    return 0
  elif [[ "$st" == "unauthorized" ]]; then
    info "device unauthorized — accept the 'Allow USB debugging?' RSA prompt on the device screen"
    return 1
  fi
  info "not ready (state='${st:-none}'). If this is a first-time Android 11+ device, run: artemis-wifi.sh pair <host:pairingPort> <code>"
  return 1
}

# --- keepalive --------------------------------------------------------------
# Android 11-16 drops the TCP transport on sleep/roam and rotates the port on
# reboot; Android 17 + adb 37 (ADB Wi-Fi 2.0) auto-reconnects on trusted nets.
# Either way this one pass re-establishes the transport artemis needs to see.
reconnect_pass() {
  local quiet="${1:-}"
  local n=0 ep st out
  while read -r ep; do
    [[ -z "$ep" ]] && continue
    n=$((n+1))
    st="$(state_of "$ep")"
    if [[ "$st" != "device" ]]; then
      out="$(adb_ connect "$ep" 2>&1)"
      local line
      line="$(date -Is) $ep was '${st:-gone}' -> $(tail -n1 <<< "$out")"
      echo "$line" >> "$LOG"
      [[ "$quiet" == "quiet" ]] || echo "[artemis-wifi] $line"
    fi
  done < "$ENDPOINTS"

  # A rotated port for a remembered host: mDNS already knows the new address.
  if (( n > 0 )); then
    local name addr host
    while read -r name addr; do
      [[ "$name" == *connect* && -n "${addr:-}" ]] || continue
      host="${addr%%:*}"
      grep -q "^${host}:" "$ENDPOINTS" || continue
      if state_of "$addr" | grep -q device; then
        remember "$addr"
        [[ "$quiet" == "quiet" ]] || info "re-discovered $addr at a new port"
      fi
    done <<< "$(timeout 8 "$ADB" mdns services 2>/dev/null || true)"
  fi
  return 0
}

cmd_reconnect() { require_adb; reconnect_pass; }

cmd_watch() {
  require_adb
  local interval="${1:-30}"
  info "watch loop started (every ${interval}s) — log: $LOG"
  while true; do
    reconnect_pass quiet
    sleep "$interval"
  done
}

# --- unattended-device preparation -----------------------------------------
# ARTEMIS's submission gate refuses a locked device, and Android sleeps the
# screen (dropping the wireless transport). These settings are user-side only.
cmd_prep() {
  require_adb
  local serial="${1:-}"
  [[ -n "$serial" ]] || { cmd_status; die "usage: artemis-wifi.sh prep <serial>"; }
  info "preparing $serial for unattended use ..."
  adb_ -s "$serial" shell svc power stayon true            && info "screen stays on while charging"
  adb_ -s "$serial" shell settings put system screen_off_timeout 2147483647 && info "screen timeout disabled"
  if adb_ -s "$serial" shell locksettings set-disabled true 2>/dev/null; then
    info "lock screen disabled (still press power to wake, not to unlock)"
  else
    info "could not disable lock screen on this build — keep the device unlocked/charging"
  fi
  adb_ -s "$serial" shell input keyevent KEYCODE_WAKEUP >/dev/null 2>&1 && info "device woken"
  echo
  info "verify with: artemis-wifi.sh status"
}

cmd_park() {
  require_adb
  local serial="${1:-}"
  [[ -n "$serial" ]] || { cmd_status; die "usage: artemis-wifi.sh park <serial>"; }
  info "parking $serial (back to normal phone behaviour) ..."
  # Undo what prep changed for unattended runs. The pairing, the accessibility
  # helper, wireless debugging and the remembered endpoint are all deliberately
  # left in place: they cost nothing while parked and save redoing setup later.
  adb_ -s "$serial" shell settings put system screen_off_timeout 30000 \
    && info "screen timeout restored to 30s (no more battery drain while off the charger)"
  adb_ -s "$serial" shell svc power stayon false >/dev/null 2>&1 \
    && info "stay-awake released"
  local st; st="$(state_of "$serial")"
  info "device state: ${st:-unknown} — press power to sleep it; tasks will refuse while locked (by design)"
}

cmd_unpark() { cmd_prep "$@"; }   # re-apply unattended prep when testing resumes

cmd_endpoints() { sed 's/^/  /' "$ENDPOINTS"; [[ -s "$ENDPOINTS" ]] || echo "  (none)"; }
cmd_forget() {
  local host="${1:-}"; [[ -n "$host" ]] || die "usage: artemis-wifi.sh forget <host>"
  local tmp; tmp="$(mktemp)"
  grep -v "^${host}\(:.*\)\?$" "$ENDPOINTS" > "$tmp" || true
  mv "$tmp" "$ENDPOINTS"; adb_ disconnect "$host" >/dev/null 2>&1 || true
  info "forgot $host"
}

case "${1:-status}" in
  status)    cmd_status ;;
  discover)  cmd_discover ;;
  pair)      shift; cmd_pair "$@" ;;
  connect)   shift; cmd_connect "$@" ;;
  watch)     shift; cmd_watch "$@" ;;
  reconnect) cmd_reconnect ;;  # one pass — cron / systemd-timer friendly
  prep)      shift; cmd_prep "$@" ;;
  unpark)    shift; cmd_unpark "$@" ;;                 # alias of prep
  park)      shift; cmd_park "$@" ;;                   # back to normal phone behaviour
  endpoints) cmd_endpoints ;;
  forget)    shift; cmd_forget "$@" ;;
  -h|--help|help)
    sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//' ;;
  *) die "unknown command '$1' (try: status|discover|pair|connect|watch|prep|endpoints|forget)" ;;
esac
