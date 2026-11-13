#!/usr/bin/env python3
"""
Wallpaper Hub - a GUI for setting Wallpaper Engine wallpapers as your Linux desktop background.

* My Library: your subscribed / downloaded Wallpaper Engine wallpapers
* Explore: browse, search, sort and filter the whole Steam Workshop for Wallpaper Engine
* Sets them through Plasma's own Wallpaper Engine plugin (no renderer of its own);
  `linux-wallpaperengine` is only an optional fallback for other desktops
* Sets a static desktop image and the lock screen image (KDE Plasma, GNOME, XFCE for static)
* `wallpaper-hub --apply` re-applies the saved setup headlessly (used for autostart)
"""
import hashlib
import html as htmllib
import json
import math
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

APP = "wallpaper-hub"
WE_APPID = "431960"
CFG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / APP
DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / APP
CACHE_DIR = DATA_DIR / "cache"
CFG_FILE = CFG_DIR / "config.json"
PID_FILE = DATA_DIR / "engine.pid"
LOG_FILE = DATA_DIR / "engine.log"
AUTOSTART = Path.home() / ".config/autostart/wallpaper-hub.desktop"
__version__ = "1.3.5"
PLUGIN_ID = "com.github.catsout.wallpaperEngineKde"   # Wallpaper Engine for KDE (Plasma wallpaper plugin)
REPO = "Blake-McCullough/wallpaper-hub"
SCRIPT = Path(os.path.realpath(__file__))
UA = f"Mozilla/5.0 (X11; Linux x86_64) WallpaperHub/{__version__}"
PER_PAGE = 30

DEFAULTS = {
    "extra_libraries": [],
    "assets_dir": "",
    "assignments": {},          # screen name -> wallpaper folder
    "fps": 60,
    "volume": 50,
    "mute": False,
    "noautomute": False,
    "no_audio_processing": False,
    "pause_fullscreen": True,
    "disable_mouse": False,
    "disable_parallax": False,
    "scaling": "default",       # default scaling mode for monitors without their own setting
    "screen_scaling": {},       # monitor name -> default / fill / fit / stretch
    "backend": "auto",          # auto / plasma (Plasma's own wallpaper plugin) / renderer (linux-wallpaperengine)
    "layout": "independent",    # independent: a wallpaper per monitor, duplicate: same one on every monitor
    "clamp": "clamp",
    "autostart": False,
    "check_updates": True,
    "cfg_version": 3,
    "ex_include": ["Everyone"], # Explore: tags a wallpaper must have
    "ex_exclude": [],           # Explore: tags a wallpaper must NOT have (e.g. "Anime")
    "steam_api_key": "",        # optional: makes Explore far more reliable
    "steamcmd_user": "",        # optional: enables direct downloads without clicking Subscribe in Steam
}

# Explore filters --------------------------------------------------------------
SORTS = [("Trending", "trend"), ("Most popular", "totaluniquesubscribers"),
         ("Most recent", "mostrecent"), ("Recently updated", "lastupdated")]
PERIODS = [("Today", 1), ("This week", 7), ("This month", 30), ("3 months", 90),
           ("6 months", 180), ("This year", 365), ("All time", -1)]
# Tag groups as shown in Wallpaper Engine's Workshop filter panel. Each tag can be required or excluded.
FILTER_GROUPS = [
    ("Type", ["Scene", "Video", "Web", "Application"]),
    ("Age rating", ["Everyone", "Questionable", "Mature"]),
    ("Genre", ["Abstract", "Animal", "Anime", "Cartoon", "CGI", "Cyberpunk", "Fantasy", "Game", "Girls",
               "Guys", "Landscape", "Medieval", "Memes", "MMD", "Music", "Nature", "Pixel art", "Relaxing",
               "Retro", "Sci-Fi", "Sports", "Technology", "Television", "Vehicle", "Unspecified"]),
    ("Category", ["Wallpaper", "Preset", "Asset"]),
    ("Special", ["Approved", "Audio responsive", "3D", "Customizable", "Puppet Warp", "HDR",
                 "Media Integration", "User Shortcut", "Video Texture", "Asset Pack", "Miscellaneous"]),
]
API_QUERY_TYPE = {"trend": 3, "totaluniquesubscribers": 9, "mostrecent": 1, "lastupdated": 21}
API_TEXT_SEARCH = 12


# ----------------------------------------------------------------------------
# Core (no Qt needed)
# ----------------------------------------------------------------------------
def load_cfg():
    cfg = dict(DEFAULTS)
    try:
        saved = json.loads(CFG_FILE.read_text())
        if saved.get("cfg_version", 1) < 3 and saved.get("scaling") == "fill":
            saved["scaling"] = "default"    # 1.1 briefly defaulted to "fill"; Wallpaper Engine's default mode is "default"
        cfg.update(saved)
    except Exception:
        pass
    cfg["cfg_version"] = 3
    return cfg


def save_cfg(cfg):
    CFG_DIR.mkdir(parents=True, exist_ok=True)
    CFG_FILE.write_text(json.dumps(cfg, indent=2))
    try:
        CFG_FILE.chmod(0o600)   # may contain an API key
    except Exception:
        pass


def steam_apps_dirs():
    """All steamapps folders we can find (default install, flatpak, extra libraries)."""
    roots = [
        Path.home() / ".local/share/Steam",
        Path.home() / ".steam/steam",
        Path.home() / ".steam/root",
        Path.home() / ".var/app/com.valvesoftware.Steam/.local/share/Steam",
        Path.home() / ".var/app/com.valvesoftware.Steam/data/Steam",
    ]
    found, seen = [], set()

    def add(p):
        p = Path(p)
        if p.is_dir():
            real = p.resolve()
            if real not in seen:
                seen.add(real)
                found.append(real)

    for r in roots:
        add(r / "steamapps")
        vdf = r / "steamapps" / "libraryfolders.vdf"
        if vdf.is_file():
            try:
                for m in re.finditer(r'"path"\s+"([^"]+)"', vdf.read_text(errors="replace")):
                    add(Path(m.group(1)) / "steamapps")
            except Exception:
                pass
    return found


def library_info(cfg):
    projects, assets = [], ""
    for sa in steam_apps_dirs():
        for sub in (
            sa / "workshop/content" / WE_APPID,
            sa / "common/wallpaper_engine/projects/defaultprojects",
            sa / "common/wallpaper_engine/projects/myprojects",
        ):
            if sub.is_dir():
                projects.append(str(sub))
        a = sa / "common/wallpaper_engine/assets"
        if not assets and a.is_dir():
            assets = str(a)
    for extra in cfg.get("extra_libraries", []):
        if Path(extra).is_dir():
            projects.append(extra)
    return projects, (cfg.get("assets_dir") or assets)


def library_signature(paths):
    """Cheap fingerprint of installed wallpapers; changes when a download finishes."""
    sig = []
    for base in paths:
        try:
            for d in os.scandir(base):
                if os.path.isfile(os.path.join(d.path, "project.json")):
                    sig.append(d.name)
        except Exception:
            pass
    return tuple(sorted(sig))


def scan(paths):
    items = []
    for base in paths:
        try:
            dirs = sorted(Path(base).iterdir())
        except Exception:
            continue
        for d in dirs:
            pj = d / "project.json"
            if not pj.is_file():
                continue
            try:
                j = json.loads(pj.read_text(encoding="utf-8-sig", errors="replace"))
            except Exception:
                continue
            preview = d / str(j.get("preview", "preview.jpg"))
            if not preview.is_file():
                cands = sorted(d.glob("preview.*"))
                preview = cands[0] if cands else None
            items.append({
                "id": d.name,
                "dir": str(d),
                "title": str(j.get("title", d.name)),
                "type": str(j.get("type", "scene")).lower(),
                "file": str(j.get("file", "")),
                "tags": [str(t) for t in j.get("tags", [])] if isinstance(j.get("tags"), list) else [],
                "preview": str(preview) if preview else "",
            })
    return items


# ---- Workshop (online) -------------------------------------------------------
def http_get(url, timeout=20, limit=None):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        if limit:
            data = r.read(limit + 1)
            if len(data) > limit:
                raise ValueError("file too large")
            return data
        return r.read()


def cached_download(url, limit=None):
    """Download an image once; later calls read from the on-disk cache."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    f = CACHE_DIR / (hashlib.sha1(url.encode()).hexdigest() + ".img")
    if f.is_file() and f.stat().st_size > 0:
        return f.read_bytes()
    data = http_get(url, timeout=40 if limit else 20, limit=limit)
    f.write_bytes(data)
    return data


_STEAM_CDN = ("steamusercontent", "akamaihd", "steamstatic")


def thumb_url(u):
    """Small version for the grid. Steam's default thumbnails are letterboxed squares, so ask for a plain
    resize that keeps the picture's own shape."""
    if any(h in u for h in _STEAM_CDN):
        return u.split("?")[0] + "?imw=480&imh=270&ima=fit"
    return u


def full_url(u):
    """The original preview (an animated GIF for animated wallpapers)."""
    return u.split("?")[0] if any(h in u for h in _STEAM_CDN) else u


def cache_file(url):
    return CACHE_DIR / (hashlib.sha1(url.encode()).hexdigest() + ".img")


def query_tags(q):
    return [t for t in q.get("include", []) if t]


def query_excluded(q):
    return [t for t in q.get("exclude", []) if t]


def scrape_url(q):
    text = q.get("text", "").strip()
    sort = "textsearch" if text else q.get("sort", "trend")
    params = [("appid", WE_APPID), ("searchtext", text), ("childpublishedfileid", "0"),
              ("browsesort", sort), ("section", "readytouseitems")]
    for t in query_tags(q):
        params.append(("requiredtags[]", t))
    for t in query_excluded(q):
        params.append(("excludedtags[]", t))
    params += [("actualsort", sort), ("p", str(q.get("page", 1)))]
    if sort == "trend":
        params.append(("days", str(q.get("days", 7))))
    return "https://steamcommunity.com/workshop/browse/?" + urllib.parse.urlencode(params)


def api_url(q, key):
    text = q.get("text", "").strip()
    sort = q.get("sort", "trend")
    params = [("key", key), ("appid", WE_APPID), ("creator_appid", WE_APPID),
              ("page", str(q.get("page", 1))), ("numperpage", str(PER_PAGE)),
              ("query_type", str(API_TEXT_SEARCH if text else API_QUERY_TYPE.get(sort, 3))),
              ("search_text", text), ("match_all_tags", "true"), ("return_tags", "true"),
              ("return_short_description", "true"), ("return_vote_data", "true"), ("filetype", "0")]
    if sort == "trend" and not text:
        d = int(q.get("days", 7))
        params.append(("days", str(d if d > 0 else 3650)))
    for i, t in enumerate(query_tags(q)):
        params.append((f"requiredtags[{i}]", t))
    for i, t in enumerate(query_excluded(q)):
        params.append((f"excludedtags[{i}]", t))
    return "https://api.steampowered.com/IPublishedFileService/QueryFiles/v1/?" + urllib.parse.urlencode(params)


def _clean(text, limit=700):
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"\[/?[a-zA-Z0-9*=#\" ]+\]", " ", text)       # BBCode
    text = htmllib.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit] + ("…" if len(text) > limit else "")


def parse_api(raw):
    r = json.loads(raw).get("response", {})
    items = []
    for f in r.get("publishedfiledetails", []) or []:
        if str(f.get("result", 1)) not in ("1",) and "title" not in f:
            continue
        tags = [t.get("display_name") or t.get("tag", "") for t in f.get("tags", []) or []]
        items.append({
            "id": str(f.get("publishedfileid", "")),
            "title": f.get("title") or str(f.get("publishedfileid", "")),
            "author": "",
            "preview_url": f.get("preview_url", ""),
            "desc": _clean(f.get("short_description") or f.get("file_description", "")),
            "subs": int(f.get("subscriptions", 0) or 0),
            "tags": [t for t in tags if t],
        })
    return [i for i in items if i["id"]], int(r.get("total", len(items)) or 0)


