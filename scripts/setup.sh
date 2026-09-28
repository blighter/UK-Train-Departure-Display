#!/bin/bash
# First-run setup: installs dependencies, walks you through config.json
# interactively, and (optionally) installs this as a systemd service with
# automatic updates. Designed to be run once over SSH, right after cloning:
#
#   cd UK-Train-Departure-Display
#   ./scripts/setup.sh
#
# It's safe to run again later — it reuses whatever's already in
# config.json as the default for each question, so re-running it is a
# quick way to tweak settings rather than hand-editing JSON.

set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1

# ---------- output helpers ----------

if [ -t 1 ] && command -v tput >/dev/null 2>&1 && [ "$(tput colors 2>/dev/null || echo 0)" -ge 8 ]; then
    BOLD=$(tput bold); DIM=$(tput dim); RESET=$(tput sgr0)
    RED=$(tput setaf 1); GREEN=$(tput setaf 2); YELLOW=$(tput setaf 3); CYAN=$(tput setaf 6)
else
    BOLD=""; DIM=""; RESET=""; RED=""; GREEN=""; YELLOW=""; CYAN=""
fi

banner() {
    printf '%s%s\n' "$CYAN" "$BOLD"
    cat <<'EOF'

   🚆  UK Train Departure Display
   ───────────────────────────────
   First-run setup
EOF
    printf '%s\n' "$RESET"
}
step()    { printf '\n%s%s▸ %s%s\n' "$BOLD" "$CYAN" "$1" "$RESET"; }
info()    { printf '  %s%s%s\n' "$DIM" "$1" "$RESET"; }
ok()      { printf '  %s✔%s %s\n' "$GREEN" "$RESET" "$1"; }
warn()    { printf '  %s⚠%s %s\n' "$YELLOW" "$RESET" "$1"; }
fail()    { printf '  %s✘%s %s\n' "$RED" "$RESET" "$1"; }

confirm() {
    # confirm "question" default(y|n)
    local prompt="$1" default="${2:-y}" ans
    local hint="y/N"; [ "$default" = "y" ] && hint="Y/n"
    printf '  %s%s%s [%s] ' "$BOLD" "$prompt" "$RESET" "$hint"
    read -r ans
    ans="${ans:-$default}"
    case "$ans" in [Yy]*) return 0 ;; *) return 1 ;; esac
}

ask() {
    # ask "question" "default" VARNAME
    local prompt="$1" default="$2" __var="$3" input
    if [ -n "$default" ]; then
        printf '  %s%s%s %s[%s]%s: ' "$BOLD" "$prompt" "$RESET" "$DIM" "$default" "$RESET"
    else
        printf '  %s%s%s: ' "$BOLD" "$prompt" "$RESET"
    fi
    read -r input
    printf -v "$__var" '%s' "${input:-$default}"
}

ask_required() {
    # ask_required "question" "default" VARNAME   — loops until non-empty
    local prompt="$1" default="$2" __var="$3"
    while true; do
        ask "$prompt" "$default" "$__var"
        if [ -n "${!__var}" ]; then return 0; fi
        warn "That one's required."
    done
}

ask_pattern() {
    # ask_pattern "question" "default" "regex" "hint" VARNAME
    local prompt="$1" default="$2" pattern="$3" hint="$4" __var="$5"
    while true; do
        ask_required "$prompt" "$default" "$__var"
        local val="${!__var}"
        val="$(printf '%s' "$val" | tr '[:lower:]' '[:upper:]')"
        if [[ "$val" =~ $pattern ]]; then
            printf -v "$__var" '%s' "$val"
            return 0
        fi
        warn "$hint"
    done
}

