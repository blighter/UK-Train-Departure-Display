#!/bin/bash
# Self-update: pull the latest commit from git, reinstall dependencies if
# requirements.txt changed, then restart the traindep service so the new
# code takes effect. Designed to run as the traindep-update.service unit
# (see systemd/traindep-update.{service,timer}), but safe to run by hand:
#
#   sudo systemctl start traindep-update.service
#   sudo journalctl -u traindep-update.service -f
#
# Never force-pushes or merges: it only ever fast-forwards, and it refuses
# to touch a working tree with local changes, so a Pi that's been hand-
# edited is left alone rather than clobbered.

set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
SERVICE=traindep.service

log() { echo "[traindep-update] $*"; }

if [ ! -d .git ]; then
    log "not a git checkout, skipping"
    exit 0
fi

BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null)
if [ -z "$BRANCH" ] || [ "$BRANCH" = "HEAD" ]; then
    log "repo is in a detached HEAD state, refusing to auto-update"
    exit 1
fi

if ! git fetch origin "$BRANCH" --quiet; then
    log "git fetch failed, network or remote problem — leaving things as they are"
    exit 1
fi

OLD_HEAD=$(git rev-parse HEAD)
REMOTE_HEAD=$(git rev-parse "origin/$BRANCH" 2>/dev/null)
if [ -z "$REMOTE_HEAD" ]; then
    log "couldn't resolve origin/$BRANCH, skipping"
    exit 1
fi

if [ "$OLD_HEAD" = "$REMOTE_HEAD" ]; then
    log "already up to date ($OLD_HEAD)"
    exit 0
fi

# config.json is gitignored so it's never part of this check; anything else
# dirty means someone's been editing on the Pi directly — leave it alone.
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    log "working tree has local changes, refusing to auto-update (resolve manually)"
    exit 1
fi

log "updating $OLD_HEAD -> $REMOTE_HEAD"
if ! git pull --ff-only origin "$BRANCH"; then
    log "fast-forward pull failed (history has diverged?), leaving repo on $OLD_HEAD"
    exit 1
fi

NEW_HEAD=$(git rev-parse HEAD)

if git diff --name-only "$OLD_HEAD" "$NEW_HEAD" | grep -qx 'requirements.txt'; then
    log "requirements.txt changed, reinstalling dependencies"
    # Debian 12+ (Bookworm) marks the system Python as externally-managed
    # (PEP 668) and refuses a bare `pip install`. There's no venv here —
    # this runs as root via systemd against the system interpreter — so
    # pass --break-system-packages when pip understands it; older pip
    # (Bullseye and earlier) doesn't have the flag at all.
    PIP_EXTRA_ARGS=""
    if pip3 install --help 2>/dev/null | grep -q -- '--break-system-packages'; then
        PIP_EXTRA_ARGS="--break-system-packages"
    fi
    if ! pip3 install $PIP_EXTRA_ARGS -r requirements.txt; then
        log "dependency install failed — rolling back to $OLD_HEAD so the running service stays consistent"
        git reset --hard "$OLD_HEAD"
        exit 1
    fi
fi

log "restarting $SERVICE"
if command -v systemctl >/dev/null && systemctl restart "$SERVICE"; then
    log "update complete, now running $NEW_HEAD"
else
    log "update pulled but restarting $SERVICE failed — restart it manually"
    exit 1
fi
