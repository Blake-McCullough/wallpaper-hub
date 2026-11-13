#!/usr/bin/env bash
# Wallpaper Hub installer for CachyOS / Arch
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"

echo "==> Installing Qt bindings"
sudo pacman -S --needed --noconfirm python-pyqt6

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
install -Dm755 "$DIR/wallpaper-hub.py" "$HOME/.local/bin/wallpaper-hub"
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