def parse_workshop_html(page):
    """Parse a steamcommunity.com workshop browse page. Tolerant: several strategies are merged."""
    items, order = {}, []

    def entry(i):
        if i not in items:
            items[i] = {"id": i, "title": "", "author": "", "preview_url": "", "desc": "", "subs": 0, "tags": []}
            order.append(i)
        return items[i]

    # 1) hover-tooltip JSON that Steam embeds for each item (title + description)
    dec = json.JSONDecoder()
    for m in re.finditer(r'SharedFileBindMouseHover\(\s*"sharedfile_(\d+)"\s*,\s*(?:true|false)\s*,\s*', page):
        try:
            obj, _ = dec.raw_decode(page, m.end())
        except Exception:
            continue
        e = entry(m.group(1))
        if isinstance(obj, dict):
            e["title"] = e["title"] or htmllib.unescape(str(obj.get("title", "")))
            e["desc"] = e["desc"] or _clean(str(obj.get("description", "")))

    # 2) the item blocks themselves
    for block in re.split(r'<div[^>]+class="[^"]*\bworkshopItem\b[^"]*"', page)[1:]:
        m = re.search(r'data-publishedfileid="(\d+)"', block) or re.search(r"filedetails/\?id=(\d+)", block)
        if not m:
            continue
        e = entry(m.group(1))
        img = re.search(r'<img[^>]+class="[^"]*workshopItemPreviewImage[^"]*"[^>]+src="([^"]+)"', block) \
            or re.search(r'<img[^>]+src="(https?://[^"]+)"', block)
        if img and not e["preview_url"]:
            e["preview_url"] = htmllib.unescape(img.group(1))
        t = re.search(r'class="[^"]*workshopItemTitle[^"]*"[^>]*>(.*?)</div>', block, re.S)
        if t and not e["title"]:
            e["title"] = _clean(t.group(1), 200)
        a = re.search(r'class="[^"]*workshopItemAuthorName[^"]*"[^>]*>(.*?)</div>', block, re.S)
        if a:
            e["author"] = re.sub(r"^\s*by\s*", "", _clean(a.group(1), 80), flags=re.I)

    # 3) fallback: bare anchors to item pages
    for m in re.finditer(r'<a[^>]+href="[^"]*filedetails/\?id=(\d+)[^"]*"[^>]*>(.*?)</a>', page, re.S):
        inner = m.group(2)
        img = re.search(r'<img[^>]+src="(https?://[^"]+)"', inner)
        alt = re.search(r'<img[^>]+alt="([^"]*)"', inner)
        if not (img or _clean(inner)):
            continue
        e = entry(m.group(1))
        if img and not e["preview_url"]:
            e["preview_url"] = htmllib.unescape(img.group(1))
        if not e["title"]:
            e["title"] = _clean(inner, 200) or (htmllib.unescape(alt.group(1)) if alt else "")
    for e in items.values():
        if re.search(r" by .+$", e["title"]) and e["author"]:
            e["title"] = e["title"][: -len(" by " + e["author"])].rstrip() if e["title"].endswith(e["author"]) else e["title"]
        e["title"] = e["title"] or e["id"]

    total = 0
    m = re.search(r"([\d,]+)\s+entries", page)
    if m:
        total = int(m.group(1).replace(",", ""))
    out = [items[i] for i in order if items[i]["preview_url"] or items[i]["title"] != i]
    return out, total or len(out)


def search_workshop(q, cfg):
    """Returns (items, total). Uses the Steam Web API if a key is set, otherwise scrapes the Workshop."""
    key = (cfg.get("steam_api_key") or "").strip()
    try:
        if key:
            return parse_api(http_get(api_url(q, key)))
        return parse_workshop_html(http_get(scrape_url(q)).decode("utf-8", "replace"))
    except Exception as e:
        msg = str(e)
        if key:
            msg = msg.replace(key, "***")
        raise RuntimeError(msg) from None


def workshop_page_url(wid):
    return f"https://steamcommunity.com/sharedfiles/filedetails/?id={wid}"


# ---- Self update from GitHub releases ----------------------------------------
def parse_version(s):
    return tuple(int(x) for x in re.findall(r"\d+", str(s))[:4]) or (0,)


def latest_release():
    try:
        data = json.loads(http_get(f"https://api.github.com/repos/{REPO}/releases/latest").decode())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise RuntimeError("No release found (is the GitHub repo public and does it have a release?)") from None
        raise RuntimeError(f"GitHub said {e.code}") from None
    return {
        "version": str(data.get("tag_name", "")).lstrip("vV"),
        "notes": data.get("body") or "",
        "url": data.get("html_url", ""),
        "assets": {a["name"]: a["browser_download_url"] for a in data.get("assets", [])},
    }


def update_blocker():
    """Why this copy of the app can't update itself, or '' if it can."""
    if (SCRIPT.parent / ".git").exists():
        return "Running from a git checkout: update with `git pull` instead."
    if not os.access(SCRIPT.parent, os.W_OK):
        return f"{SCRIPT.parent} isn't writable: re-run install.sh from the new release."
    return ""


def install_update(rel):
    """Download the release's wallpaper-hub.py, verify it, and replace the running script."""
    why = update_blocker()
    if why:
        raise RuntimeError(why)
    url = rel["assets"].get("wallpaper-hub.py")
    if not url:
        raise RuntimeError("That release has no wallpaper-hub.py file attached.")
    data = http_get(url, timeout=60)
    sha_url = rel["assets"].get("wallpaper-hub.py.sha256")
    if sha_url:
        want = http_get(sha_url).decode().split()[0].lower()
        if hashlib.sha256(data).hexdigest() != want:
            raise RuntimeError("Checksum mismatch, update aborted.")
    compile(data, "wallpaper-hub.py", "exec")           # refuse a file that doesn't even parse
    m = re.search(rb'^__version__ = "([^"]+)"', data, re.M)
    if not m or parse_version(m.group(1).decode()) < parse_version(__version__):
        raise RuntimeError("Downloaded file isn't a newer Wallpaper Hub.")
    tmp = SCRIPT.with_name(SCRIPT.name + ".new")
    tmp.write_bytes(data)
    tmp.chmod(0o755)
    try:
        shutil.copy2(SCRIPT, SCRIPT.with_name(SCRIPT.name + ".bak"))
    except Exception:
        pass
    os.replace(tmp, SCRIPT)
    return m.group(1).decode()


# ---- Renderer / desktop integration -----------------------------------------
def engine_path():
    return shutil.which("linux-wallpaperengine")


_flags_cache = {}


def engine_flags():
    """Options the installed linux-wallpaperengine understands (its CLI changes between versions)."""
    exe = engine_path()
    if not exe:
        return set()
    if exe not in _flags_cache:
        try:
            r = subprocess.run([exe, "--help"], capture_output=True, text=True, timeout=10)
            _flags_cache[exe] = set(re.findall(r"--[a-z][a-z0-9-]*", r.stdout + r.stderr))
        except Exception:
            _flags_cache[exe] = set()
    return _flags_cache[exe]


def build_command(cfg):
    exe = engine_path()
    if not exe or not cfg["assignments"]:
        return None
    flags = engine_flags()
    _, assets = library_info(cfg)
    cmd = [exe]

    def has(flag):
        return not flags or flag in flags      # unknown help output: be permissive

    def opt(flag, *args):
        if has(flag):
            cmd.extend([flag, *map(str, args)])

    opt("--fps", cfg["fps"])
    if cfg["mute"]:
        opt("--silent")
    else:
        opt("--volume", cfg["volume"])
    if cfg["noautomute"]:
        opt("--noautomute")
    if cfg["no_audio_processing"]:
        opt("--no-audio-processing")
    if not cfg["pause_fullscreen"]:
        opt("--no-fullscreen-pause")
    if cfg["disable_mouse"]:
        opt("--disable-mouse")
    if cfg["disable_parallax"]:
        opt("--disable-parallax")
    if assets:
        opt("--assets-dir", assets)

    def scaling_of(screen):
        return cfg.get("screen_scaling", {}).get(screen, cfg["scaling"])

    def per_screen_opts(screen):
        if scaling_of(screen) != "default":
            opt("--scaling", scaling_of(screen))
        if cfg["clamp"] != "clamp":
            opt("--clamp", cfg["clamp"])

    if has("--bg"):
        for screen, wdir in cfg["assignments"].items():
            cmd += ["--screen-root", screen, "--bg", wdir]
            per_screen_opts(screen)
    else:   # older builds: one wallpaper shared by all listed screens, given as the last argument
        first = next(iter(cfg["assignments"]))
        for screen in cfg["assignments"]:
            cmd += ["--screen-root", screen]
        per_screen_opts(first)
        cmd.append(cfg["assignments"][first])
    return cmd


def log_tail(n=15):
    try:
        return "\n".join(LOG_FILE.read_text(errors="replace").splitlines()[-n:])
    except Exception:
        return ""


def session_hint():
    if os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland" and "kde" in desktop_env():
        return ("You're on Plasma (Wayland). If the wallpaper runs but you can't see it, Plasma's own wallpaper "
                "may be drawn over it; the reliable fix is logging out and choosing “Plasma (X11)” at the login screen.")
    return ""


def diagnostics():
    cfg = load_cfg()
    cmd = build_command(cfg)
    lines = [f"Session: {os.environ.get('XDG_SESSION_TYPE', '?')}   Desktop: {os.environ.get('XDG_CURRENT_DESKTOP', '?')}",
             f"Backend in use: {resolve_backend(cfg)}   (Plasma plugin installed: {'yes' if plugin_installed() else 'no'})",
             "Plasma plugin settings stored:", plasma_config_excerpt(), "",
             f"Renderer (optional fallback): {engine_path() or 'not installed'}",
             f"Options it supports: {' '.join(sorted(engine_flags())) or 'unknown'}",
             "Command: " + (shlex.join(cmd) if cmd else "(no wallpaper assigned yet)"), "",
             "--- engine.log (last 80 lines) ---"]
    try:
        lines += LOG_FILE.read_text(errors="replace").splitlines()[-80:]
    except Exception:
        lines.append("(no log yet)")
    return "\n".join(lines)


def engine_pid():
    try:
        pid = int(PID_FILE.read_text().strip())
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
        if b"linux-wallpaperengine" in cmdline:
            return pid
    except Exception:
        pass
    return None


def plugin_installed():
    return any((base / PLUGIN_ID).is_dir() for base in (Path.home() / ".local/share/plasma/wallpapers",
                                                         Path("/usr/share/plasma/wallpapers")))


def resolve_backend(cfg):
    """'plasma' = hand the wallpaper to Plasma's own Wallpaper Engine plugin; 'renderer' = run linux-wallpaperengine."""
    b = cfg.get("backend", "auto")
    if b == "auto":
        return "plasma" if ("kde" in desktop_env() and plugin_installed()) else "renderer"
    return b


IMAGE_EXT = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tga"}      # (.gif moves, so it counts as animated)


def animated_available(cfg):
    b = resolve_backend(cfg)
    return plugin_installed() if b == "plasma" else bool(engine_path())


