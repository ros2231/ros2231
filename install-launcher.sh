#!/usr/bin/env bash
# Creates MintLink launchers in your app menu (and on the Desktop if it exists):
#   MintLink            - phone remote control (Bluetooth)
#   MintLink Phone Screen - show your phone's screen on the PC (USB, no internet)
# Run once from the folder that contains mintlink.py:  bash install-launcher.sh
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -f "$DIR/mintlink.py" ] || { echo "mintlink.py not found next to this script." >&2; exit 1; }
chmod +x "$DIR/mintlink.py" "$DIR/mintlink-launch.sh"

APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
DESKTOP="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"
mkdir -p "$APPS"

# Terminal=true so you can see the PIN / messages. The wrapper script keeps the
# window open if MintLink exits with an error, so you can read it.
make() { # file name comment exec-args
  local f="$APPS/$1.desktop"
  cat > "$f" <<DESK
[Desktop Entry]
Type=Application
Version=1.0
Name=$2
Comment=$3
Exec="$DIR/mintlink-launch.sh"$4
Icon=$DIR/icon-512.png
Terminal=true
Categories=Utility;
Keywords=phone;remote;touchpad;bluetooth;scrcpy;mirror;
StartupNotify=false
DESK
  chmod +x "$f"
  if [ -d "$DESKTOP" ]; then
    cp "$f" "$DESKTOP/$2.desktop"; chmod +x "$DESKTOP/$2.desktop"
    command -v gio >/dev/null && gio set "$DESKTOP/$2.desktop" metadata::trusted true 2>/dev/null || true
  fi
  echo "App menu entry: $f"
}
make mintlink "MintLink" "Use your phone as a touchpad, keyboard and remote for this PC" ""
make mintlink-phone-screen "MintLink Phone Screen" "Show your phone's screen on this PC over USB (no internet needed)" " --mirror"

command -v update-desktop-database >/dev/null && update-desktop-database "$APPS" 2>/dev/null || true
[ -d "$DESKTOP" ] && echo "Desktop shortcuts added to $DESKTOP (if one shows a warning, right-click > Allow Launching)"
echo "Done. Search for 'MintLink' in your menu."