ask_secret() {
    # ask_secret "question" HAS_EXISTING(0|1) VARNAME
    local prompt="$1" has_existing="$2" __var="$3" input
    if [ "$has_existing" = "1" ]; then
        printf '  %s%s%s %s[leave blank to keep current]%s: ' "$BOLD" "$prompt" "$RESET" "$DIM" "$RESET"
    else
        printf '  %s%s%s: ' "$BOLD" "$prompt" "$RESET"
    fi
    read -rs input
    printf '\n'
    if [ -z "$input" ] && [ "$has_existing" != "1" ]; then
        warn "That one's required."
        ask_secret "$prompt" "$has_existing" "$__var"
        return
    fi
    printf -v "$__var" '%s' "$input"
}

banner

if ! command -v python3 >/dev/null 2>&1; then
    fail "python3 isn't installed. Install Python 3.9+ first, then re-run this script."
    exit 1
fi

if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'; then
    fail "Python 3.9+ is required — found $(python3 --version)."
    exit 1
fi

if [ ! -f config.sample.json ] && [ ! -f config.json ]; then
    fail "Can't find config.sample.json — run this from a full clone of the repo (./scripts/setup.sh)."
    exit 1
fi

if [ -f config.json ]; then
    info "Found an existing config.json — reusing its values as defaults, just press enter to keep any of them."
fi

# ---------- 1. system packages (Pi OS Lite needs libopenjp2-7 for Pillow) ----------

step "System packages"
if [ "$(uname -s)" = "Linux" ] && command -v apt-get >/dev/null 2>&1; then
    if dpkg -s libopenjp2-7 >/dev/null 2>&1; then
        ok "libopenjp2-7 already installed"
    elif confirm "Install libopenjp2-7 (needed by Pillow on Raspberry Pi OS Lite)?" y; then
        sudo apt-get update -q && sudo apt-get install -y libopenjp2-7
    else
        warn "Skipped — if Pillow fails to import later, install it manually: sudo apt-get install libopenjp2-7"
    fi
else
    info "Not on a Debian-based Linux system, skipping"
fi

# ---------- 2. python dependencies ----------

step "Python dependencies"
if confirm "Install/update Python packages from requirements.txt?" y; then
    # Debian 12+ (Bookworm) marks the system Python as externally-managed
    # (PEP 668) and refuses a bare `pip install`. Pass --break-system-packages
    # when pip understands it; older pip (Bullseye and earlier) doesn't have
    # the flag at all.
    PIP_EXTRA_ARGS=""
    if pip3 install --help 2>/dev/null | grep -q -- '--break-system-packages'; then
        PIP_EXTRA_ARGS="--break-system-packages"
    fi
    if pip3 install $PIP_EXTRA_ARGS -r requirements.txt; then
        ok "Dependencies installed"
    else
        fail "pip3 install failed — see the README's Troubleshooting section, then re-run this script."
        exit 1
    fi
else
    warn "Skipped — make sure they're installed before starting the service"
fi

# ---------- 3. config.json ----------

step "Board configuration"

eval "$(python3 - <<'PY'
import json, shlex

cfg = {}
for path in ("config.json", "config.sample.json"):
    try:
        with open(path) as f:
            cfg = json.load(f)
        break
    except FileNotFoundError:
        continue
    except (ValueError, OSError):
        break

j = cfg.get("journey", {}) or {}
r = cfg.get("rttApi", {}) or {}
d = (cfg.get("display", {}) or {}).get("dimming", {}) or {}

fields = {
    "CUR_DEPARTURE": j.get("departureStation") or "",
    "CUR_DESTINATION": j.get("destinationStation") or "",
    "CUR_OUT_OF_HOURS": j.get("outOfHoursName") or "",
    "CUR_REFRESH": str(cfg.get("refreshTime", 180)),
    "CUR_RTT_USERNAME": r.get("username") or "",
    "CUR_HAS_PASSWORD": "1" if r.get("password") else "0",
    "CUR_OPERATING_HOURS": r.get("operatingHours") or "6-23",
    "CUR_DIM_ENABLED": "1" if d.get("enabled") else "0",
    "CUR_DIM_START": str(d.get("startHour", 22)),
    "CUR_DIM_END": str(d.get("endHour", 6)),
    "CUR_DIM_BRIGHTNESS": str(d.get("brightness", 10)),
}
for k, v in fields.items():
    print(f"{k}={shlex.quote(v)}")
