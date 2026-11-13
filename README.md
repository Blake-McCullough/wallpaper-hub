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

## Explore the Workshop
The **Explore Workshop** tab browses the whole Wallpaper Engine Workshop from inside the app: search, sort
(trending / most popular / most recent / recently updated), time period and paging. **Filters…** opens the
Workshop tag panel (Type, Age rating, Genre, Category, Special, plus any custom tag): click a tag to require it (✓)
or exclude it (✕), e.g. exclude Anime. Your filters are remembered.
Wallpapers you already have show a ✓.
- **Subscribe in Steam** opens the item in your Steam client; press Subscribe there and Steam downloads it.
  It appears in **My Library** automatically once the download finishes.
- **Download directly (steamcmd)** is optional: install `steamcmd`, sign in once in a terminal
  (`steamcmd +login YOURUSER`), then enter the username in Settings.
- Settings has an optional **Steam Web API key** ([free](https://steamcommunity.com/dev/apikey)). With one,
  Explore uses Steam's official API; without one it reads the Workshop web pages, which Steam can change.

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