def plan_for(it, cfg):
    """Decide how a wallpaper should be applied: ('animated' | 'static', explanation)."""
    suffix = Path(it.get("file", "")).suffix.lower()
    if it["type"] == "image" or (it["type"] not in ("scene", "video", "web") and suffix in IMAGE_EXT):
        return "static", "A still picture, so it's set as a normal wallpaper."
    if it["type"] == "application":
        return "static", "Application wallpapers are Windows-only, so its preview picture is used instead."
    if animated_available(cfg):
        how = "Plasma's Wallpaper Engine plugin" if resolve_backend(cfg) == "plasma" else "linux-wallpaperengine"
        return "animated", f"Animated: applied through {how}."
    return "static", ("Nothing is installed to play animated wallpapers, so a still frame is used. "
                      "Install wallpaper-engine-kde-plugin-git for animation.")


def kde_each(geo, body):
    """Plasma script that runs `body` (where `d` is a desktop) on the desktop of one monitor, or on every desktop
    when geo is None. geo = (x, y, w, h, connector_name). The monitor is found by its connector name (Plasma's
    screenForConnector) and, if that isn't available, by the nearest centre, which survives Qt and Plasma
    disagreeing by a pixel."""
    if geo:
        name = json.dumps(geo[4]) if len(geo) > 4 else "\"\""
        target = "{cx:%s,cy:%s}" % (geo[0] + geo[2] / 2, geo[1] + geo[3] / 2)
    else:
        name, target = "\"\"", "null"
    return ("var T=" + target + ";var N=" + name + ";"
            "var bn=(N&&typeof screenForConnector==='function')?screenForConnector(N):-1;"
            "var ds=desktops();var L=[];var min=1e18;"
            "for(var i=0;i<ds.length;i++){var s=ds[i].screen;if(s<0)continue;var g=screenGeometry(s);"
            "var dist=!T?0:(bn>=0?(s==bn?0:1e12):Math.pow(g.x+g.width/2-T.cx,2)+Math.pow(g.y+g.height/2-T.cy,2));"
            "L.push([ds[i],dist]);if(dist<min)min=dist;}"
            "var n=0;for(var k=0;k<L.length;k++){if(L[k][1]<=min+1){var d=L[k][0];" + body + "n++;}}"
            "n>0?\"ok\":\"nomatch\";")


def kde_eval(js):
    qd = shutil.which("qdbus6") or shutil.which("qdbus") or shutil.which("qdbus-qt6")
    if not qd:
        return False, "qdbus6 not found (it ships with Plasma's Qt tools)."
    ok, out = run([qd, "org.kde.plasmashell", "/PlasmaShell", "org.kde.PlasmaShell.evaluateScript", js])
    if not ok:
        return False, out or "Plasma didn't accept the request (is plasmashell running?)."
    if "nomatch" in out:
        return False, "Couldn't match that monitor in Plasma."
    return True, "ok"


def kde_apply_plugin(wdir, geo=None, scaling="default"):
    """Make Plasma's Wallpaper Engine plugin show `wdir` on one monitor (geo = x,y,w,h) or on all monitors."""
    if not plugin_installed():
        return False, ("The Wallpaper Engine plugin for Plasma isn't installed.\n\n"
                       "Install it with:  paru -S wallpaper-engine-kde-plugin-git\n"
                       "then restart Plasma (log out and in).")
    src, lib, wid = plugin_values(wdir)
    body = ("d.wallpaperPlugin=" + json.dumps(PLUGIN_ID) + ";"
            "d.currentConfigGroup=[\"Wallpaper\"," + json.dumps(PLUGIN_ID) + ",\"General\"];"
            "d.writeConfig(\"SteamLibraryPath\"," + json.dumps(lib) + ");"
            "d.writeConfig(\"WallpaperSource\"," + json.dumps(src) + ");"
            "d.writeConfig(\"WallpaperWorkShopId\"," + json.dumps(wid) + ");"
            "d.reloadConfig();")
    return kde_eval(kde_each(geo, body))


def plugin_values(wdir):
    """The three settings the Wallpaper Engine plugin stores for a wallpaper, in the format it writes itself:
    SteamLibraryPath=<steam root>, WallpaperSource=file:<media file>+<type>, WallpaperWorkShopId=<id>."""
    d = Path(wdir)
    try:
        j = json.loads((d / "project.json").read_text(encoding="utf-8-sig", errors="replace"))
    except Exception:
        j = {}
    kind = str(j.get("type", "scene")).lower()
    f = str(j.get("file", ""))
    media = d / f if f else d
    if kind == "scene" and not media.is_file() and (d / "scene.pkg").is_file():
        media = d / "scene.pkg"                     # scenes are shipped packed
    parts = d.parts
    if "steamapps" in parts:
        lib = str(Path(*parts[:parts.index("steamapps")]))
    else:
        dirs = steam_apps_dirs()
        lib = str(dirs[0].parent) if dirs else str(Path.home() / ".local/share/Steam")
    return f"file:{media}+{kind}", lib, d.name


def kde_stop_plugin():
    """Go back to Plasma's normal picture wallpaper on every monitor that is using the Wallpaper Engine plugin."""
    js = ("var ds=desktops();for(var i=0;i<ds.length;i++){var d=ds[i];if(d.wallpaperPlugin==" + json.dumps(PLUGIN_ID) +
          "){d.wallpaperPlugin=\"org.kde.image\";d.reloadConfig();}}\"ok\";")
    return kde_eval(js)


def plasma_config_excerpt():
    """The lines Plasma stored for the plugin, for diagnostics."""
    f = Path.home() / ".config/plasma-org.kde.plasma.desktop-appletsrc"
    try:
        lines = f.read_text(errors="replace").splitlines()
    except Exception:
        return "(couldn't read plasma-org.kde.plasma.desktop-appletsrc)"
    out, show = [], False
    for ln in lines:
        if ln.startswith("["):
            show = "wallpaperEngineKde" in ln
        if show:
            out.append(ln)
    return "\n".join(out) or "(no Wallpaper Engine plugin section stored yet)"


def stop_engine():
    run(["pkill", "-x", "linux-wallpaperengine"], timeout=5)      # also catches renderers left over from older runs
    pid = engine_pid()
    if pid:
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except Exception:
            try:
                os.kill(pid, signal.SIGTERM)
            except Exception:
                pass
    try:
        PID_FILE.unlink()
    except Exception:
        pass


def start_engine(cfg):
    if not engine_path():
        return False, "linux-wallpaperengine is not installed (run install.sh)."
    cmd = build_command(cfg)
    if not cmd:
        return False, "No wallpaper assigned to any screen yet."
    stop_engine()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    log = open(LOG_FILE, "w")
    log.write("$ " + shlex.join(cmd) + "\n")
    log.flush()
    p = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    log.close()
    PID_FILE.write_text(str(p.pid))
    time.sleep(1.5)
    if p.poll() is not None:        # crashed straight away: show why instead of silently doing nothing
        try:
            PID_FILE.unlink()
        except Exception:
            pass
        return False, ("linux-wallpaperengine exited immediately.\n\n" + log_tail(14) +
                       "\n\n(Settings → “Show engine log” has the full command and log.)")
    return True, f"Started (pid {p.pid}). Log: {LOG_FILE}"


def run(cmd, timeout=15):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, (r.stdout + r.stderr).strip()
    except Exception as e:
        return False, str(e)


def desktop_env():
    return os.environ.get("XDG_CURRENT_DESKTOP", "").lower()


def kde_set_on_screen(path, geo=None):
    """Set Plasma's normal picture wallpaper on one monitor (geo = x, y, w, h) or on all of them.
    Also switches the monitor away from the animated Wallpaper Engine plugin."""
    body = ("d.wallpaperPlugin=\"org.kde.image\";"
            "d.currentConfigGroup=[\"Wallpaper\",\"org.kde.image\",\"General\"];"
            "d.writeConfig(\"Image\"," + json.dumps(Path(path).as_uri()) + ");d.reloadConfig();")
    return kde_eval(kde_each(geo, body))


def set_static_desktop(path, geo=None):
    """geo=(x, y, w, h) targets one monitor (Plasma only); None sets every monitor."""
    de = desktop_env()
    uri = Path(path).as_uri()
    if "kde" in de:
        ok, msg = kde_set_on_screen(path, geo)
        if ok or geo:
            return ok, msg
        tool = shutil.which("plasma-apply-wallpaperimage")      # all monitors: fall back to KDE's own tool
        if not tool:
            return False, msg
        return run([tool, path])
    note = " (this desktop only supports one image for all monitors)" if geo else ""
    if "gnome" in de or "cinnamon" in de or "budgie" in de:
        ok1, m1 = run(["gsettings", "set", "org.gnome.desktop.background", "picture-uri", uri])
        ok2, m2 = run(["gsettings", "set", "org.gnome.desktop.background", "picture-uri-dark", uri])
        return ok1 and ok2, (m1 + m2).strip() or "ok" + note
    if "xfce" in de:
        ok, out = run(["xfconf-query", "-c", "xfce4-desktop", "-l"])
        if not ok:
            return False, out
        for prop in [l for l in out.splitlines() if l.endswith("last-image")]:
            run(["xfconf-query", "-c", "xfce4-desktop", "-p", prop, "-s", path])
        return True, "ok" + note
    return False, f"Static wallpaper isn't wired up for desktop '{de or 'unknown'}'."


def set_lockscreen(path):
    de = desktop_env()
    uri = Path(path).as_uri()
    if "kde" in de:
        kw = shutil.which("kwriteconfig6") or shutil.which("kwriteconfig5")
        if not kw:
            return False, "kwriteconfig6 not found."
        steps = [
            [kw, "--file", "kscreenlockerrc", "--group", "Greeter", "--key", "WallpaperPlugin", "org.kde.image"],
            [kw, "--file", "kscreenlockerrc", "--group", "Greeter", "--group", "Wallpaper",
             "--group", "org.kde.image", "--group", "General", "--key", "Image", uri],
        ]
        for s in steps:
            ok, m = run(s)
            if not ok:
                return False, m
        return True, "ok"
    if "gnome" in de:
        ok, m = run(["gsettings", "set", "org.gnome.desktop.screensaver", "picture-uri", uri])
        return ok, m or "ok (newer GNOME versions may ignore this key)"
    return False, f"Lock screen isn't wired up for desktop '{de or 'unknown'}'."


def set_autostart(on):
    if on:
        AUTOSTART.parent.mkdir(parents=True, exist_ok=True)
        script = os.path.realpath(__file__)
        AUTOSTART.write_text(
            "[Desktop Entry]\nType=Application\nName=Wallpaper Hub (apply)\n"
            f"Exec=sh -c 'sleep 4 && python3 \"{script}\" --apply'\n"
            "X-GNOME-Autostart-enabled=true\n"
        )
    else:
        try:
            AUTOSTART.unlink()
        except Exception:
            pass


# ----------------------------------------------------------------------------
# Headless mode (autostart)
# ----------------------------------------------------------------------------
if __name__ == "__main__" and "--apply" in sys.argv:
    ok, msg = start_engine(load_cfg())
    print(msg)
    sys.exit(0 if ok else 1)


# ----------------------------------------------------------------------------
# GUI
# ----------------------------------------------------------------------------
from PyQt6.QtCore import QObject, Qt, QSize, QThread, QTimer, QUrl, pyqtSignal  # noqa: E402
from PyQt6.QtGui import QColor, QDesktopServices, QIcon, QImage, QImageReader, QMovie, QPixmap  # noqa: E402
from PyQt6.QtWidgets import (  # noqa: E402
    QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout, QGridLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QListView, QListWidget, QListWidgetItem, QMainWindow, QMenu,
    QMessageBox, QPushButton, QScrollArea, QSlider, QSpinBox, QSplitter, QSystemTrayIcon, QTabWidget,
    QVBoxLayout, QWidget,
)

try:    # optional: lets the app sign in to Steam's website so it can subscribe to Workshop items for you
    from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile  # noqa: E402
    from PyQt6.QtWebEngineWidgets import QWebEngineView  # noqa: E402
    HAVE_WEB = True
