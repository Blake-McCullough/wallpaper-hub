# Wallpaper Hub

A small GUI for running Wallpaper Engine wallpapers on CachyOS/Linux.

## Install
```bash
chmod +x install.sh && ./install.sh
```
Installs `python-pyqt6`, the renderer `linux-wallpaperengine-git` (AUR), and a "Wallpaper Hub" menu entry.

## Prerequisites
- Wallpaper Engine owned and installed in Steam (needed for the assets folder).
- Wallpapers subscribed in the Steam Workshop (they download to `steamapps/workshop/content/431960`).

## Use
- Pick a wallpaper, choose a screen (or all), click **Apply to desktop (animated)**. Double-click also applies.
- **Settings** tab: FPS, volume/mute, scaling, pause on fullscreen, mouse/parallax, re-apply on login.
- **Set as static desktop image** / **Set as lock screen image**: KDE Plasma and GNOME (static also XFCE). Lock screens are still images only.
- Closing the window hides it to the tray; the wallpaper keeps running independently.
- Config: `~/.config/wallpaper-hub/config.json`; renderer log: `~/.local/share/wallpaper-hub/engine.log`.

## Notes
- Scene and video wallpapers work best; web is partial; "application" wallpapers are Windows-only.
- Run `linux-wallpaperengine --help` if your build differs; flags are assembled in `build_command()`.
- On Plasma Wayland the renderer's layer may sit behind Plasma's own desktop. If you see no animation,
  try an X11 session or check the log.
