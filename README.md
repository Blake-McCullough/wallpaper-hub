# Wallpaper Hub

A small GUI for browsing Wallpaper Engine wallpapers and setting them as your desktop background on CachyOS/KDE Plasma.
It has no renderer of its own: it hands the wallpaper to Plasma's built-in wallpaper system through the
[Wallpaper Engine for KDE](https://github.com/catsout/wallpaper-engine-kde-plugin) plugin.

## Install
From a clone: `chmod +x install.sh && ./install.sh`, or without cloning:
```bash
curl -fsSL https://github.com/Blake-McCullough/wallpaper-hub/releases/latest/download/install.sh | bash
```
Installs `python-pyqt6`, `python-pyqt6-webengine` (for signing in to Steam), the Plasma plugin
`wallpaper-engine-kde-plugin-git` (AUR; log out and in once afterwards), the app in `~/.local/bin`, and a menu entry.
`./install.sh --with-renderer` also installs the standalone `linux-wallpaperengine` (only needed off KDE).

## Updating
The app checks this repo's GitHub releases on startup. When a newer one exists a blue bar appears:
**Update now** downloads `wallpaper-hub.py`, verifies its checksum, replaces the installed copy and offers to
restart. Also under **Settings → Updates**. (Not available when running from a git checkout; use `git pull`.)
Releases are published automatically: bump `__version__` in `wallpaper-hub.py` and push to `main`.

## Prerequisites
- Wallpaper Engine owned and installed in Steam (needed for the assets folder).
- Wallpapers subscribed in the Steam Workshop (they download to `steamapps/workshop/content/431960`).

## Explore the Workshop
The **Explore Workshop** tab browses the whole Wallpaper Engine Workshop from inside the app: search, sort
(trending / most popular / most recent / recently updated), time period and paging. **Filters…** opens the
Workshop tag panel (Type, Age rating, Genre, Category, Special, plus any custom tag): click a tag to require it (✓)
or exclude it (✕), e.g. exclude Anime. Your filters are remembered.
Wallpapers you already have show a ✓.
- **Subscribe & download** subscribes you to the item on Steam (it shows in your Wallpaper Engine
  subscriptions) and Steam downloads it; it then appears in **My Library** automatically. The first time, a Steam
  sign-in window opens (Settings → Steam account). Steam must be running to do the download.
- **Set as desktop wallpaper** (Workshop item tab) applies it right away if you have it, otherwise downloads it
  and applies it the moment it arrives. Pick the monitor and scaling mode first.
- **steamcmd** is an optional extra: with it set up in Settings the file is also fetched immediately.
  Install `steamcmd`, sign in once in a terminal (`steamcmd +login YOURUSER`), enter the username in Settings.
- Settings has an optional **Steam Web API key** ([free](https://steamcommunity.com/dev/apikey)). With one,
  Explore uses Steam's official API; without one it reads the Workshop web pages, which Steam can change.

## Use
- Pick a wallpaper, choose a screen (or all), click **Apply to desktop (animated)**. Double-click also applies.
- **Layout**: choose a monitor under *Apply to* and a scaling mode (Default / Fill / Fit / Stretch; Default is
  Wallpaper Engine's default). Settings → *Display layout* switches between a wallpaper per monitor and the same
  wallpaper on every monitor.
- **Settings** tab: FPS, volume/mute, scaling, pause on fullscreen, mouse/parallax, re-apply on login.
- **Set as static desktop image** / **Set as lock screen image**: KDE Plasma and GNOME (static also XFCE). Lock screens are still images only.
- Closing the window hides it to the tray; the wallpaper keeps running independently.
- Config: `~/.config/wallpaper-hub/config.json`; renderer log: `~/.local/share/wallpaper-hub/engine.log`.

## Notes
- Scene and video wallpapers work best; web is partial; "application" wallpapers are Windows-only.
- Run `linux-wallpaperengine --help` if your build differs; flags are assembled in `build_command()`.
- On Plasma Wayland the renderer's layer may sit behind Plasma's own desktop. If you see no animation,
  try an X11 session or check the log.