PY
)"

ask_pattern "Departure station CRS code" "$CUR_DEPARTURE" '^[A-Z]{3}$' \
    "That doesn't look like a CRS code — it should be 3 letters, e.g. SVG for Sevenoaks. Look yours up at nationalrail.co.uk." \
    SETUP_DEPARTURE
info "(only trains from this station will be shown)"

ask "Only show trains heading to this station? CRS code, or leave blank for all destinations" "$CUR_DESTINATION" SETUP_DESTINATION
if [ -n "$SETUP_DESTINATION" ]; then
    SETUP_DESTINATION="$(printf '%s' "$SETUP_DESTINATION" | tr '[:lower:]' '[:upper:]')"
    if ! [[ "$SETUP_DESTINATION" =~ ^[A-Z]{3}$ ]]; then
        warn "That doesn't look like a CRS code, ignoring it — leaving destination filtering off."
        SETUP_DESTINATION=""
    fi
fi

ask_required "Text to show on the blank screen outside operating hours (e.g. your station's name)" "$CUR_OUT_OF_HOURS" SETUP_OUT_OF_HOURS

ask "Refresh interval in seconds" "${CUR_REFRESH:-180}" SETUP_REFRESH
if ! [[ "$SETUP_REFRESH" =~ ^[0-9]+$ ]] || [ "$SETUP_REFRESH" -lt 10 ]; then
    warn "That didn't look like a sane number of seconds, defaulting to 180."
    SETUP_REFRESH=180
fi

printf '\n'
info "Get a free Real Time Trains API account at https://api.rtt.io if you haven't already."
ask_required "Real Time Trains username" "$CUR_RTT_USERNAME" SETUP_RTT_USERNAME
ask_secret "Real Time Trains password" "$CUR_HAS_PASSWORD" SETUP_RTT_PASSWORD

ask_pattern "Operating hours (24h range, e.g. 6-23)" "$CUR_OPERATING_HOURS" '^[0-9]{1,2}-[0-9]{1,2}$' \
    "That should look like 6-23 (start hour, dash, end hour)." \
    SETUP_OPERATING_HOURS

printf '\n'
DIM_DEFAULT="n"; [ "$CUR_DIM_ENABLED" = "1" ] && DIM_DEFAULT="y"
if confirm "Dim the display overnight?" "$DIM_DEFAULT"; then
    SETUP_DIM_ENABLED=1
    ask "Dimming start hour (0-23)" "$CUR_DIM_START" SETUP_DIM_START
    ask "Dimming end hour (0-23)" "$CUR_DIM_END" SETUP_DIM_END
    ask "Dimmed brightness (0-255)" "$CUR_DIM_BRIGHTNESS" SETUP_DIM_BRIGHTNESS
else
    SETUP_DIM_ENABLED=0
    SETUP_DIM_START="$CUR_DIM_START"
    SETUP_DIM_END="$CUR_DIM_END"
    SETUP_DIM_BRIGHTNESS="$CUR_DIM_BRIGHTNESS"
fi

export SETUP_DEPARTURE SETUP_DESTINATION SETUP_OUT_OF_HOURS SETUP_REFRESH \
    SETUP_RTT_USERNAME SETUP_RTT_PASSWORD SETUP_OPERATING_HOURS \
    SETUP_DIM_ENABLED SETUP_DIM_START SETUP_DIM_END SETUP_DIM_BRIGHTNESS

python3 - <<'PY'
import json, os

cfg = {}
for path in ("config.json", "config.sample.json"):
    try:
        with open(path) as f:
            cfg = json.load(f)
        break
    except FileNotFoundError:
        continue

