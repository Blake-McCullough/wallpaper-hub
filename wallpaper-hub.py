#!/usr/bin/env python3
"""
Wallpaper Hub - a GUI for running Wallpaper Engine wallpapers on Linux.

* My Library: your subscribed / downloaded Wallpaper Engine wallpapers
* Explore: browse, search, sort and filter the whole Steam Workshop for Wallpaper Engine
* Runs wallpapers on the desktop through `linux-wallpaperengine` (per-monitor)
* Sets a static desktop image and the lock screen image (KDE Plasma, GNOME, XFCE for static)
* `wallpaper-hub --apply` re-applies the saved setup headlessly (used for autostart)
"""
import hashlib
import html as htmllib
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
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
UA = "Mozilla/5.0 (X11; Linux x86_64) WallpaperHub/1.1"
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
    "scaling": "default",
    "clamp": "clamp",
    "autostart": False,
    "steam_api_key": "",        # optional: makes Explore far more reliable
    "steamcmd_user": "",        # optional: enables direct downloads without clicking Subscribe in Steam
}

# Explore filters --------------------------------------------------------------
SORTS = [("Trending", "trend"), ("Most popular", "totaluniquesubscribers"),
         ("Most recent", "mostrecent"), ("Recently updated", "lastupdated")]
PERIODS = [("Today", 1), ("This week", 7), ("This month", 30), ("3 months", 90),
           ("6 months", 180), ("This year", 365), ("All time", -1)]
TYPES = ["Any type", "Scene", "Video", "Web"]
CATEGORIES = ["Any category", "Abstract", "Animal", "Anime", "Cartoon", "CGI", "Cyberpunk", "Fantasy",
              "Game", "Girls", "Guys", "Landscape", "Medieval", "Memes", "MMD", "Music", "Nature",
              "Pixel art", "Relaxing", "Retro", "Sci-Fi", "Sports", "Technology", "Television",
              "Vehicle", "Unspecified"]
RATINGS = ["Everyone", "Questionable", "Mature"]
API_QUERY_TYPE = {"trend": 3, "totaluniquesubscribers": 9, "mostrecent": 1, "lastupdated": 21}
API_TEXT_SEARCH = 12


# ----------------------------------------------------------------------------
# Core (no Qt needed)
# ----------------------------------------------------------------------------
def load_cfg():
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads(CFG_FILE.read_text()))
    except Exception:
        pass
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
                "tags": [str(t) for t in j.get("tags", [])] if isinstance(j.get("tags"), list) else [],
                "preview": str(preview) if preview else "",
            })
    return items


