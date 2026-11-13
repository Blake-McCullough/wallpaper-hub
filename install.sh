#!/usr/bin/env bash
# Wallpaper Hub installer for CachyOS / Arch.
# Works from a git checkout, or on its own:
#   curl -fsSL https://github.com/Blake-McCullough/wallpaper-hub/releases/latest/download/install.sh | bash
set -e
REPO="Blake-McCullough/wallpaper-hub"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || echo .)"

echo "==> Installing Qt bindings"
sudo pacman -S --needed --noconfirm python-pyqt6 python-pyqt6-webengine

if ! command -v linux-wallpaperengine >/dev/null 2>&1; then
  echo "==> Installing linux-wallpaperengine (renderer)"
  if command -v paru >/dev/null 2>&1; then
    paru -S --needed linux-wallpaperengine-git
  elif command -v yay >/dev/null 2>&1; then
    yay -S --needed linux-wallpaperengine-git
  else
    echo "No AUR helper found. Install paru/yay, then: paru -S linux-wallpaperengine-git"
  fi
fi

echo "==> Installing app"
if [ -f "$DIR/wallpaper-hub.py" ]; then
  install -Dm755 "$DIR/wallpaper-hub.py" "$HOME/.local/bin/wallpaper-hub"
else
  echo "    downloading the latest release"
  mkdir -p "$HOME/.local/bin"
  curl -fsSL "https://github.com/$REPO/releases/latest/download/wallpaper-hub.py" -o "$HOME/.local/bin/wallpaper-hub.tmp"
  install -m755 "$HOME/.local/bin/wallpaper-hub.tmp" "$HOME/.local/bin/wallpaper-hub"
  rm -f "$HOME/.local/bin/wallpaper-hub.tmp"
fi

mkdir -p "$HOME/.local/share/applications"
cat > "$HOME/.local/share/applications/wallpaper-hub.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Wallpaper Hub
Comment=Run Wallpaper Engine wallpapers on Linux
Exec=$HOME/.local/bin/wallpaper-hub
Icon=preferences-desktop-wallpaper
Categories=Settings;DesktopSettings;
Terminal=false
EOF

echo "Done. Launch 'Wallpaper Hub' from your app menu, or run: wallpaper-hub"
echo "Future updates: Settings -> Updates inside the app."
