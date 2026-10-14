#!/usr/bin/env bash
# Set up Argus on this Mac: virtualenv, config, LaunchAgent.
# Safe to re-run; every step is idempotent.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LABEL="com.argus.agent"
TEMPLATE="$ROOT/ops/$LABEL.plist.template"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PYTHON="${PYTHON:-python3}"

cd "$ROOT"

# 1. Virtualenv + dependencies.
if [[ ! -x .venv/bin/python ]]; then
    echo "==> Creating .venv with $PYTHON"
    "$PYTHON" -m venv .venv
fi
echo "==> Installing Argus into .venv"
.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/python -m pip install --quiet -e '.[dev]'

# 2. Config.
if [[ ! -f argus.toml ]]; then
    cp argus.example.toml argus.toml
    echo
    echo "Created argus.toml from argus.example.toml."
    echo "Edit it and set self_handles to your own iMessage phone number / Apple ID email,"
    echo "then run ./setup.sh again."
    exit 1
fi

mkdir -p logs

# 3. Render the LaunchAgent.
# Escape characters that are special in XML, then in a sed replacement.
escape_for_plist() {
    # (sed, not ${v//...}: bash 5.2's patsub_replacement treats & in a replacement specially.)
    printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g' -e 's/[\\|&]/\\&/g'
}
root_sed="$(escape_for_plist "$ROOT")"
home_sed="$(escape_for_plist "$HOME")"
mkdir -p "$(dirname "$PLIST")"
sed -e "s|__ROOT__|$root_sed|g" -e "s|__HOME__|$home_sed|g" "$TEMPLATE" > "$PLIST.tmp"
plutil -lint "$PLIST.tmp" >/dev/null
mv "$PLIST.tmp" "$PLIST"
echo "==> Wrote $PLIST"

# 4. Manual steps (printed, never run).
REAL_PYTHON="$(.venv/bin/python -c 'import os, sys; print(os.path.realpath(sys.executable))')"
cat <<EOF

==> Manual steps (do these once; this script does not run them)

1. Full Disk Access (required: Argus reads ~/Library/Messages/chat.db).
   Under launchd the process is .venv/bin/python, which is a symlink to:

       $REAL_PYTHON

   Open System Settings -> Privacy & Security -> Full Disk Access, click +,
   press Cmd+Shift+G, paste the path above, and add that binary.
   Granting it to Terminal is NOT enough for the LaunchAgent.
   Note: upgrading Python (e.g. brew upgrade) changes this path; grant it again
   and re-run ./setup.sh afterwards.

2. Automation -> Messages (required: Argus sends replies via AppleScript).
   macOS asks the first time Argus sends a message; click Allow. You can check it
   later under System Settings -> Privacy & Security -> Automation.

3. Keep the Mac mini always on and restarting after power loss:

       sudo pmset -a sleep 0 displaysleep 3 disksleep 0 autorestart 1

4. Enable automatic login for your user (System Settings -> Users & Groups ->
   Automatically log in as...), so the LaunchAgent starts after a reboot.
   (Automatic login is unavailable while FileVault is on.)

5. Make sure Messages is signed in to iMessage on this Mac, and that Claude Code
   is logged in to your subscription (run the bundled CLI once, or 'claude login').

EOF

# 5. Load the LaunchAgent (asks first).
read -r -p "Load (or reload) the Argus LaunchAgent now? [y/N] " answer
if [[ "$answer" =~ ^[Yy]$ ]]; then
    domain="gui/$(id -u)"
    if launchctl print "$domain/$LABEL" >/dev/null 2>&1; then
        echo "==> Unloading the running agent"
        launchctl bootout "$domain/$LABEL" || true
        # bootout is asynchronous; wait for it to finish before bootstrapping again.
        for _ in 1 2 3 4 5 6 7 8 9 10; do
            launchctl print "$domain/$LABEL" >/dev/null 2>&1 || break
            sleep 1
        done
    fi
    launchctl bootstrap "$domain" "$PLIST"
    echo "==> Argus is running. Logs: $ROOT/logs/argus.log"
else
    echo "Skipped. Load it later with:"
    echo "    launchctl bootstrap gui/\$(id -u) $PLIST"
fi