except Exception:
    HAVE_WEB = False

SCALINGS = [("Default", "default"), ("Fill", "fill"), ("Fit", "fit"), ("Stretch", "stretch")]
UR = Qt.ItemDataRole.UserRole
THUMB_W, THUMB_H = 220, 124
CELL = QSize(240, 184)
ITEM = QSize(232, 178)
_placeholder = None


def placeholder_icon():
    global _placeholder
    if _placeholder is None:
        pm = QPixmap(THUMB_W, THUMB_H)
        pm.fill(QColor(128, 128, 128, 45))
        _placeholder = QIcon(pm)
    return _placeholder


def thumb(img):
    """Scale-to-cover and centre-crop so every card has the same size."""
    s = img.scaled(THUMB_W, THUMB_H, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                   Qt.TransformationMode.SmoothTransformation)
    return s.copy((s.width() - THUMB_W) // 2, (s.height() - THUMB_H) // 2, THUMB_W, THUMB_H)


def make_grid():
    g = QListWidget()
    g.setViewMode(QListView.ViewMode.IconMode)
    g.setResizeMode(QListView.ResizeMode.Adjust)
    g.setMovement(QListView.Movement.Static)
    g.setIconSize(QSize(THUMB_W, THUMB_H))
    g.setGridSize(CELL)
    g.setUniformItemSizes(False)       # sizes are set per item, see grid_item()
    g.setWordWrap(True)
    g.setTextElideMode(Qt.TextElideMode.ElideRight)
    g.setSpacing(2)
    g.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    return g


def grid_item(text, tooltip, data):
    li = QListWidgetItem(placeholder_icon(), text)
    li.setSizeHint(ITEM)               # fixed size: otherwise cards collapse before thumbnails arrive
    li.setToolTip(tooltip)
    li.setData(UR, data)
    return li


class ImageLoader(QThread):
    """Loads thumbnails from local files or URLs (cached on disk) without blocking the UI."""
    loaded = pyqtSignal(int, int, QImage)

    def __init__(self, jobs, gen):
        super().__init__()
        self.jobs, self.gen = jobs, gen

    def run(self):
        from concurrent.futures import ThreadPoolExecutor

        def work(job):
            row, src = job
            if self.isInterruptionRequested() or not src:
                return
            try:
                img = QImage.fromData(cached_download(src)) if src.startswith("http") else QImage(src)
            except Exception:
                return
            if not img.isNull():
                self.loaded.emit(self.gen, row, thumb(img))

        with ThreadPoolExecutor(6) as ex:
            list(ex.map(work, self.jobs))


class PreviewLabel(QLabel):
    """Preview box that keeps the picture's own aspect ratio and plays animated GIF/WebP previews."""
    BOX = QSize(340, 192)

    def __init__(self, text=""):
        super().__init__(text)
        self.mv = None
        self.setMinimumSize(self.BOX)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setStyleSheet("background: rgba(128,128,128,40); border-radius: 6px;")

    def reset(self, text=""):
        if self.mv:
            self.mv.stop()
            self.mv.deleteLater()
            self.mv = None
        self.clear()
        self.setText(text)

    def show_file(self, path):
        self.reset("No preview")
        if not path or not os.path.isfile(path):
            return
        rd = QImageReader(path)
        size = rd.size()
        if rd.supportsAnimation() and rd.imageCount() > 1:
            m = QMovie(path)
            if size.isValid():
                size.scale(self.BOX, Qt.AspectRatioMode.KeepAspectRatio)
                m.setScaledSize(size)
            m.setCacheMode(QMovie.CacheMode.CacheAll)
            self.setMovie(m)
            m.start()
            self.mv = m
            return
        pm = QPixmap(path)
        if not pm.isNull():
            self.setPixmap(pm.scaled(self.BOX, Qt.AspectRatioMode.KeepAspectRatio,
                                     Qt.TransformationMode.SmoothTransformation))


class DetailLoader(QThread):
    """Fetches the full-size (possibly animated) preview of the selected Workshop item."""
    ready = pyqtSignal(int, str)

    def __init__(self, url, gen):
        super().__init__()
        self.url, self.gen = url, gen

    def run(self):
        try:
            cached_download(self.url, limit=30 * 1024 * 1024)
            self.ready.emit(self.gen, self.url)
        except Exception:
            pass        # the small thumbnail stays on screen


class SearchThread(QThread):
    done = pyqtSignal(int, list, int, str)

    def __init__(self, q, cfg, gen):
        super().__init__()
        self.q, self.cfg, self.gen = q, dict(cfg), gen

    def run(self):
        try:
            items, total = search_workshop(self.q, self.cfg)
            self.done.emit(self.gen, items, total, "")
        except Exception as e:
            self.done.emit(self.gen, [], 0, str(e))


class SteamCmdThread(QThread):
    line = pyqtSignal(str)
    done = pyqtSignal(bool, str)

    def __init__(self, exe, user, root, wid):
        super().__init__()
        self.exe, self.user, self.root, self.wid = exe, user, str(root), wid

    def run(self):
        cmd = [self.exe, "+force_install_dir", self.root, "+login", self.user,
               "+workshop_download_item", WE_APPID, self.wid, "validate", "+quit"]
        try:
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                 stdin=subprocess.DEVNULL)
            out = []
            for ln in p.stdout:
                ln = ln.strip()
                if ln:
                    out.append(ln)
                    self.line.emit(ln[:140])
            p.wait()
            text = "\n".join(out)
            if "Success. Downloaded item" in text:
                self.done.emit(True, "Downloaded.")
            elif "Login Failure" in text or "FAILED" in text.upper() and "login" in text.lower():
                self.done.emit(False, f"steamcmd couldn't log in. Run `steamcmd +login {self.user}` once in a "
                                      "terminal to sign in (and approve Steam Guard), then try again.")
            else:
                self.done.emit(False, (out[-1] if out else "steamcmd produced no output"))
        except Exception as e:
            self.done.emit(False, str(e))


class UpdateCheckThread(QThread):
    done = pyqtSignal(bool, dict, str)

    def run(self):
        try:
            self.done.emit(True, latest_release(), "")
        except Exception as e:
            self.done.emit(False, {}, str(e))


class UpdateInstallThread(QThread):
    done = pyqtSignal(bool, str)

    def __init__(self, rel):
        super().__init__()
        self.rel = rel

    def run(self):
        try:
            self.done.emit(True, install_update(self.rel))
        except Exception as e:
            self.done.emit(False, str(e))


SUBSCRIBE_JS = """(function(){window.__wh=null;
var m=document.cookie.match(/(?:^|; )sessionid=([^;]+)/);var sid=m?decodeURIComponent(m[1]):(window.g_sessionID||'');
if(!sid){window.__wh='ERR:no Steam session found (sign in again)';return;}
var body='id=__ID__&appid=431960&sessionid='+encodeURIComponent(sid);
fetch('/sharedfiles/subscribe',{method:'POST',credentials:'same-origin',
 headers:{'Content-Type':'application/x-www-form-urlencoded; charset=UTF-8'},body:body})
.then(function(r){return r.text().then(function(t){
 window.__wh=(r.ok&&/"success"\\s*:\\s*(1|true)/.test(t))?'OK':'ERR:Steam answered '+r.status+' '+t.slice(0,160);});})
.catch(function(e){window.__wh='ERR:'+e;});})();"""