# ---- Workshop (online) -------------------------------------------------------
def http_get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def cached_download(url):
    """Download an image once; later calls read from the on-disk cache."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    f = CACHE_DIR / (hashlib.sha1(url.encode()).hexdigest() + ".img")
    if f.is_file() and f.stat().st_size > 0:
        return f.read_bytes()
    data = http_get(url)
    f.write_bytes(data)
    return data


def cache_file(url):
    return CACHE_DIR / (hashlib.sha1(url.encode()).hexdigest() + ".img")


def query_tags(q):
    tags = []
    if q.get("type") and q["type"] != TYPES[0]:
        tags.append(q["type"])
    if q.get("category") and q["category"] != CATEGORIES[0]:
        tags.append(q["category"])
    if q.get("rating"):
        tags.append(q["rating"])
    return tags


def scrape_url(q):
    text = q.get("text", "").strip()
    sort = "textsearch" if text else q.get("sort", "trend")
    params = [("appid", WE_APPID), ("searchtext", text), ("childpublishedfileid", "0"),
              ("browsesort", sort), ("section", "readytouseitems")]
    for t in query_tags(q):
        params.append(("requiredtags[]", t))
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


# ---- Renderer / desktop integration -----------------------------------------
def engine_path():
    return shutil.which("linux-wallpaperengine")


def build_command(cfg):
    exe = engine_path()
    if not exe or not cfg["assignments"]:
        return None
    _, assets = library_info(cfg)
    cmd = [exe, "--fps", str(cfg["fps"])]
    if cfg["mute"]:
        cmd.append("--silent")
    else:
        cmd += ["--volume", str(cfg["volume"])]
    if cfg["noautomute"]:
        cmd.append("--noautomute")
    if cfg["no_audio_processing"]:
        cmd.append("--no-audio-processing")
    if not cfg["pause_fullscreen"]:
        cmd.append("--no-fullscreen-pause")
    if cfg["disable_mouse"]:
        cmd.append("--disable-mouse")
    if cfg["disable_parallax"]:
        cmd.append("--disable-parallax")
    if assets:
        cmd += ["--assets-dir", assets]
    for screen, wdir in cfg["assignments"].items():
        cmd += ["--screen-root", screen, "--bg", wdir]
        if cfg["scaling"] != "default":
            cmd += ["--scaling", cfg["scaling"]]
        if cfg["clamp"] != "clamp":
            cmd += ["--clamp", cfg["clamp"]]
    return cmd


def engine_pid():
    try:
        pid = int(PID_FILE.read_text().strip())
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
        if b"linux-wallpaperengine" in cmdline:
            return pid
    except Exception:
        pass
    return None


def stop_engine():
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
    log.write("$ " + " ".join(cmd) + "\n")
    log.flush()
    p = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    PID_FILE.write_text(str(p.pid))
    return True, f"Started (pid {p.pid}). Log: {LOG_FILE}"


def run(cmd, timeout=15):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, (r.stdout + r.stderr).strip()
    except Exception as e:
        return False, str(e)


def desktop_env():
    return os.environ.get("XDG_CURRENT_DESKTOP", "").lower()


def set_static_desktop(path):
    de = desktop_env()
    uri = Path(path).as_uri()
    if "kde" in de:
        tool = shutil.which("plasma-apply-wallpaperimage")
        if not tool:
            return False, "plasma-apply-wallpaperimage not found."
        return run([tool, path])
    if "gnome" in de or "cinnamon" in de or "budgie" in de:
        ok1, m1 = run(["gsettings", "set", "org.gnome.desktop.background", "picture-uri", uri])
        ok2, m2 = run(["gsettings", "set", "org.gnome.desktop.background", "picture-uri-dark", uri])
        return ok1 and ok2, (m1 + m2).strip()
    if "xfce" in de:
        ok, out = run(["xfconf-query", "-c", "xfce4-desktop", "-l"])
        if not ok:
            return False, out
        for prop in [l for l in out.splitlines() if l.endswith("last-image")]:
            run(["xfconf-query", "-c", "xfce4-desktop", "-p", prop, "-s", path])
        return True, "ok"
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
from PyQt6.QtCore import Qt, QSize, QThread, QTimer, QUrl, pyqtSignal  # noqa: E402
from PyQt6.QtGui import QColor, QDesktopServices, QIcon, QImage, QPixmap  # noqa: E402
from PyQt6.QtWidgets import (  # noqa: E402
    QApplication, QCheckBox, QComboBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QListView, QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox,
    QPushButton, QSlider, QSpinBox, QSplitter, QSystemTrayIcon, QTabWidget, QVBoxLayout, QWidget,
)

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


class Main(QMainWindow):
    def __init__(self):
        super().__init__()
        self.cfg = load_cfg()
        self.items = []
        self.ex_items = []
        self.ex_page, self.ex_total, self.ex_gen, self.lib_gen = 1, 0, 0, 0
        self.ex_loaded = False
        self.threads = set()
        self.lib_loader = self.ex_loader = None
        self.lib_sig = ()
        self.quitting = False
        self.setWindowTitle("Wallpaper Hub")
        self.resize(1320, 800)
        self.build_ui()
        self.build_tray()
        self.rescan()
        self.refresh_status()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(3000)
        if not engine_path():
            QTimer.singleShot(400, lambda: QMessageBox.warning(
                self, "linux-wallpaperengine missing",
                "The renderer isn't installed, so wallpapers can't run on the desktop yet.\n\n"
                "Install it with:  paru -S linux-wallpaperengine-git\n(or run install.sh)"))

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
        self.setCentralWidget(split)

        self.left = QTabWidget()
        self.left.addTab(self.page_library(), "My Library")
        self.left.addTab(self.page_explore(), "Explore Workshop")
        self.left.currentChanged.connect(self.left_changed)
        split.addWidget(self.left)

        self.right = QTabWidget()
        self.right.addTab(self.tab_wallpaper(), "Wallpaper")
        self.right.addTab(self.tab_workshop_item(), "Workshop item")
        self.right.addTab(self.tab_settings(), "Settings")
        self.right.addTab(self.tab_lock(), "Lock screen")
        split.addWidget(self.right)
        split.setStretchFactor(0, 1)
        split.setSizes([900, 400])

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
        self.ex_type = QComboBox()
        self.ex_type.addItems(TYPES)
        self.ex_cat = QComboBox()
        self.ex_cat.addItems(CATEGORIES)
        self.ex_rating = QComboBox()
        self.ex_rating.addItems(RATINGS)
        for c in (self.ex_sort, self.ex_period, self.ex_type, self.ex_cat, self.ex_rating):
            c.currentIndexChanged.connect(self.ex_filter_changed)
            r2.addWidget(c)
        v.addLayout(r2)

        self.ex_grid = make_grid()
        self.ex_grid.currentItemChanged.connect(self.show_ex_item)
        self.ex_grid.itemDoubleClicked.connect(lambda *_: self.subscribe())
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
        self.preview = QLabel("Select a wallpaper")
        self.preview.setMinimumSize(340, 192)
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setStyleSheet("background: rgba(128,128,128,40); border-radius: 6px;")
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

        row = QHBoxLayout()
        row.addWidget(QLabel("Apply to:"))
        self.target = QComboBox()
        row.addWidget(self.target, 1)
        v.addLayout(row)
        self.refresh_screens()

        b_apply = QPushButton("Apply to desktop (animated)")
        b_apply.clicked.connect(self.apply_selected)
        b_stop = QPushButton("Stop animated wallpaper")
        b_stop.clicked.connect(self.stop_clicked)
        b_static = QPushButton("Set as static desktop image")
        b_static.clicked.connect(self.static_selected)
        b_lock = QPushButton("Set as lock screen image")
        b_lock.clicked.connect(self.lock_selected)
        for b in (b_apply, b_stop, b_static, b_lock):
            v.addWidget(b)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        v.addWidget(self.status)
        v.addStretch(1)
        return w

    def tab_workshop_item(self):
        w = QWidget()
        v = QVBoxLayout(w)
        self.ex_preview = QLabel("Pick a wallpaper in “Explore Workshop”")
        self.ex_preview.setMinimumSize(340, 192)
        self.ex_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.ex_preview.setStyleSheet("background: rgba(128,128,128,40); border-radius: 6px;")
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
        self.b_sub = QPushButton("Subscribe in Steam")
        self.b_sub.clicked.connect(self.subscribe)
        self.b_dl = QPushButton("Download directly (steamcmd)")
        self.b_dl.clicked.connect(self.download_direct)
        self.b_web = QPushButton("Open Workshop page in browser")
        self.b_web.clicked.connect(lambda: self.open_web())
        self.b_show = QPushButton("Show in My Library")
        self.b_show.clicked.connect(self.show_in_library)
        for b in (self.b_sub, self.b_dl, self.b_web, self.b_show):
            v.addWidget(b)
        self.ex_status = QLabel("")
        self.ex_status.setWordWrap(True)
        v.addWidget(self.ex_status)
        v.addStretch(1)
        self.update_dl_button()
        return w

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

        sc = QComboBox()
        sc.addItems(["default", "stretch", "fit", "fill"])
        sc.setCurrentText(c["scaling"])
        sc.currentTextChanged.connect(lambda v: self.setc("scaling", v))
        f.addRow("Scaling", sc)

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
        self.target.clear()
        self.target.addItem("All screens", "*")
        for s in QApplication.screens():
            self.target.addItem(f"{s.name()}  ({s.geometry().width()}×{s.geometry().height()})", s.name())

    def export_png(self, it):
        img = QImage(it["preview"]) if it["preview"] else QImage()
        if img.isNull():
            return None
        out = DATA_DIR / "static" / f"{it['id']}.png"
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
        pm = QPixmap(it["preview"]) if it["preview"] else QPixmap()
        if pm.isNull():
            self.preview.setText("No preview")
        else:
            self.preview.setPixmap(pm.scaled(340, 192, Qt.AspectRatioMode.KeepAspectRatio,
                                             Qt.TransformationMode.SmoothTransformation))
        self.title.setText(it["title"])
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
            "type": self.ex_type.currentText(), "category": self.ex_cat.currentText(),
            "rating": self.ex_rating.currentText(),
        }

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
        self.ex_loader = ImageLoader([(i, it["preview_url"]) for i, it in enumerate(items)], self.ex_gen)
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

    def show_ex_item(self, *_):
        it = self.ex_current()
        if not it:
            return
        have = {x["id"] for x in self.items}
        pm = QPixmap(str(cache_file(it["preview_url"]))) if it["preview_url"] else QPixmap()
        if pm.isNull():
            self.ex_preview.setText("Loading preview…")
        else:
            self.ex_preview.setPixmap(pm.scaled(340, 192, Qt.AspectRatioMode.KeepAspectRatio,
                                                Qt.TransformationMode.SmoothTransformation))
        self.ex_title.setText(it["title"])
        bits = [b for b in (f"by {it['author']}" if it["author"] else "",
                            f"{it['subs']:,} subscribers" if it["subs"] else "", it["id"]) if b]
        self.ex_meta.setText(" · ".join(bits) + (f"\n{', '.join(it['tags'])}" if it["tags"] else ""))
        self.ex_desc.setText(it["desc"])
        installed = it["id"] in have
        self.b_show.setVisible(installed)
        self.b_sub.setText("Already in your library — re-open in Steam" if installed else "Subscribe in Steam")
        self.ex_status.setText("✓ Installed" if installed else "")
        self.right.setCurrentIndex(1)

    def subscribe(self):
        it = self.ex_current()
        if not it:
            return
        if not QDesktopServices.openUrl(QUrl(f"steam://url/CommunityFilePage/{it['id']}")):
            self.open_web()
            return
        self.ex_status.setText("Opened in Steam. Press Subscribe there; Steam downloads it and it will "
                               "show up in My Library automatically.")

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

    def update_dl_button(self):
        exe = shutil.which("steamcmd")
        user = self.cfg.get("steamcmd_user", "").strip()
        self.b_dl.setEnabled(bool(exe and user))
        self.b_dl.setToolTip("" if exe and user else
                             "Needs steamcmd installed and a steamcmd username set in Settings "
                             "(sign in once with `steamcmd +login <user>` in a terminal).")

    def cmd_user_changed(self):
        self.setc("steamcmd_user", self.cmd_user.text().strip())
        self.update_dl_button()

    def download_direct(self):
        it = self.ex_current()
        exe = shutil.which("steamcmd")
        dirs = steam_apps_dirs()
        if not (it and exe and dirs):
            self.ex_status.setText("steamcmd or your Steam library folder wasn't found.")
            return
        t = SteamCmdThread(exe, self.cfg["steamcmd_user"].strip(), dirs[0].parent, it["id"])
        t.line.connect(lambda s: self.ex_status.setText(s))
        t.done.connect(lambda ok, msg: (self.ex_status.setText(msg), self.rescan() if ok else None))
        self.b_dl.setEnabled(False)
        t.finished.connect(self.update_dl_button)
        self.ex_status.setText("Starting steamcmd…")
        self.spawn(t)

    # ---- actions ----------------------------------------------------------
    def apply_selected(self):
        it = self.need_item()
        if not it:
            return
        target = self.target.currentData()
        names = [s.name() for s in QApplication.screens()]
        self.cfg["assignments"] = {k: v for k, v in self.cfg["assignments"].items() if k in names}
        for n in (names if target == "*" else [target]):
            self.cfg["assignments"][n] = it["dir"]
        save_cfg(self.cfg)
        ok, msg = start_engine(self.cfg)
        self.report(ok, msg, f"Applied “{it['title']}”")
        self.refresh_status()

    def restart_clicked(self):
        ok, msg = start_engine(self.cfg)
        self.report(ok, msg, "Restarted")
        self.refresh_status()

    def stop_clicked(self):
        stop_engine()
        self.refresh_status()

    def static_selected(self):
        it = self.need_item()
        if not it:
            return
        png = self.export_png(it)
        if not png:
            return QMessageBox.warning(self, "Wallpaper Hub", "This wallpaper has no usable preview image.")
        ok, msg = set_static_desktop(png)
        self.report(ok, msg, "Static desktop image set (stop the animated wallpaper to see it)")

    def lock_selected(self):
        it = self.need_item()
        if not it:
            return
        png = self.export_png(it)
        if not png:
            return QMessageBox.warning(self, "Wallpaper Hub", "This wallpaper has no usable preview image.")
        ok, msg = set_lockscreen(png)
        self.report(ok, msg, "Lock screen image set")

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
        lines = [f"Engine: {'running (pid %d)' % pid if pid else 'stopped'}"]
        for scr, d in self.cfg["assignments"].items():
            lines.append(f"  {scr} → {by_dir.get(d, Path(d).name)}")
        self.status.setText("\n".join(lines))
        paths, assets = library_info(self.cfg)
        self.info.setText(f"Renderer: {engine_path() or 'NOT INSTALLED'}\n"
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