journey = cfg.setdefault("journey", {})
journey["departureStation"] = os.environ["SETUP_DEPARTURE"]
journey["destinationStation"] = os.environ["SETUP_DESTINATION"] or None
journey["outOfHoursName"] = os.environ["SETUP_OUT_OF_HOURS"]
journey.setdefault("stationAbbr", {"International": "Intl."})

cfg["refreshTime"] = int(os.environ["SETUP_REFRESH"])
cfg["apiMethod"] = "rtt"

rtt = cfg.setdefault("rttApi", {})
rtt["username"] = os.environ["SETUP_RTT_USERNAME"]
if os.environ.get("SETUP_RTT_PASSWORD"):
    rtt["password"] = os.environ["SETUP_RTT_PASSWORD"]
rtt["operatingHours"] = os.environ["SETUP_OPERATING_HOURS"]

cfg.setdefault("transportApi", {"appId": "", "apiKey": "", "operatingHours": "0-23"})

dimming = cfg.setdefault("display", {}).setdefault("dimming", {})
dimming["enabled"] = os.environ["SETUP_DIM_ENABLED"] == "1"
dimming["startHour"] = int(os.environ["SETUP_DIM_START"])
dimming["endHour"] = int(os.environ["SETUP_DIM_END"])
dimming["brightness"] = int(os.environ["SETUP_DIM_BRIGHTNESS"])
dimming.setdefault("normalBrightness", 255)

with open("config.json", "w") as f:
    json.dump(cfg, f, indent=2)
    f.write("\n")
PY

ok "config.json written"

# ---------- 4. systemd services ----------

step "Background service"

if ! command -v systemctl >/dev/null 2>&1; then
    warn "systemd isn't available here (not on the Pi?) — skipping service installation."
    warn "Try it now with: ./run.sh"
else
    if confirm "Install this as a background service (runs on boot, restarts on crash)?" y; then
        TARGET_DIR=/var/local/UK-Train-Departure-Display
        HERE="$(pwd -P)"

        if [ "$HERE" = "$TARGET_DIR" ]; then
            ok "Already running from $TARGET_DIR"
        elif [ -d "$TARGET_DIR" ]; then
            warn "$TARGET_DIR already exists — leaving it alone."
            warn "Use scripts/update.sh (or the traindep-update service) to update it instead."
        elif confirm "Copy this repo to $TARGET_DIR, where the service expects to find it?" y; then
            sudo mkdir -p /var/local
            sudo cp -r "$HERE" "$TARGET_DIR"
            ok "Copied to $TARGET_DIR"
        else
            TARGET_DIR="$HERE"
            warn "Using $TARGET_DIR instead — the shipped systemd units hard-code /var/local/UK-Train-Departure-Display,"
            warn "so edit WorkingDirectory/ExecStart in them if you keep the repo here."
        fi

        sudo cp "$TARGET_DIR/systemd/traindep.service" /etc/systemd/system/
        sudo systemctl daemon-reload
        sudo systemctl enable --now traindep.service
        ok "traindep.service installed and started"

        if confirm "Also enable automatic updates via git (checks every 30 min)?" y; then
            sudo cp "$TARGET_DIR/systemd/traindep-update.service" /etc/systemd/system/
            sudo cp "$TARGET_DIR/systemd/traindep-update.timer" /etc/systemd/system/
            sudo systemctl daemon-reload
            sudo systemctl enable --now traindep-update.timer
            ok "traindep-update.timer installed and started"
        fi
    else
        info "Skipped. Run it in the foreground any time with: ./run.sh"
    fi
fi

# ---------- done ----------

printf '\n%s%s' "$GREEN" "$BOLD"
cat <<'EOF'
   ──────────────────────────────────────
   All aboard! 🚉
EOF
printf '%s\n' "$RESET"
info "Useful commands:"
info "  sudo systemctl status traindep.service    # is it running?"
info "  sudo journalctl -u traindep.service -f    # tail the logs"
info "  ./scripts/setup.sh                        # re-run this any time to change settings"
printf '\n'