class SteamWeb(QObject):
    """Signs in to steamcommunity.com (cookies kept in the app's own profile) and subscribes to items."""
    login_changed = pyqtSignal(bool)
    subscribed = pyqtSignal(str, bool, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.logged_in = False
        self.pages = set()
        (DATA_DIR / "web").mkdir(parents=True, exist_ok=True)
        self.profile = QWebEngineProfile("wallpaperhub", self)
        self.profile.setPersistentStoragePath(str(DATA_DIR / "web"))
        self.profile.setCachePath(str(DATA_DIR / "web" / "cache"))
        self.profile.setPersistentCookiesPolicy(QWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies)
        cs = self.profile.cookieStore()
        cs.cookieAdded.connect(self.cookie_added)
        cs.cookieRemoved.connect(self.cookie_removed)
        cs.loadAllCookies()

    @staticmethod
    def is_login_cookie(c):
        return bytes(c.name()).decode(errors="ignore") == "steamLoginSecure" and "steamcommunity" in c.domain()

    def cookie_added(self, c):
        if self.is_login_cookie(c) and not self.logged_in:
            self.logged_in = True
            self.login_changed.emit(True)

    def cookie_removed(self, c):
        if self.is_login_cookie(c) and self.logged_in:
            self.logged_in = False
            self.login_changed.emit(False)

    def sign_out(self):
        self.profile.cookieStore().deleteAllCookies()
        self.logged_in = False
        self.login_changed.emit(False)

    def subscribe(self, wid):
        page = QWebEnginePage(self.profile, self)
        self.pages.add(page)
        timer = QTimer(self)
        timer.setInterval(400)
        state = {"n": 0, "done": False}

        def finish(ok, msg):
            if state["done"]:
                return
            state["done"] = True
            timer.stop()
            self.pages.discard(page)
            page.deleteLater()
            self.subscribed.emit(wid, ok, msg)

        def got(r):
            if r:
                finish(r == "OK", "" if r == "OK" else str(r).removeprefix("ERR:"))

        def poll():
            state["n"] += 1
            if state["n"] > 40:
                finish(False, "Timed out talking to Steam.")
            else:
                page.runJavaScript("window.__wh||null", got)

        def loaded(ok):
            if not ok:
                finish(False, "Couldn't load the Workshop page.")
                return
            page.runJavaScript(SUBSCRIBE_JS.replace("__ID__", wid))
            timer.timeout.connect(poll)
            timer.start()

        page.loadFinished.connect(loaded)
        page.load(QUrl(workshop_page_url(wid)))


class SteamLoginDialog(QDialog):
    def __init__(self, parent, web):
        super().__init__(parent)
        self.setWindowTitle("Sign in to Steam")
        self.resize(560, 720)
        v = QVBoxLayout(self)
        v.addWidget(QLabel("Sign in with the Steam account that owns Wallpaper Engine. Your password goes "
                           "straight to Steam; this app only keeps the login cookie on this computer."))
        self.view = QWebEngineView()
        self.view.setPage(QWebEnginePage(web.profile, self.view))
        self.view.load(QUrl("https://steamcommunity.com/login/home/?goto=%2Fsharedfiles%2F"))
        v.addWidget(self.view, 1)
        web.login_changed.connect(lambda ok: self.accept() if ok else None)


class TagButton(QPushButton):
    """Click cycles: off -> required (green ✓) -> excluded (red ✕) -> off."""
    changed = pyqtSignal()
    STYLES = {
        0: "",
        1: "background:#2e7d32;color:white;font-weight:bold;",
        2: "background:#b71c1c;color:white;font-weight:bold;",
    }

    def __init__(self, tag, state=0):
        super().__init__()
        self.tag, self.state = tag, state
        self.clicked.connect(self.cycle)
        self.paint()

    def cycle(self):
        self.state = (self.state + 1) % 3
        self.paint()
        self.changed.emit()

    def set_state(self, s):
        self.state = s
        self.paint()

    def paint(self):
        self.setText({0: "", 1: "✓ ", 2: "✕ "}[self.state] + self.tag)
        self.setStyleSheet(self.STYLES[self.state])
        self.setToolTip("Click to cycle: off → must have → must NOT have")


class FilterDialog(QDialog):
    """Wallpaper Engine style tag filters with include / exclude, plus custom tags."""

    def __init__(self, parent, include, exclude):
        super().__init__(parent)
        self.setWindowTitle("Workshop filters")
        self.resize(640, 640)
        self.buttons = {}
        known = {t for _, tags in FILTER_GROUPS for t in tags}
        v = QVBoxLayout(self)
        v.addWidget(QLabel("Click a tag to cycle: <b style='color:#2e7d32'>✓ must have</b> → "
                           "<b style='color:#b71c1c'>✕ must NOT have</b> → off.<br>"
                           "Required tags must all match. “Mature” results only appear with a Steam Web API key."))
        area = QScrollArea()
        area.setWidgetResizable(True)
        inner = QWidget()
        iv = QVBoxLayout(inner)
        for name, tags in FILTER_GROUPS:
            iv.addWidget(self.group(name, tags, include, exclude))
        custom = [t for t in [*include, *exclude] if t not in known]
        self.custom_box = self.group("Custom tags", custom, include, exclude)
        iv.addWidget(self.custom_box)
        row = QHBoxLayout()
        self.custom_edit = QLineEdit()
        self.custom_edit.setPlaceholderText("Any other tag, e.g. a resolution like “3440 x 1440”")
        self.custom_edit.returnPressed.connect(lambda: self.add_custom(1))
        b1 = QPushButton("Require")
        b1.clicked.connect(lambda: self.add_custom(1))
        b2 = QPushButton("Exclude")
        b2.clicked.connect(lambda: self.add_custom(2))
        for w in (self.custom_edit, b1, b2):
            row.addWidget(w)
        row.setStretch(0, 1)
        iv.addLayout(row)
        iv.addStretch(1)
        area.setWidget(inner)
        v.addWidget(area, 1)

        bottom = QHBoxLayout()
        clear = QPushButton("Clear all")
        clear.clicked.connect(lambda: [b.set_state(0) for b in self.buttons.values()])
        ok = QPushButton("Apply")
        ok.setDefault(True)
        ok.clicked.connect(self.accept)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        bottom.addWidget(clear)
        bottom.addStretch(1)
        bottom.addWidget(cancel)
        bottom.addWidget(ok)
        v.addLayout(bottom)

    def group(self, name, tags, include, exclude):
        box = QGroupBox(name)
        g = QGridLayout(box)
        box.grid = g
        for i, t in enumerate(tags):
            self.place(g, i, t, 1 if t in include else 2 if t in exclude else 0)
        return box

    def place(self, grid, i, tag, state):
        b = TagButton(tag, state)
        self.buttons[tag] = b
        grid.addWidget(b, i // 4, i % 4)

    def add_custom(self, state):
        t = self.custom_edit.text().strip()
        if not t:
            return
        if t in self.buttons:
            self.buttons[t].set_state(state)
        else:
            self.place(self.custom_box.grid, len(self.buttons), t, state)
        self.custom_edit.clear()

    def result_tags(self):
        inc = [t for t, b in self.buttons.items() if b.state == 1]
        exc = [t for t, b in self.buttons.items() if b.state == 2]
        return inc, exc


class Main(QMainWindow):
    def __init__(self):
        super().__init__()
        self.cfg = load_cfg()
        self.items = []
        self.ex_items = []
        self.ex_page, self.ex_total, self.ex_gen, self.lib_gen = 1, 0, 0, 0
        self.ex_full_gen = 0
        self.ex_loaded = False
        self.threads = set()
        self.lib_loader = self.ex_loader = None
        self.lib_sig = ()
        self.quitting = False
        self.pending = None         # Workshop item to set as wallpaper once its download lands
        self.latest = None          # newest GitHub release, if known
        self.web = None
        if HAVE_WEB:
            self.web = SteamWeb(self)
            self.web.subscribed.connect(self.on_subscribed)
        self.setWindowTitle("Wallpaper Hub")
        self.resize(1320, 800)
        self.build_ui()
        self.build_tray()
        self.rescan()
        self.refresh_status()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(3000)
        if self.cfg["check_updates"]:
            QTimer.singleShot(2500, self.check_updates)
        if resolve_backend(self.cfg) == "renderer" and not engine_path():
            QTimer.singleShot(400, lambda: QMessageBox.warning(
                self, "Wallpaper Engine support missing",
                "Nothing is installed that can show Wallpaper Engine wallpapers yet.\n\n"
                "On KDE Plasma, install the plugin:  paru -S wallpaper-engine-kde-plugin-git\n"
                "(then log out and in).\n\nOr install the standalone renderer: paru -S linux-wallpaperengine-git"))

    # ---- threads ----------------------------------------------------------
    def spawn(self, t):
        self.threads.add(t)
        t.finished.connect(lambda t=t: self.threads.discard(t))
        t.start()
        return t

    def drop(self, loader):
        if loader:
            loader.requestInterruption()
            try:
                loader.loaded.disconnect()
            except Exception:
                pass

    # ---- UI ---------------------------------------------------------------
    def build_ui(self):
        split = QSplitter()
        central = QWidget()
        cv = QVBoxLayout(central)
        cv.setContentsMargins(0, 0, 0, 0)
        cv.setSpacing(0)
        self.banner = QWidget()
        self.banner.setStyleSheet("background:#1565c0;")
        bh = QHBoxLayout(self.banner)
        self.banner_label = QLabel("")
        self.banner_label.setStyleSheet("color:white;font-weight:bold;")
        self.banner_btn = QPushButton("Update now")
        self.banner_btn.clicked.connect(self.install_update_clicked)
        bh.addWidget(self.banner_label, 1)
        bh.addWidget(self.banner_btn)
        self.banner.hide()
        cv.addWidget(self.banner)
        cv.addWidget(split, 1)
        self.setCentralWidget(central)

        self.left = QTabWidget()
        self.left.addTab(self.page_library(), "My Library")
        self.left.addTab(self.page_explore(), "Explore Workshop")
        self.left.currentChanged.connect(self.left_changed)
        split.addWidget(self.left)

        self.right = QTabWidget()
        self.right.addTab(self.tab_wallpaper(), "Wallpaper")
        self.right.addTab(self.tab_workshop_item(), "Workshop item")
        sa = QScrollArea()
        sa.setWidgetResizable(True)
        sa.setFrameShape(QScrollArea.Shape.NoFrame)
        sa.setWidget(self.tab_settings())
        self.right.addTab(sa, "Settings")
        self.right.addTab(self.tab_lock(), "Lock screen")
        split.addWidget(self.right)
        split.setStretchFactor(0, 1)
        split.setSizes([900, 400])
        self.refresh_screens()

    def page_library(self):
        left = QWidget()
        lv = QVBoxLayout(left)
        top = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search your library by title, tag or workshop id…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.apply_filter)
        self.typef = QComboBox()
        self.typef.addItems(["All types", "scene", "video", "web", "application"])
        self.typef.currentIndexChanged.connect(self.apply_filter)
        b_rescan = QPushButton("Rescan")
        b_rescan.clicked.connect(self.rescan)
        b_add = QPushButton("Add folder…")
        b_add.clicked.connect(self.add_library)
        for w in (self.search, self.typef, b_rescan, b_add):
            top.addWidget(w)
        top.setStretch(0, 1)
        lv.addLayout(top)

        self.grid = make_grid()
        self.grid.currentItemChanged.connect(self.show_item)
        self.grid.itemDoubleClicked.connect(lambda *_: self.apply_selected())
        lv.addWidget(self.grid, 1)
        self.count = QLabel()
        lv.addWidget(self.count)
        return left

    def page_explore(self):
        w = QWidget()
        v = QVBoxLayout(w)

        r1 = QHBoxLayout()
        self.ex_search = QLineEdit()
        self.ex_search.setPlaceholderText("Search the Workshop (press Enter)…")
        self.ex_search.setClearButtonEnabled(True)
        self.ex_search.returnPressed.connect(lambda: self.ex_run(1))
        b = QPushButton("Search")
        b.clicked.connect(lambda: self.ex_run(1))
        r1.addWidget(self.ex_search, 1)
        r1.addWidget(b)
        v.addLayout(r1)

        r2 = QHBoxLayout()
        self.ex_sort = QComboBox()
        for label, val in SORTS:
            self.ex_sort.addItem(label, val)
        self.ex_period = QComboBox()
        for label, val in PERIODS:
            self.ex_period.addItem(label, val)
        self.ex_period.setCurrentIndex(1)
        for c in (self.ex_sort, self.ex_period):
            c.currentIndexChanged.connect(self.ex_filter_changed)
            r2.addWidget(c)
        self.ex_filters_btn = QPushButton("Filters…")
        self.ex_filters_btn.clicked.connect(self.edit_filters)
        r2.addWidget(self.ex_filters_btn)
        self.ex_chips = QLabel("")
        self.ex_chips.setWordWrap(True)
        r2.addWidget(self.ex_chips, 1)
        v.addLayout(r2)
        self.update_chips()

        self.ex_grid = make_grid()
        self.ex_grid.currentItemChanged.connect(self.show_ex_item)
        self.ex_grid.itemDoubleClicked.connect(lambda *_: self.right.setCurrentIndex(1))
        v.addWidget(self.ex_grid, 1)

        r3 = QHBoxLayout()
        self.ex_prev = QPushButton("◀ Prev")
        self.ex_prev.clicked.connect(lambda: self.ex_run(self.ex_page - 1))
        self.ex_next = QPushButton("Next ▶")
        self.ex_next.clicked.connect(lambda: self.ex_run(self.ex_page + 1))
        self.ex_label = QLabel("")
        r3.addWidget(self.ex_prev)
        r3.addWidget(self.ex_next)
        r3.addWidget(self.ex_label, 1)
        v.addLayout(r3)
        self.ex_prev.setEnabled(False)
        self.ex_next.setEnabled(False)
        return w

    def tab_wallpaper(self):
        w = QWidget()
        v = QVBoxLayout(w)
        self.preview = PreviewLabel("Select a wallpaper")
        self.title = QLabel("")
        self.title.setWordWrap(True)
        self.title.setStyleSheet("font-size: 16px; font-weight: bold;")
        self.meta = QLabel("")
        self.meta.setWordWrap(True)
        self.warn = QLabel("")
        self.warn.setWordWrap(True)
        self.warn.setStyleSheet("color: #d98e04;")
        v.addWidget(self.preview)
        v.addWidget(self.title)
        v.addWidget(self.meta)
        v.addWidget(self.warn)
        self.plan = QLabel("")
        self.plan.setWordWrap(True)
        v.addWidget(self.plan)

        row, self.target, self.scaling_box = self.apply_row()
        v.addLayout(row)

        b_apply = QPushButton("Set as wallpaper (picks animated or still automatically)")
        b_apply.clicked.connect(self.apply_selected)
        b_stop = QPushButton("Stop animated wallpaper")
        b_stop.clicked.connect(self.stop_clicked)
        b_static = QPushButton("Use a still image instead")
        b_static.clicked.connect(self.static_selected)
        b_lock = QPushButton("Set as lock screen image")
        b_lock.clicked.connect(self.lock_selected)
        for b in (b_apply, b_stop, b_static, b_lock):
            v.addWidget(b)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        v.addWidget(self.status)
        self.apply_hint = QLabel("")
        self.apply_hint.setWordWrap(True)
        self.apply_hint.setStyleSheet("color: #d98e04;")
        v.addWidget(self.apply_hint)
        v.addStretch(1)
        return w

    def tab_workshop_item(self):
        w = QWidget()
        v = QVBoxLayout(w)
        self.ex_preview = PreviewLabel("Pick a wallpaper in “Explore Workshop”")
        self.ex_title = QLabel("")
        self.ex_title.setWordWrap(True)
        self.ex_title.setStyleSheet("font-size: 16px; font-weight: bold;")
        self.ex_meta = QLabel("")
        self.ex_meta.setWordWrap(True)
        self.ex_desc = QLabel("")
        self.ex_desc.setWordWrap(True)
        self.ex_desc.setAlignment(Qt.AlignmentFlag.AlignTop)
        for x in (self.ex_preview, self.ex_title, self.ex_meta, self.ex_desc):
            v.addWidget(x)
        row, self.ex_target, self.ex_scaling = self.apply_row()
        v.addLayout(row)
        self.b_set = QPushButton("Set as desktop wallpaper")
        self.b_set.clicked.connect(self.ex_set_desktop)
        self.b_sub = QPushButton("Subscribe && download")
        self.b_sub.clicked.connect(lambda: self.get_item())
        self.b_steam = QPushButton("Open in Steam client")
        self.b_steam.clicked.connect(self.open_in_steam)
        self.b_web = QPushButton("Open Workshop page in browser")
        self.b_web.clicked.connect(lambda: self.open_web())
        self.b_show = QPushButton("Show in My Library")
        self.b_show.clicked.connect(self.show_in_library)
        for b in (self.b_set, self.b_sub, self.b_steam, self.b_web, self.b_show):
            v.addWidget(b)
        self.ex_status = QLabel("")
        self.ex_status.setWordWrap(True)
        v.addWidget(self.ex_status)
        self.ex_dl = QLabel("")
        self.ex_dl.setWordWrap(True)
        v.addWidget(self.ex_dl)
        v.addStretch(1)
        return w

    def apply_row(self):
        """'Apply to [monitor]  Scaling [mode]' controls shared by the Wallpaper and Workshop tabs."""
        row = QHBoxLayout()
        row.addWidget(QLabel("Apply to:"))
        target = QComboBox()
        row.addWidget(target, 1)
        row.addWidget(QLabel("Scaling:"))
        scaling = QComboBox()
        for label, val in SCALINGS:
            scaling.addItem(label, val)
        scaling.setToolTip("Default = as the wallpaper's author designed it. Fill crops to cover the monitor, "
                           "Fit shows the whole picture, Stretch distorts to the monitor's shape.")
        row.addWidget(scaling)
        target.currentIndexChanged.connect(lambda *_: self.sync_scaling(target, scaling))
        return row, target, scaling

    def sync_scaling(self, target, scaling):
        name = target.currentData()
        mode = self.cfg["scaling"] if name in (None, "*") else self.cfg["screen_scaling"].get(name, self.cfg["scaling"])
        scaling.setCurrentIndex(max(0, scaling.findData(mode)))

    def tab_settings(self):
        w = QWidget()
        f = QFormLayout(w)
        c = self.cfg

        fps = QSpinBox()
        fps.setRange(1, 240)
        fps.setValue(c["fps"])
        fps.valueChanged.connect(lambda v: self.setc("fps", v))
        f.addRow("FPS limit", fps)

        vol = QSlider(Qt.Orientation.Horizontal)
        vol.setRange(0, 100)
        vol.setValue(c["volume"])
        vol.valueChanged.connect(lambda v: self.setc("volume", v))
        f.addRow("Volume", vol)

        be = QComboBox()
        be.addItem("Automatic (Plasma plugin if installed)", "auto")
        be.addItem("Plasma's Wallpaper Engine plugin", "plasma")
        be.addItem("linux-wallpaperengine renderer (own window)", "renderer")
        be.setCurrentIndex(max(0, be.findData(c["backend"])))
        be.currentIndexChanged.connect(lambda *_: self.setc("backend", be.currentData()))
        f.addRow("How wallpapers are applied", be)
        f.addRow(QLabel("FPS, volume and the options below only apply to the linux-wallpaperengine renderer; "
                        "with Plasma's plugin set them in Plasma's own wallpaper settings."))

        lay = QComboBox()
        lay.addItem("Independent: a wallpaper per monitor", "independent")
        lay.addItem("Duplicate: same wallpaper on every monitor", "duplicate")
        lay.setCurrentIndex(max(0, lay.findData(c["layout"])))
        lay.currentIndexChanged.connect(lambda *_: (self.setc("layout", lay.currentData()), self.apply_layout_state()))
        f.addRow("Display layout", lay)

        sc = QComboBox()
        for label, val in SCALINGS:
            sc.addItem(label, val)
        sc.setCurrentIndex(max(0, sc.findData(c["scaling"])))
        sc.currentIndexChanged.connect(lambda *_: self.setc("scaling", sc.currentData()))
        f.addRow("Default scaling mode", sc)

        cl = QComboBox()
        cl.addItems(["clamp", "border", "repeat"])
        cl.setCurrentText(c["clamp"])
        cl.currentTextChanged.connect(lambda v: self.setc("clamp", v))
        f.addRow("Texture clamp", cl)

        for key, text in [
            ("mute", "Mute audio"),
            ("noautomute", "Don't auto-mute when other apps play audio"),
            ("no_audio_processing", "Disable audio reactivity"),
            ("pause_fullscreen", "Pause when an app is fullscreen"),
            ("disable_mouse", "Disable mouse interaction"),
            ("disable_parallax", "Disable parallax"),
        ]:
            cb = QCheckBox(text)
            cb.setChecked(bool(c[key]))
            cb.toggled.connect(lambda v, k=key: self.setc(k, v))
            f.addRow(cb)

        self.assets = QLineEdit(c["assets_dir"])
        self.assets.setPlaceholderText("auto-detected from Steam")
        self.assets.editingFinished.connect(lambda: self.setc("assets_dir", self.assets.text().strip()))
        f.addRow("Assets folder", self.assets)

        auto = QCheckBox("Re-apply wallpaper on login")
        auto.setChecked(bool(c["autostart"]))
        auto.toggled.connect(self.toggle_autostart)
        f.addRow(auto)

        b = QPushButton("Restart wallpaper with these settings")
        b.clicked.connect(self.restart_clicked)
        f.addRow(b)
        b_log = QPushButton("Show engine log && diagnostics")
        b_log.clicked.connect(self.show_log)
        f.addRow(b_log)

        f.addRow(QLabel("<b>Workshop (Explore)</b>"))
        self.api_key = QLineEdit(c["steam_api_key"])
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key.setPlaceholderText("optional – makes Explore more reliable")
        self.api_key.editingFinished.connect(lambda: self.setc("steam_api_key", self.api_key.text().strip()))
        f.addRow("Steam Web API key", self.api_key)
        link = QLabel('<a href="https://steamcommunity.com/dev/apikey">Get a free key</a> '
                      "(any domain name works). Without one, Explore reads the Workshop web pages.")
        link.setOpenExternalLinks(True)
        link.setWordWrap(True)
        f.addRow(link)
        self.cmd_user = QLineEdit(c["steamcmd_user"])
        self.cmd_user.setPlaceholderText("optional – Steam username that owns Wallpaper Engine")
        self.cmd_user.editingFinished.connect(self.cmd_user_changed)
        f.addRow("steamcmd username", self.cmd_user)

        f.addRow(QLabel("<b>Steam account (for Subscribe)</b>"))
        self.acct = QLabel("")
        self.acct.setWordWrap(True)
        f.addRow(self.acct)
        ab = QHBoxLayout()
        self.b_in = QPushButton("Sign in…")
        self.b_in.clicked.connect(lambda: SteamLoginDialog(self, self.web).exec())
        self.b_out = QPushButton("Sign out")
        self.b_out.clicked.connect(lambda: self.web.sign_out())
        ab.addWidget(self.b_in)
        ab.addWidget(self.b_out)
        f.addRow(ab)
        if HAVE_WEB:
            self.web.login_changed.connect(lambda *_: self.refresh_acct())
        self.refresh_acct()

        f.addRow(QLabel("<b>Updates</b>"))
        f.addRow(QLabel(f"Wallpaper Hub v{__version__}"))
        auto_upd = QCheckBox("Check for updates on startup")
        auto_upd.setChecked(bool(c["check_updates"]))
        auto_upd.toggled.connect(lambda v: self.setc("check_updates", v))
        f.addRow(auto_upd)
        ub = QHBoxLayout()
        b_chk = QPushButton("Check for updates")
        b_chk.clicked.connect(lambda: self.check_updates(manual=True))
        self.b_upd = QPushButton("Install update")
        self.b_upd.setEnabled(False)
        self.b_upd.clicked.connect(self.install_update_clicked)
        ub.addWidget(b_chk)
        ub.addWidget(self.b_upd)
        f.addRow(ub)
        self.upd_status = QLabel("")
        self.upd_status.setWordWrap(True)
        f.addRow(self.upd_status)

        self.info = QLabel("")
        self.info.setWordWrap(True)
        f.addRow(self.info)
        return w

    def tab_lock(self):
        w = QWidget()
        v = QVBoxLayout(w)
        t = QLabel(
            "Lock screens can't run animated wallpapers, so this sets a still image.\n"
            "Supported: KDE Plasma (kscreenlocker) and GNOME. Your session: "
            f"{desktop_env() or 'unknown'}."
        )
        t.setWordWrap(True)
        v.addWidget(t)
        b1 = QPushButton("Use selected wallpaper's preview")
        b1.clicked.connect(self.lock_selected)
        b2 = QPushButton("Choose an image file…")
        b2.clicked.connect(self.lock_file)
        v.addWidget(b1)
        v.addWidget(b2)
        v.addWidget(QLabel("Tip: use Choose image… with a full-res frame/screenshot for a sharper result;\n"
                           "Workshop previews are low resolution."))
        v.addStretch(1)
        return w

    def build_tray(self):
        self.tray = QSystemTrayIcon(QIcon.fromTheme("preferences-desktop-wallpaper"), self)
        m = QMenu()
        m.addAction("Show Wallpaper Hub", self.showNormal)
        m.addAction("Stop animated wallpaper", self.stop_clicked)
        m.addAction("Quit (wallpaper keeps running)", self.quit_app)
        self.tray.setContextMenu(m)
        self.tray.activated.connect(lambda r: self.showNormal() if r == QSystemTrayIcon.ActivationReason.Trigger else None)
        self.tray.show()

    # ---- helpers ----------------------------------------------------------
    def setc(self, k, v):
        self.cfg[k] = v
        save_cfg(self.cfg)

    def current(self):
        li = self.grid.currentItem()
        return self.items[li.data(UR)] if li else None

    def need_item(self):
        it = self.current()
        if not it:
            QMessageBox.information(self, "Wallpaper Hub", "Select a wallpaper first.")
        return it

    def refresh_screens(self):
        for t, sc in ((self.target, self.scaling_box), (self.ex_target, self.ex_scaling)):
            t.clear()
            t.addItem("All screens", "*")
            for s in QApplication.screens():
                t.addItem(f"{s.name()}  ({s.geometry().width()}×{s.geometry().height()})", s.name())
            self.sync_scaling(t, sc)
        self.apply_layout_state()

    def apply_layout_state(self):
        """'Same wallpaper on every monitor' layout: the monitor pickers are locked to All screens."""
        dup = self.cfg["layout"] == "duplicate"
        for t in (self.target, self.ex_target):
            if dup:
                t.setCurrentIndex(0)
            t.setEnabled(not dup)

    def target_screen(self):
        """The monitor chosen in 'Apply to', or None for all monitors."""
        name = self.target.currentData()
        for s in QApplication.screens():
            if s.name() == name:
                return s
        return None

    def still_image(self, it):
        """Best still frame available: a video frame via ffmpeg, otherwise the preview picture."""
        f = ""
        try:
            f = json.loads((Path(it["dir"]) / "project.json").read_text(encoding="utf-8-sig")).get("file", "")
        except Exception:
            pass
        src = Path(it["dir"]) / f if f else None
        if src and src.is_file() and src.suffix.lower() in IMAGE_EXT:     # an image wallpaper: use the real picture
            img = QImage(str(src))
            if not img.isNull():
                return img
        if it["type"] == "video" and src and src.is_file() and shutil.which("ffmpeg"):
            out = DATA_DIR / "static" / f"{it['id']}-frame.png"
            out.parent.mkdir(parents=True, exist_ok=True)
            for ss in ("1", "0"):
                if out.exists():
                    out.unlink()
                run(["ffmpeg", "-y", "-loglevel", "error", "-ss", ss, "-i", str(src), "-frames:v", "1", str(out)],
                    timeout=40)
                img = QImage(str(out)) if out.is_file() else QImage()
                if not img.isNull():
                    return img
        return QImage(it["preview"]) if it["preview"] else QImage()

    def export_png(self, it, screen=None):
        """Still image scaled to cover the monitor and centre-cropped (previews are often square/small)."""
        img = self.still_image(it)
        if img.isNull():
            return None
        s = screen or QApplication.primaryScreen()
        dpr = s.devicePixelRatio()
        w, h = round(s.geometry().width() * dpr), round(s.geometry().height() * dpr)
        img = img.scaled(w, h, Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation)
        img = img.copy((img.width() - w) // 2, (img.height() - h) // 2, w, h)
        out = DATA_DIR / "static" / f"{it['id']}-{w}x{h}.png"
        out.parent.mkdir(parents=True, exist_ok=True)
        img.save(str(out), "PNG")
        return str(out)

    def report(self, ok, msg, done):
        if ok:
            self.statusBar().showMessage(done, 6000)
        else:
            QMessageBox.warning(self, "Wallpaper Hub", msg)

    def left_changed(self, i):
        self.right.setCurrentIndex(1 if i == 1 else 0)
        if i == 1 and not self.ex_loaded:
            self.ex_run(1)

    # ---- library ----------------------------------------------------------
    def rescan(self):
        keep = self.current()["id"] if self.current() else None
        paths, _ = library_info(self.cfg)
        self.lib_sig = library_signature(paths)
        self.items = scan(paths)
        self.lib_gen += 1
        self.drop(self.lib_loader)
        self.grid.clear()
        for i, it in enumerate(self.items):
            self.grid.addItem(grid_item(it["title"], f"{it['title']}\n{it['type']} · {it['id']}", i))
            if keep and it["id"] == keep:
                self.grid.setCurrentRow(i)
        self.lib_loader = ImageLoader([(i, it["preview"]) for i, it in enumerate(self.items)], self.lib_gen)
        self.lib_loader.loaded.connect(self.on_lib_thumb)
        self.spawn(self.lib_loader)
        self.apply_filter()
        self.refresh_ex_marks()
        self.check_pending()
        if not self.items:
            self.count.setText("No wallpapers found. Subscribe to some in the Explore tab, in Steam, "
                               "or use “Add folder…”.")

    def on_lib_thumb(self, gen, row, img):
        if gen == self.lib_gen and row < self.grid.count():
            self.grid.item(row).setIcon(QIcon(QPixmap.fromImage(img)))

    def add_library(self):
        d = QFileDialog.getExistingDirectory(self, "Folder containing wallpaper project folders")
        if d:
            self.cfg["extra_libraries"].append(d)
            save_cfg(self.cfg)
            self.rescan()

    def apply_filter(self):
        q = self.search.text().strip().lower()
        t = self.typef.currentText()
        shown = 0
        for i, it in enumerate(self.items):
            hay = " ".join([it["title"], it["id"], *it["tags"]]).lower()
            ok = (not q or q in hay) and (t == "All types" or it["type"] == t)
            self.grid.item(i).setHidden(not ok)
            shown += ok
        if self.items:
            self.count.setText(f"{shown} of {len(self.items)} wallpapers")

    def show_item(self, *_):
        it = self.current()
        if not it:
            return
        self.preview.show_file(it["preview"])
        self.title.setText(it["title"])
        self.plan.setText(plan_for(it, self.cfg)[1])
        self.meta.setText(f"{it['type']} · {it['id']}" + (f"\n{', '.join(it['tags'])}" if it["tags"] else ""))
        self.warn.setText("Application-type wallpapers are Windows executables and won't run on Linux."
                          if it["type"] == "application" else
                          "Web wallpapers have only partial support in linux-wallpaperengine."
                          if it["type"] == "web" else "")

    # ---- explore ----------------------------------------------------------
    def ex_query(self, page):
        return {
            "text": self.ex_search.text(), "page": page,
            "sort": self.ex_sort.currentData(), "days": self.ex_period.currentData(),
            "include": list(self.cfg["ex_include"]), "exclude": list(self.cfg["ex_exclude"]),
        }

    def update_chips(self):
        inc, exc = self.cfg["ex_include"], self.cfg["ex_exclude"]
        chips = [f"<span style='color:#2e7d32'>✓ {htmllib.escape(t)}</span>" for t in inc] + \
                [f"<span style='color:#b71c1c'>✕ {htmllib.escape(t)}</span>" for t in exc]
        self.ex_chips.setText("  ".join(chips) if chips else "No filters")
        n = len(inc) + len(exc)
        self.ex_filters_btn.setText(f"Filters ({n})…" if n else "Filters…")

    def edit_filters(self):
        d = FilterDialog(self, self.cfg["ex_include"], self.cfg["ex_exclude"])
        if d.exec() == QDialog.DialogCode.Accepted:
            inc, exc = d.result_tags()
            self.cfg["ex_include"], self.cfg["ex_exclude"] = inc, exc
            save_cfg(self.cfg)
            self.update_chips()
            self.ex_run(1)

    def ex_filter_changed(self, *_):
        self.ex_period.setEnabled(self.ex_sort.currentData() == "trend")
        if self.ex_loaded:
            self.ex_run(1)

    def ex_run(self, page):
        if page < 1:
            return
        self.ex_loaded = True
        self.ex_gen += 1
        self.drop(self.ex_loader)
        self.ex_page = page
        self.ex_label.setText("Loading…")
        self.ex_prev.setEnabled(False)
        self.ex_next.setEnabled(False)
        t = SearchThread(self.ex_query(page), self.cfg, self.ex_gen)
        t.done.connect(self.ex_results)
        self.spawn(t)

    def ex_results(self, gen, items, total, err):
        if gen != self.ex_gen:
            return
        self.ex_items, self.ex_total = items, total
        self.ex_grid.clear()
        have = {it["id"] for it in self.items}
        for i, it in enumerate(items):
            mark = "✓ " if it["id"] in have else ""
            self.ex_grid.addItem(grid_item(mark + it["title"], f"{it['title']}\n{it['author']}".strip(), i))
        pages = max(1, min(1000, math.ceil(total / PER_PAGE))) if total else 1
        if err:
            self.ex_label.setText(f"Couldn't reach the Workshop: {err}")
        elif not items:
            self.ex_label.setText("No results. If this keeps happening, add a Steam Web API key in Settings.")
        else:
            self.ex_label.setText(f"Page {self.ex_page} of {pages} · {total:,} wallpapers")
        self.ex_prev.setEnabled(self.ex_page > 1)
        self.ex_next.setEnabled(bool(items) and self.ex_page < pages)
        self.ex_loader = ImageLoader([(i, thumb_url(it["preview_url"]) if it["preview_url"] else "")
                                      for i, it in enumerate(items)], self.ex_gen)
        self.ex_loader.loaded.connect(self.on_ex_thumb)
        self.spawn(self.ex_loader)

    def on_ex_thumb(self, gen, row, img):
        if gen == self.ex_gen and row < self.ex_grid.count():
            self.ex_grid.item(row).setIcon(QIcon(QPixmap.fromImage(img)))

    def refresh_ex_marks(self):
        have = {it["id"] for it in self.items}
        for i, it in enumerate(self.ex_items):
            if i < self.ex_grid.count():
                self.ex_grid.item(i).setText(("✓ " if it["id"] in have else "") + it["title"])
        self.show_ex_item()

    def ex_current(self):
        li = self.ex_grid.currentItem()
        return self.ex_items[li.data(UR)] if li and li.data(UR) < len(self.ex_items) else None

    def show_ex_item(self, *args):
        it = self.ex_current()
        if not it:
            return
        have = {x["id"] for x in self.items}
        if args:                    # selection changed (not just a library refresh): load its preview
            self.show_ex_preview(it)
        self.ex_title.setText(it["title"])
        bits = [b for b in (f"by {it['author']}" if it["author"] else "",
                            f"{it['subs']:,} subscribers" if it["subs"] else "", it["id"]) if b]
        self.ex_meta.setText(" · ".join(bits) + (f"\n{', '.join(it['tags'])}" if it["tags"] else ""))
        self.ex_desc.setText(it["desc"])
        installed = it["id"] in have
        self.b_show.setVisible(installed)
        self.b_sub.setVisible(not installed)
        self.b_set.setText("Set as desktop wallpaper" if installed else "Download && set as desktop wallpaper")
        if installed:
            self.ex_status.setText("✓ Installed. " + plan_for(self.local_item(it["id"]), self.cfg)[1])
        elif self.ex_status.text().startswith("✓ Installed"):
            self.ex_status.setText("")
        if args:
            self.right.setCurrentIndex(1)

    def show_ex_preview(self, it):
        """Show the item's full-size preview (animated if it's a GIF); a small one meanwhile."""
        u = it["preview_url"]
        if not u:
            self.ex_preview.show_file("")
            return
        full = cache_file(full_url(u))
        if full.is_file() and full.stat().st_size:
            self.ex_preview.show_file(str(full))
            return
        small = cache_file(thumb_url(u))
        if small.is_file():
            self.ex_preview.show_file(str(small))
        else:
            self.ex_preview.reset("Loading preview…")
        self.ex_full_gen += 1
        t = DetailLoader(full_url(u), self.ex_full_gen)
        t.ready.connect(self.on_full_ready)
        self.spawn(t)

    def on_full_ready(self, gen, url):
        if gen == self.ex_full_gen:
            self.ex_preview.show_file(str(cache_file(url)))

    def open_in_steam(self):
        it = self.ex_current()
        if it and not QDesktopServices.openUrl(QUrl(f"steam://url/CommunityFilePage/{it['id']}")):
            self.open_web()

    def local_item(self, wid):
        return next((x for x in self.items if x["id"] == wid), None)

    def ex_set_desktop(self):
        """Workshop item -> desktop. Installed: apply now. Otherwise download, then apply when it arrives."""
        it = self.ex_current()
        if not it:
            return
        local = self.local_item(it["id"])
        if local:
            self.apply_item(local, self.ex_target.currentData(), self.ex_scaling.currentData())
            return
        self.pending = {"id": it["id"], "target": self.ex_target.currentData(),
                        "scaling": self.ex_scaling.currentData()}
        self.ex_status.setText("Not downloaded yet. It will be set as your wallpaper as soon as it arrives.")
        self.get_item(it)

    def check_pending(self):
        if not self.pending:
            return
        local = self.local_item(self.pending["id"])
        if local:
            p, self.pending = self.pending, None
            self.apply_item(local, p["target"], p["scaling"])

    def steamcmd_ready(self):
        return bool(shutil.which("steamcmd") and self.cfg.get("steamcmd_user", "").strip())

    def get_item(self, it=None):
        """Subscribe to the item on Steam (so it's in your Wallpaper Engine subscriptions) and download it."""
        it = it or self.ex_current()
        if not it:
            return
        started = False
        if HAVE_WEB:
            if not self.web.logged_in:
                SteamLoginDialog(self, self.web).exec()
            if self.web.logged_in:
                self.ex_status.setText("Subscribing on Steam…")
                self.web.subscribe(it["id"])
                started = True
            else:
                self.ex_status.setText("Not signed in to Steam, so it can't be subscribed from here.")
        else:
            self.ex_status.setText("Subscribing from inside the app needs `python-pyqt6-webengine` "
                                   "(sudo pacman -S python-pyqt6-webengine).")
        if self.steamcmd_ready():
            self.run_steamcmd(it)
            started = True
        if not started:
            self.open_in_steam()
            self.ex_status.setText(self.ex_status.text() + "\nOpened it in the Steam client; press Subscribe there.")

    def on_subscribed(self, wid, ok, msg):
        cur = self.ex_current()
        if ok:
            text = ("Subscribed ✓. Steam will download it (Steam must be running) and it will appear in "
                    "My Library automatically.")
        else:
            text = f"Couldn't subscribe: {msg}"
            if not self.steamcmd_ready():
                self.open_in_steam()
                text += "\nOpened it in the Steam client instead; press Subscribe there."
        if cur and cur["id"] == wid:
            self.ex_status.setText(text)
        else:
            self.statusBar().showMessage(text, 8000)

    def run_steamcmd(self, it):
        exe = shutil.which("steamcmd")
        dirs = steam_apps_dirs()
        if not (exe and dirs):
            self.ex_dl.setText("steamcmd or your Steam library folder wasn't found.")
            return
        t = SteamCmdThread(exe, self.cfg["steamcmd_user"].strip(), dirs[0].parent, it["id"])
        t.line.connect(lambda s: self.ex_dl.setText("steamcmd: " + s))
        t.done.connect(lambda ok, msg: (self.ex_dl.setText("steamcmd: " + msg), self.rescan() if ok else None))
        self.ex_dl.setText("Starting steamcmd…")
        self.spawn(t)

    def open_web(self):
        it = self.ex_current()
        if it:
            QDesktopServices.openUrl(QUrl(workshop_page_url(it["id"])))

    def show_in_library(self):
        it = self.ex_current()
        if not it:
            return
        for i, x in enumerate(self.items):
            if x["id"] == it["id"]:
                self.left.setCurrentIndex(0)
                self.search.clear()
                self.grid.setCurrentRow(i)
                self.grid.scrollToItem(self.grid.item(i))
                return

    def refresh_acct(self):
        if not HAVE_WEB:
            self.acct.setText("Not available: install `python-pyqt6-webengine` "
                              "(sudo pacman -S python-pyqt6-webengine) and restart.")
            self.b_in.setEnabled(False)
            self.b_out.setEnabled(False)
            return
        on = self.web.logged_in
        self.acct.setText("Signed in to Steam ✓" if on else "Not signed in.")
        self.b_in.setEnabled(not on)
        self.b_out.setEnabled(on)

    # ---- updates ----------------------------------------------------------
    def check_updates(self, manual=False):
        if manual:
            self.upd_status.setText("Checking…")
        t = UpdateCheckThread()
        t.done.connect(lambda ok, rel, err: self.on_update_checked(ok, rel, err, manual))
        self.spawn(t)

    def on_update_checked(self, ok, rel, err, manual):
        if not ok:
            if manual:
                self.upd_status.setText(f"Couldn't check for updates: {err}")
            return
        self.latest = rel
        if parse_version(rel["version"]) > parse_version(__version__):
            msg = f"Wallpaper Hub {rel['version']} is available (you have {__version__})."
            self.banner_label.setText(msg)
            self.banner.show()
            self.b_upd.setEnabled(True)
            self.upd_status.setText(msg + ("\n\n" + rel["notes"][:600] if rel["notes"] else ""))
        elif manual:
            self.upd_status.setText(f"You're up to date (v{__version__}).")

    def install_update_clicked(self):
        if not self.latest:
            return
        why = update_blocker()
        if why:
            QMessageBox.information(self, "Update", why)
            return
        self.banner_btn.setEnabled(False)
        self.b_upd.setEnabled(False)
        self.upd_status.setText("Downloading and verifying the update…")
        t = UpdateInstallThread(self.latest)
        t.done.connect(self.on_update_installed)
        self.spawn(t)

    def on_update_installed(self, ok, msg):
        self.banner_btn.setEnabled(True)
        if not ok:
            self.b_upd.setEnabled(True)
            self.upd_status.setText(f"Update failed: {msg}")
            QMessageBox.warning(self, "Update failed", msg)
            return
        self.banner.hide()
        self.upd_status.setText(f"Updated to v{msg}. Restart to use it.")
        if QMessageBox.question(self, "Updated", f"Wallpaper Hub v{msg} is installed. Restart now?") == \
                QMessageBox.StandardButton.Yes:
            self.restart_app()

    def restart_app(self):
        self.quitting = True
        os.execv(sys.executable, [sys.executable, str(SCRIPT), *sys.argv[1:]])

    def cmd_user_changed(self):
        self.setc("steamcmd_user", self.cmd_user.text().strip())

    # ---- actions ----------------------------------------------------------
    def apply_selected(self):
        it = self.need_item()
        if it:
            self.apply_item(it, self.target.currentData(), self.scaling_box.currentData())

    def apply_item(self, it, target, scaling):
        """Run `it` (a library entry) on one monitor or all of them, with the chosen scaling mode."""
        names = [s.name() for s in QApplication.screens()]
        if self.cfg["layout"] == "duplicate":
            target = "*"
        mode, note = plan_for(it, self.cfg)
        if mode == "static":
            self.apply_static(it, target, note)
            return
        self.cfg["assignments"] = {k: v for k, v in self.cfg["assignments"].items() if k in names}
        for n in (names if target == "*" else [target]):
            self.cfg["assignments"][n] = it["dir"]
            self.cfg["screen_scaling"][n] = scaling
        save_cfg(self.cfg)
        where = "all screens" if target == "*" else target
        if resolve_backend(self.cfg) == "plasma":
            stop_engine()                       # no stray renderer fighting with Plasma
            scr = next((s for s in QApplication.screens() if s.name() == target), None)
            g = scr.geometry() if (scr and target != "*") else None
            ok, msg = kde_apply_plugin(it["dir"], (g.x(), g.y(), g.width(), g.height(), scr.name()) if g else None, scaling)
            self.report(ok, msg, f"Applied “{it['title']}” to {where} through Plasma")
            self.apply_hint.setText("")
        else:
            ok, msg = start_engine(self.cfg)
            self.report(ok, msg, f"Applied “{it['title']}” to {where}")
            self.apply_hint.setText(session_hint() if ok else "")
        self.refresh_status()

    def apply_static(self, it, target, note=""):
        """Set a still picture as the wallpaper of one monitor or all of them (no animation running)."""
        stop_engine()
        scr = next((s for s in QApplication.screens() if s.name() == target), None) if target != "*" else None
        png = self.export_png(it, scr)
        if not png:
            return QMessageBox.warning(self, "Wallpaper Hub", "This wallpaper has no usable image.")
        g = scr.geometry() if scr else None
        ok, msg = set_static_desktop(png, (g.x(), g.y(), g.width(), g.height(), scr.name()) if g else None)
        self.report(ok, msg, f"Set “{it['title']}” on {scr.name() if scr else 'all screens'}. {note}")
        self.apply_hint.setText(note if ok else "")
        self.refresh_status()

    def show_log(self):
        from PyQt6.QtGui import QFontDatabase
        from PyQt6.QtWidgets import QPlainTextEdit
        d = QDialog(self)
        d.setWindowTitle("Engine log & diagnostics")
        d.resize(780, 520)
        v = QVBoxLayout(d)
        t = QPlainTextEdit(diagnostics())
        t.setReadOnly(True)
        t.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        v.addWidget(t)
        b = QPushButton("Copy to clipboard")
        b.clicked.connect(lambda: QApplication.clipboard().setText(t.toPlainText()))
        v.addWidget(b)
        d.exec()

    def restart_clicked(self):
        if resolve_backend(self.cfg) == "plasma":
            ok, msg = True, ""
            for scr_name, wdir in self.cfg["assignments"].items():
                scr = next((s for s in QApplication.screens() if s.name() == scr_name), None)
                g = scr.geometry() if scr else None
                ok, msg = kde_apply_plugin(wdir, (g.x(), g.y(), g.width(), g.height(), scr.name()) if g else None,
                                           self.cfg["screen_scaling"].get(scr_name, self.cfg["scaling"]))
                if not ok:
                    break
        else:
            ok, msg = start_engine(self.cfg)
        self.report(ok, msg, "Re-applied")
        self.refresh_status()

    def stop_clicked(self):
        """Back to a normal (non-animated) wallpaper: leaves the plugin and stops any renderer."""
        stop_engine()
        if "kde" in desktop_env() and plugin_installed():
            kde_stop_plugin()
        self.refresh_status()

    def static_selected(self):
        it = self.need_item()
        if not it:
            return
        scr = self.target_screen()
        png = self.export_png(it, scr)
        if not png:
            return QMessageBox.warning(self, "Wallpaper Hub", "This wallpaper has no usable preview image.")
        g = scr.geometry() if scr else None
        ok, msg = set_static_desktop(png, (g.x(), g.y(), g.width(), g.height(), scr.name()) if g else None)
        self.report(ok, msg, f"Static desktop image set on {scr.name() if scr else 'all screens'} "
                             "(stop the animated wallpaper to see it)")

    def lock_selected(self):
        it = self.need_item()
        if not it:
            return
        png = self.export_png(it, self.target_screen())
        if not png:
            return QMessageBox.warning(self, "Wallpaper Hub", "This wallpaper has no usable preview image.")
        ok, msg = set_lockscreen(png)
        self.report(ok, msg, "Lock screen image set (the lock screen uses one image for every monitor)")

    def lock_file(self):
        f, _ = QFileDialog.getOpenFileName(self, "Lock screen image", str(Path.home()),
                                           "Images (*.png *.jpg *.jpeg *.webp *.bmp)")
        if f:
            ok, msg = set_lockscreen(f)
            self.report(ok, msg, "Lock screen image set")

    def toggle_autostart(self, on):
        self.setc("autostart", on)
        set_autostart(on)

    def tick(self):
        paths, _ = library_info(self.cfg)
        if library_signature(paths) != self.lib_sig:     # a Workshop download finished (or was removed)
            self.rescan()
        self.refresh_status()

    def refresh_status(self):
        pid = engine_pid()
        by_dir = {it["dir"]: it["title"] for it in self.items}
        if resolve_backend(self.cfg) == "plasma":
            lines = ["Applied through Plasma's Wallpaper Engine plugin" if plugin_installed()
                     else "Plasma's Wallpaper Engine plugin is NOT installed"]
        else:
            lines = [f"Engine: {'running (pid %d)' % pid if pid else 'stopped'}"]
        for scr, d in self.cfg["assignments"].items():
            lines.append(f"  {scr} → {by_dir.get(d, Path(d).name)}")
        self.status.setText("\n".join(lines))
        paths, assets = library_info(self.cfg)
        self.info.setText(f"Backend: {resolve_backend(self.cfg)}   Plasma plugin: "
                          f"{'installed' if plugin_installed() else 'not installed'}\n"
                          f"Renderer (optional): {engine_path() or 'not installed'}\n"
                          f"steamcmd: {shutil.which('steamcmd') or 'not installed (optional)'}\n"
                          f"Assets: {assets or 'not found'}\nLibraries: {', '.join(paths) or 'none found'}\n"
                          f"Log: {LOG_FILE}")

    # ---- lifecycle --------------------------------------------------------
    def quit_app(self):
        self.quitting = True
        for l in (self.lib_loader, self.ex_loader):
            self.drop(l)
        for t in list(self.threads):
            t.wait(1500)
        QApplication.quit()

    def closeEvent(self, e):
        if not self.quitting and self.tray.isVisible():
            self.hide()
            e.ignore()
        else:
            e.accept()


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Wallpaper Hub")
    app.setQuitOnLastWindowClosed(False)
    win = Main()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
