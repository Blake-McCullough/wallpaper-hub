#!/usr/bin/env python3
"""
Wallpaper Hub - a small GUI for running Wallpaper Engine wallpapers on Linux.

* Browses your Steam Workshop / Wallpaper Engine library
* Runs wallpapers on the desktop through `linux-wallpaperengine` (per-monitor)
* Sets a static desktop image and the lock screen image (KDE Plasma, GNOME, XFCE for static)
* `wallpaper-hub --apply` re-applies the saved setup headlessly (used for autostart)
"""
import json
import os
import re
import shutil
import signal
import subprocess
import sys
from pathlib import Path

APP = "wallpaper-hub"
WE_APPID = "431960"
CFG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / APP
DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / APP
CFG_FILE = CFG_DIR / "config.json"
PID_FILE = DATA_DIR / "engine.pid"
LOG_FILE = DATA_DIR / "engine.log"
AUTOSTART = Path.home() / ".config/autostart/wallpaper-hub.desktop"

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
}


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
from PyQt6.QtCore import Qt, QSize, QThread, QTimer, pyqtSignal  # noqa: E402
from PyQt6.QtGui import QIcon, QImage, QPixmap  # noqa: E402
from PyQt6.QtWidgets import (  # noqa: E402
    QApplication, QCheckBox, QComboBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QListView, QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox,
    QPushButton, QSlider, QSpinBox, QSplitter, QSystemTrayIcon, QTabWidget, QVBoxLayout, QWidget,
)

UR = Qt.ItemDataRole.UserRole


class ThumbLoader(QThread):
    loaded = pyqtSignal(int, QImage)

    def __init__(self, items):
        super().__init__()
        self.items = items

    def run(self):
        for i, it in enumerate(self.items):
            if self.isInterruptionRequested():
                return
            if not it["preview"]:
                continue
            img = QImage(it["preview"])
            if img.isNull():
                continue
            self.loaded.emit(i, img.scaledToWidth(220, Qt.TransformationMode.SmoothTransformation))


class Main(QMainWindow):
    def __init__(self):
        super().__init__()
        self.cfg = load_cfg()
        self.items = []
        self.loader = None
        self.quitting = False
        self.setWindowTitle("Wallpaper Hub")
        self.resize(1280, 780)
        self.build_ui()
        self.build_tray()
        self.rescan()
        self.refresh_status()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh_status)
        self.timer.start(3000)
        if not engine_path():
            QTimer.singleShot(400, lambda: QMessageBox.warning(
                self, "linux-wallpaperengine missing",
                "The renderer isn't installed, so wallpapers can't run on the desktop yet.\n\n"
                "Install it with:  paru -S linux-wallpaperengine-git\n(or run install.sh)"))

    # ---- UI ---------------------------------------------------------------
    def build_ui(self):
        split = QSplitter()
        self.setCentralWidget(split)

        left = QWidget()
        lv = QVBoxLayout(left)
        top = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search title, tag or workshop id…")
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

        self.grid = QListWidget()
        self.grid.setViewMode(QListView.ViewMode.IconMode)
        self.grid.setResizeMode(QListView.ResizeMode.Adjust)
        self.grid.setMovement(QListView.Movement.Static)
        self.grid.setIconSize(QSize(220, 124))
        self.grid.setGridSize(QSize(240, 172))
        self.grid.setUniformItemSizes(True)
        self.grid.setWordWrap(True)
        self.grid.setSpacing(4)
        self.grid.currentItemChanged.connect(self.show_item)
        self.grid.itemDoubleClicked.connect(lambda *_: self.apply_selected())
        lv.addWidget(self.grid, 1)
        self.count = QLabel()
        lv.addWidget(self.count)
        split.addWidget(left)

        tabs = QTabWidget()
        tabs.addTab(self.tab_wallpaper(), "Wallpaper")
        tabs.addTab(self.tab_settings(), "Settings")
        tabs.addTab(self.tab_lock(), "Lock screen")
        split.addWidget(tabs)
        split.setStretchFactor(0, 1)
        split.setSizes([860, 400])

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

    # ---- library ----------------------------------------------------------
    def rescan(self):
        if self.loader:
            self.loader.requestInterruption()
            self.loader.wait()
        paths, _ = library_info(self.cfg)
        self.items = scan(paths)
        self.grid.clear()
        for i, it in enumerate(self.items):
            li = QListWidgetItem(it["title"])
            li.setData(UR, i)
            li.setToolTip(f"{it['title']}\n{it['type']} · {it['id']}")
            self.grid.addItem(li)
        self.loader = ThumbLoader(self.items)
        self.loader.loaded.connect(lambda i, img: self.grid.item(i).setIcon(QIcon(QPixmap.fromImage(img))))
        self.loader.start()
        self.apply_filter()
        if not self.items:
            self.count.setText("No wallpapers found. Subscribe to some in Steam (Wallpaper Engine) "
                               "or use “Add folder…”.")

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

    def refresh_status(self):
        pid = engine_pid()
        by_dir = {it["dir"]: it["title"] for it in self.items}
        lines = [f"Engine: {'running (pid %d)' % pid if pid else 'stopped'}"]
        for scr, d in self.cfg["assignments"].items():
            lines.append(f"  {scr} → {by_dir.get(d, Path(d).name)}")
        self.status.setText("\n".join(lines))
        paths, assets = library_info(self.cfg)
        self.info.setText(f"Renderer: {engine_path() or 'NOT INSTALLED'}\n"
                          f"Assets: {assets or 'not found'}\nLibraries: {', '.join(paths) or 'none found'}\n"
                          f"Log: {LOG_FILE}")

    # ---- lifecycle --------------------------------------------------------
    def quit_app(self):
        self.quitting = True
        if self.loader:
            self.loader.requestInterruption()
            self.loader.wait()
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
