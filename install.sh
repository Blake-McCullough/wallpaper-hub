#!/usr/bin/env bash
# Wallpaper Hub installer for CachyOS / Arch.
# Works from a git checkout, or on its own:
#   curl -fsSL https://github.com/Blake-McCullough/wallpaper-hub/releases/latest/download/install.sh | bash
set -e
REPO="Blake-McCullough/wallpaper-hub"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || echo .)"

echo "==> Installing Qt bindings"
sudo pacman -S --needed --noconfirm python-pyqt6 python-pyqt6-webengine

# Plasma's own Wallpaper Engine plugin is what actually shows the wallpapers (no renderer of our own).
# (answers are read from the terminal, so this also works when piped from curl)
aur() {
  local in=/dev/stdin; [ -r /dev/tty ] && in=/dev/tty
  if command -v paru >/dev/null 2>&1; then paru -S --needed "$@" < "$in" || echo "!! AUR install failed or was cancelled: paru -S $*"
  elif command -v yay >/dev/null 2>&1; then yay -S --needed "$@" < "$in" || echo "!! AUR install failed or was cancelled: yay -S $*"
  else echo "No AUR helper found. Install paru/yay, then: paru -S $*"; fi
}
if [ ! -d /usr/share/plasma/wallpapers/com.github.catsout.wallpaperEngineKde ] && \
   [ ! -d "$HOME/.local/share/plasma/wallpapers/com.github.catsout.wallpaperEngineKde" ]; then
  echo "==> Installing the Wallpaper Engine plugin for Plasma"
  aur wallpaper-engine-kde-plugin-git
  echo "    (log out and back in once so Plasma loads the plugin)"
fi
# Optional standalone renderer for non-KDE desktops:  ./install.sh --with-renderer
if [ "$1" = "--with-renderer" ] && ! command -v linux-wallpaperengine >/dev/null 2>&1; then
  echo "==> Installing linux-wallpaperengine (optional renderer)"
  aur linux-wallpaperengine-git
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
