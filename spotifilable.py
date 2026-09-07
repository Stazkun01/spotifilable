"""
Spotifilable
============
Paste a Spotify song / playlist / album link, pick a folder, press Download.
Everything else happens in the background, with a live log, a real progress
bar, and a cool little sunglasses dog keeping you company.

Features
--------
- Auto-grabs a Spotify link from your clipboard when you open the app
- Right-click Cut/Copy/Paste, Enter to start, Esc to cancel
- Remembers your folder, quality and recent playlists between runs
- "Recent" menu to re-download / re-sync any playlist you've done before
- Quality picker (Good / Better / Best) -> mp3 at 128k / 192k / 320k
- Creates a tidy folder per playlist (optional)
- Live progress bar + per-track ✓ / ✗ log, with elapsed time
- Cancel button (stops after the current track)
- Retry Failed button for tracks that hit a network blip
- Open Folder button when done
- Send to Device: copies your music straight onto a Walkman / USB / SD card
- Skips tracks you already have, so re-running is instant
- Plain-English error messages (no internet, private playlist, etc.)
- Mascot moods: idle, working, celebrating, sulking

Built on spotDL as a library (not a subprocess), so the whole thing packages
into ONE standalone .exe with PyInstaller — see README.md.

Run from source:
    pip install spotdl
    python spotifilable.py
"""

import os
import re
import sys
import json
import math
import time
import shutil
import socket
import ctypes
import random
import threading
import queue
import subprocess
import tkinter as tk
from tkinter import filedialog, messagebox
from datetime import datetime

from spotdl import Spotdl
from spotdl.utils.ffmpeg import is_ffmpeg_installed, download_ffmpeg

APP_NAME = "Spotifilable"
APP_VERSION = "1.0"

# spotDL ships with a public default Spotify app registration for exactly this
# kind of use — no per-user Spotify developer setup needed.
DEFAULT_CLIENT_ID = "5f573c9620494bae87890c0f08a60293"
DEFAULT_CLIENT_SECRET = "212476d9b0f3472eaa762d90b19b0ba8"

# ---------------------------------------------------------------------------
# Palette & type — black, ivory, champagne gold
# ---------------------------------------------------------------------------
BG = "#0c0c0d"
PANEL = "#161616"
PANEL_2 = "#1c1c1d"
BORDER = "#262626"
GOLD = "#c9a86a"
GOLD_HOVER = "#dcc08a"
GOLD_DIM = "#3a3428"
IVORY = "#f4f1ea"
MUTED = "#8f8a7c"
FAINT = "#4a4740"
RED = "#d17b7b"
GREEN = "#9fbf8f"

F_TITLE = ("Georgia", 26, "bold")
F_SUB = ("Georgia", 11)
F_LABEL = ("Segoe UI", 10)
F_TINY = ("Segoe UI", 8, "bold")
F_LOG = ("Consolas", 9)
F_BTN = ("Segoe UI", 11, "bold")
F_BTN_SM = ("Segoe UI", 9, "bold")
F_FOOT = ("Georgia", 9, "italic")

QUALITY = {  # label -> bitrate
    "Good (128k)": "128k",
    "Better (192k)": "192k",
    "Best (320k)": "320k",
}

CAPTIONS = [
    "Sniffing out your tracks…",
    "On the scent of a good playlist…",
    "Fetching, boss.",
    "Locked onto the target song…",
    "Cool as ever, working on it…",
    "Tail's wagging, files are flowing…",
    "This one's a banger, hold on…",
]

SETTINGS_DIR = os.path.join(os.path.expanduser("~"), ".spotifilable")
SETTINGS_FILE = os.path.join(SETTINGS_DIR, "settings.json")


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------
def resource_path(name):
    """Works both from source and inside a PyInstaller --onefile build."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(sys.argv[0])))
    return os.path.join(base, name)


def safe_folder_name(name, fallback="Spotifilable"):
    name = (name or "").strip()
    name = re.sub(r'[\\/*?:"<>|]+', "", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name[:80] or fallback


def open_in_file_manager(path):
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception:
        pass


def friendly_error(err):
    """Turn scary tracebacks into something a human can act on."""
    s = str(err)
    low = s.lower()
    if "nameresolution" in low or "getaddrinfo" in low or "max retries" in low \
            or "connectionerror" in low or "connection error" in low or "timed out" in low:
        return "No internet, or YouTube/Spotify isn't reachable right now. Check your connection and try again."
    if "404" in low or "not found" in low or "non-existent" in low or "invalid" in low and "url" in low:
        return "Couldn't open that link. Is the playlist private, or was the link copied incorrectly?"
    if "ffmpeg" in low:
        return "The audio helper (FFmpeg) isn't ready. Try Download again to set it up."
    if "rate" in low and "limit" in low or "429" in low:
        return "Spotify is asking us to slow down (rate limit). Wait a minute and try again."
    if len(s) > 160:
        s = s[:157] + "…"
    return s or "Unknown error"


def list_removable_drives():
    """Return [(label, path)] for removable drives / mounted volumes."""
    drives = []
    if sys.platform.startswith("win"):
        try:
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            bitmask = kernel32.GetLogicalDrives()
            for i in range(26):
                if not bitmask & (1 << i):
                    continue
                root = f"{chr(65 + i)}:\\"
                dtype = kernel32.GetDriveTypeW(ctypes.c_wchar_p(root))
                if dtype != 2:  # DRIVE_REMOVABLE
                    continue
                buf = ctypes.create_unicode_buffer(261)
                kernel32.GetVolumeInformationW(ctypes.c_wchar_p(root), buf, 261,
                                               None, None, None, None, 0)
                label = buf.value or "Removable drive"
                drives.append((f"{label} ({root[:2]})", root))
        except Exception:
            pass
    else:
        for base in ("/media", "/run/media", "/Volumes"):
            if not os.path.isdir(base):
                continue
            for entry in os.listdir(base):
                p = os.path.join(base, entry)
                if os.path.isdir(p):
                    # /media/<user>/<volume> on Linux
                    subs = [os.path.join(p, s) for s in os.listdir(p)] if base != "/Volumes" else [p]
                    for sp in subs:
                        if os.path.isdir(sp) and os.access(sp, os.W_OK):
                            drives.append((os.path.basename(sp), sp))
    return drives


# ---------------------------------------------------------------------------
# Settings persistence
# ---------------------------------------------------------------------------
class Settings:
    DEFAULTS = {
        "output_dir": os.path.join(os.path.expanduser("~"), "Music", "Spotifilable"),
        "quality": "Better (192k)",
        "per_playlist_folder": True,
        "recent": [],  # [{"url","name","count","date"}]
    }

    def __init__(self):
        self.data = dict(self.DEFAULTS)
        self.load()

    def load(self):
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
            for k in self.DEFAULTS:
                if k in saved:
                    self.data[k] = saved[k]
        except Exception:
            pass

    def save(self):
        try:
            os.makedirs(SETTINGS_DIR, exist_ok=True)
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2)
        except Exception:
            pass

    def add_recent(self, url, name, count):
        url = url.split("?")[0]
        recent = [r for r in self.data["recent"] if r.get("url") != url]
        recent.insert(0, {"url": url, "name": name, "count": count,
                          "date": datetime.now().strftime("%Y-%m-%d")})
        self.data["recent"] = recent[:8]
        self.save()


# ---------------------------------------------------------------------------
# Mascot — a little gold dog in sunglasses, with moods
# ---------------------------------------------------------------------------
class Mascot:
    def __init__(self, canvas):
        self.c = canvas
        self.frame = 0
        self.mood = "idle"      # idle | working | happy | sad
        self._mood_until = 0
        self._tick()

    def set_mood(self, mood, seconds=None):
        self.mood = mood
        self._mood_until = (time.time() + seconds) if seconds else 0

    def _tick(self):
        if self._mood_until and time.time() > self._mood_until:
            self.mood, self._mood_until = "idle", 0
        self._draw()
        self.frame += 1
        self.c.after(60, self._tick)

    def _draw(self):
        c = self.c
        c.delete("all")
        f = self.frame
        bounce, wag, tilt = 0, 0, 0
        if self.mood == "working":
            bounce = math.sin(f / 6) * 6
            wag = 8 * math.sin(f / 4)
        elif self.mood == "happy":
            bounce = abs(math.sin(f / 4)) * -14
            wag = 12 * math.sin(f / 2)
        elif self.mood == "sad":
            bounce = 4
            tilt = 6
        else:  # idle: slow breathing
            bounce = math.sin(f / 30) * 2

        cx, cy = 60, 62 + bounce
        ear = "#3a2e22"
        # ears (droop when sad)
        c.create_oval(cx - 34, cy - 30 + tilt, cx - 12, cy + 2 + tilt, fill=ear, outline="")
        c.create_oval(cx + 12, cy - 30 + tilt, cx + 34, cy + 2 + tilt, fill=ear, outline="")
        # head
        c.create_oval(cx - 26, cy - 24, cx + 26, cy + 20, fill=GOLD, outline="")
        # snout + nose
        c.create_oval(cx - 10, cy + 2, cx + 10, cy + 16, fill="#f4e9d8", outline="")
        c.create_oval(cx - 3, cy + 6, cx + 3, cy + 11, fill="#2a2320", outline="")
        # sunglasses (slide down a bit when sad, blink occasionally otherwise)
        blink = self.mood in ("idle", "working") and (f // 20) % 9 == 0
        gy = cy + (5 if self.mood == "sad" else 0)
        if blink:
            c.create_line(cx - 22, gy - 3, cx - 2, gy - 3, fill=BG, width=2)
            c.create_line(cx + 2, gy - 3, cx + 22, gy - 3, fill=BG, width=2)
        else:
            c.create_rectangle(cx - 22, gy - 8, cx - 2, gy + 2, fill=BG, outline="")
            c.create_rectangle(cx + 2, gy - 8, cx + 22, gy + 2, fill=BG, outline="")
            c.create_line(cx - 2, gy - 4, cx + 2, gy - 4, fill=BG, width=3)
            if self.mood == "sad":  # peeking eyes above the glasses
                c.create_oval(cx - 15, gy - 12, cx - 9, gy - 8, fill="#2a2320", outline="")
                c.create_oval(cx + 9, gy - 12, cx + 15, gy - 8, fill="#2a2320", outline="")
        # mouth
        if self.mood == "happy":
            c.create_arc(cx - 8, cy + 6, cx + 8, cy + 18, start=200, extent=140, style="arc", outline="#2a2320", width=2)
        elif self.mood == "sad":
            c.create_arc(cx - 6, cy + 12, cx + 6, cy + 20, start=20, extent=140, style="arc", outline="#2a2320", width=2)
        # paw + toy blaster prop (cartoon)
        px, py = cx + 26, cy + 10
        c.create_oval(px - 5, py - 5, px + 5, py + 5, fill=ear, outline="")
        c.create_rectangle(px, py - 2, px + 26, py + 5, fill="#4a4a4a", outline="")
        c.create_rectangle(px + 20, py - 6, px + 26, py - 2, fill="#4a4a4a", outline="")
        # tail
        c.create_line(cx - 30, cy + 15, cx - 42, cy + 15 + wag, fill=ear, width=5)
        # sparkles when happy
        if self.mood == "happy":
            for k in range(4):
                a = f / 5 + k * math.pi / 2
                sx, sy = cx + math.cos(a) * 44, cy - 10 + math.sin(a) * 30
                c.create_text(sx, sy, text="✦", fill=GOLD, font=("Segoe UI", 10))


# ---------------------------------------------------------------------------
# Thin gold progress bar
# ---------------------------------------------------------------------------
class ProgressBar(tk.Canvas):
    def __init__(self, master, **kw):
        super().__init__(master, height=6, bg=PANEL_2, highlightthickness=0, **kw)
        self.value = 0.0
        self.bind("<Configure>", lambda e: self._redraw())

    def set(self, fraction):
        self.value = max(0.0, min(1.0, fraction))
        self._redraw()

    def _redraw(self):
        self.delete("all")
        w = self.winfo_width()
        if w > 1 and self.value > 0:
            self.create_rectangle(0, 0, int(w * self.value), 6, fill=GOLD, outline="")


# ---------------------------------------------------------------------------
# Main app
# ---------------------------------------------------------------------------
class Spotifilable:
    def __init__(self, root):
        self.root = root
        self.settings = Settings()
        root.title(APP_NAME)
        root.geometry("720x740")
        root.configure(bg=BG)
        root.minsize(640, 660)
        self._set_icon()

        self.q = queue.Queue()
        self.is_downloading = False
        self.cancel_event = threading.Event()
        self.ffmpeg_event = threading.Event()
        self.ffmpeg_granted = False
        self.failed_songs = []
        self.last_output_folder = None
        self.session_files = []       # files downloaded this session (for Send to Device)
        self.run_started = None

        self._build_ui()
        self._log("Hi! Paste a Spotify link above and press Download — or just press Enter.", "gold")
        os.makedirs(self.settings.data["output_dir"], exist_ok=True)

        root.bind("<Return>", lambda e: self.on_download())
        root.bind("<Escape>", lambda e: self.on_cancel())
        root.bind("<FocusIn>", self._maybe_grab_clipboard)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(300, self._maybe_grab_clipboard)
        root.after(120, self._poll)

    # ---------------- icon ----------------
    def _set_icon(self):
        try:
            p = resource_path("icon.ico")
            if os.path.exists(p):
                self.root.iconbitmap(p)
        except Exception:
            pass

    # ---------------- UI ----------------
    def _btn(self, master, text, cmd, primary=False, danger=False, **kw):
        if primary:
            bg, fg, abg, afg = GOLD, "#161616", GOLD_HOVER, "#161616"
        elif danger:
            bg, fg, abg, afg = PANEL, RED, "#2a1f1f", "#e79a9a"
        else:
            bg, fg, abg, afg = PANEL, GOLD, "#222", GOLD_HOVER
        return tk.Button(master, text=text, command=cmd, bg=bg, fg=fg,
                         activebackground=abg, activeforeground=afg,
                         relief="flat", cursor="hand2", bd=0,
                         disabledforeground=FAINT, **kw)

    def _build_ui(self):
        outer = tk.Frame(self.root, bg=BG, padx=34, pady=22)
        outer.pack(fill="both", expand=True)

        # header
        head = tk.Frame(outer, bg=BG)
        head.pack(fill="x")
        tcol = tk.Frame(head, bg=BG)
        tcol.pack(side="left", fill="x", expand=True)
        tk.Label(tcol, text=APP_NAME, font=F_TITLE, bg=BG, fg=GOLD).pack(anchor="w")
        tk.Label(tcol, text="Paste a link. It downloads itself.", font=F_SUB, bg=BG, fg=MUTED).pack(anchor="w", pady=(2, 0))
        self.mascot_canvas = tk.Canvas(head, width=120, height=110, bg=BG, highlightthickness=0)
        self.mascot_canvas.pack(side="right")
        self.mascot = Mascot(self.mascot_canvas)

        tk.Frame(outer, bg=BG, height=10).pack()

        # link row
        lrow_lbl = tk.Frame(outer, bg=BG)
        lrow_lbl.pack(fill="x")
        tk.Label(lrow_lbl, text="SPOTIFY LINK", font=F_TINY, bg=BG, fg=GOLD).pack(side="left")
        tk.Label(lrow_lbl, text="song · playlist · album", font=("Segoe UI", 8), bg=BG, fg=FAINT).pack(side="left", padx=8)

        lrow = tk.Frame(outer, bg=BG)
        lrow.pack(fill="x", pady=(4, 14))
        self.link = tk.Entry(lrow, font=F_LABEL, bg=PANEL_2, fg=IVORY, insertbackground=IVORY,
                             relief="flat", highlightthickness=1, highlightbackground=BORDER,
                             highlightcolor=GOLD)
        self.link.pack(side="left", fill="x", expand=True, ipady=8, padx=(1, 8))
        self._placeholder = "https://open.spotify.com/…"
        self._show_placeholder()
        self.link.bind("<FocusIn>", self._clear_placeholder)
        self.link.bind("<FocusOut>", lambda e: self._show_placeholder() if not self.link.get().strip() else None)
        self._context_menu(self.link)
        self._btn(lrow, "Paste", self.on_paste, font=F_BTN_SM, padx=12).pack(side="left", ipady=7)
        self.recent_btn = self._btn(lrow, "Recent ▾", self.on_recent, font=F_BTN_SM, padx=10)
        self.recent_btn.pack(side="left", ipady=7, padx=(6, 0))

        # folder row
        tk.Label(outer, text="SAVE TO", font=F_TINY, bg=BG, fg=GOLD).pack(anchor="w")
        frow = tk.Frame(outer, bg=BG)
        frow.pack(fill="x", pady=(4, 12))
        self.folder_var = tk.StringVar(value=self.settings.data["output_dir"])
        tk.Entry(frow, textvariable=self.folder_var, font=F_LABEL, bg=PANEL_2, fg=MUTED, relief="flat",
                 state="readonly", readonlybackground=PANEL_2).pack(side="left", fill="x", expand=True, ipady=8, padx=(1, 8))
        self._btn(frow, "Browse", self.on_browse, font=F_BTN_SM, padx=14).pack(side="left", ipady=7)

        # options row
        orow = tk.Frame(outer, bg=BG)
        orow.pack(fill="x", pady=(0, 14))
        tk.Label(orow, text="QUALITY", font=F_TINY, bg=BG, fg=GOLD).pack(side="left")
        self.quality_var = tk.StringVar(value=self.settings.data["quality"])
        qmenu = tk.OptionMenu(orow, self.quality_var, *QUALITY.keys(), command=lambda v: self._save_prefs())
        qmenu.configure(bg=PANEL, fg=IVORY, activebackground="#222", activeforeground=GOLD_HOVER,
                        relief="flat", bd=0, highlightthickness=0, font=F_BTN_SM, cursor="hand2", padx=10)
        qmenu["menu"].configure(bg=PANEL, fg=IVORY, activebackground=GOLD, activeforeground="#161616", bd=0)
        qmenu.pack(side="left", padx=(8, 18))
        self.subfolder_var = tk.BooleanVar(value=self.settings.data["per_playlist_folder"])
        tk.Checkbutton(orow, text="Folder per playlist", variable=self.subfolder_var, command=self._save_prefs,
                       bg=BG, fg=MUTED, activebackground=BG, activeforeground=IVORY,
                       selectcolor=PANEL_2, font=F_LABEL, cursor="hand2", bd=0,
                       highlightthickness=0).pack(side="left")

        # action row
        arow = tk.Frame(outer, bg=BG)
        arow.pack(fill="x", pady=(0, 10))
        self.download_btn = self._btn(arow, "⬇  DOWNLOAD", self.on_download, primary=True, font=F_BTN)
        self.download_btn.pack(side="left", fill="x", expand=True, ipady=12)
        self.cancel_btn = self._btn(arow, "✕ Cancel", self.on_cancel, danger=True, font=("Segoe UI", 10, "bold"),
                                    padx=10, state="disabled")
        self.cancel_btn.pack(side="left", padx=(10, 0), ipady=12)

        # progress + status
        self.progress = ProgressBar(outer)
        self.progress.pack(fill="x", pady=(0, 6))
        srow = tk.Frame(outer, bg=BG)
        srow.pack(fill="x", pady=(0, 8))
        self.status_var = tk.StringVar(value="Ready.")
        tk.Label(srow, textvariable=self.status_var, font=F_LABEL, bg=BG, fg=MUTED, anchor="w").pack(side="left", fill="x", expand=True)
        self.count_var = tk.StringVar(value="")
        tk.Label(srow, textvariable=self.count_var, font=F_LABEL, bg=BG, fg=GOLD, anchor="e").pack(side="right")

        # after-run row (packed BEFORE the log, pinned to the bottom, so it can never be squeezed out)
        tk.Label(outer, text="a quiet little corner of Spotify, just for you", font=F_FOOT, bg=BG, fg=FAINT
                 ).pack(side="bottom", anchor="e", pady=(6, 0))
        drow = tk.Frame(outer, bg=BG)
        drow.pack(side="bottom", fill="x", pady=(10, 0))
        self.open_btn = self._btn(drow, "Open Folder", self.on_open_folder, font=F_BTN_SM, padx=12, state="disabled")
        self.open_btn.pack(side="left", ipady=6)
        self.device_btn = self._btn(drow, "Send to Device", self.on_send_to_device, font=F_BTN_SM, padx=12, state="disabled")
        self.device_btn.pack(side="left", ipady=6, padx=(8, 0))
        self.retry_btn = self._btn(drow, "Retry Failed", self.on_retry_failed, font=F_BTN_SM, padx=12)
        # (retry button is packed only when there is something to retry)

        # log
        lp = tk.Frame(outer, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
        lp.pack(fill="both", expand=True)
        self.log_text = tk.Text(lp, bg="#111111", fg=MUTED, font=F_LOG, relief="flat", wrap="word",
                                padx=12, pady=10, state="disabled", cursor="arrow", height=6)
        self.log_text.pack(side="left", fill="both", expand=True, padx=1, pady=1)
        sb = tk.Scrollbar(lp, command=self.log_text.yview, bg=PANEL, troughcolor=PANEL, bd=0)
        sb.pack(side="right", fill="y")
        self.log_text.configure(yscrollcommand=sb.set)
        for tag, color in (("gold", GOLD), ("ivory", IVORY), ("muted", MUTED), ("error", RED), ("ok", GREEN)):
            self.log_text.tag_config(tag, foreground=color)

    # ---------------- entry helpers ----------------
    def _show_placeholder(self):
        self.link.delete(0, tk.END)
        self.link.insert(0, self._placeholder)
        self.link.configure(fg=FAINT)

    def _clear_placeholder(self, event=None):
        if self.link.get() == self._placeholder:
            self.link.delete(0, tk.END)
            self.link.configure(fg=IVORY)

    def _set_link(self, text):
        self.link.delete(0, tk.END)
        self.link.insert(0, text.strip())
        self.link.configure(fg=IVORY)

    def _current_link(self):
        v = self.link.get().strip()
        return "" if v == self._placeholder else v

    def _context_menu(self, w):
        m = tk.Menu(w, tearoff=0, bg=PANEL, fg=IVORY, activebackground=GOLD, activeforeground="#161616", bd=0)
        m.add_command(label="Cut", command=lambda: w.event_generate("<<Cut>>"))
        m.add_command(label="Copy", command=lambda: w.event_generate("<<Copy>>"))
        m.add_command(label="Paste", command=self.on_paste)
        m.add_separator()
        m.add_command(label="Select All", command=lambda: w.select_range(0, tk.END))
        m.add_command(label="Clear", command=lambda: (w.delete(0, tk.END), w.configure(fg=IVORY)))
        w.bind("<Button-3>", lambda e: m.tk_popup(e.x_root, e.y_root))

    def _maybe_grab_clipboard(self, event=None):
        """If the clipboard holds a Spotify link and the box is empty, use it."""
        if self.is_downloading or self._current_link():
            return
        try:
            clip = self.root.clipboard_get().strip()
        except Exception:
            return
        if "open.spotify.com/" in clip and len(clip) < 400 and "\n" not in clip:
            self._set_link(clip)
            self._status("Grabbed the link from your clipboard ✦  Press Download when ready.")

    # ---------------- log / status ----------------
    def _log(self, text, tag="muted"):
        self.log_text.configure(state="normal")
        self.log_text.insert(tk.END, text + "\n", tag)
        self.log_text.see(tk.END)
        self.log_text.configure(state="disabled")

    def _status(self, text):
        self.status_var.set(text)

    def _poll(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "line":
                    self._log(payload, "muted")
                elif kind == "ok":
                    self._log(payload, "ok")
                elif kind == "error":
                    self._log(payload, "error")
                elif kind == "gold":
                    self._log(payload, "gold")
                elif kind == "status":
                    self._status(payload)
                elif kind == "progress":
                    done, total = payload
                    self.progress.set(done / total if total else 0)
                    self.count_var.set(f"{done} / {total}")
                elif kind == "ask_ffmpeg":
                    self._ask_ffmpeg()
                elif kind == "done":
                    self._finish(payload)
        except queue.Empty:
            pass
        self.root.after(120, self._poll)

    # ---------------- actions ----------------
    def on_paste(self):
        try:
            clip = self.root.clipboard_get().strip()
        except Exception:
            clip = ""
        if clip:
            self._set_link(clip)
            self.link.focus_set()

    def on_recent(self):
        recent = self.settings.data.get("recent", [])
        m = tk.Menu(self.root, tearoff=0, bg=PANEL, fg=IVORY, activebackground=GOLD, activeforeground="#161616", bd=0)
        if not recent:
            m.add_command(label="Nothing yet — your playlists will show up here", state="disabled")
        for r in recent:
            label = f"{r.get('name', 'Playlist')}  ·  {r.get('count', '?')} tracks  ·  {r.get('date', '')}"
            m.add_command(label=label, command=lambda u=r["url"]: self._set_link(u))
        if recent:
            m.add_separator()
            m.add_command(label="Clear list", command=self._clear_recent)
        x = self.recent_btn.winfo_rootx()
        y = self.recent_btn.winfo_rooty() + self.recent_btn.winfo_height()
        m.tk_popup(x, y)

    def _clear_recent(self):
        self.settings.data["recent"] = []
        self.settings.save()

    def on_browse(self):
        chosen = filedialog.askdirectory(initialdir=self.settings.data["output_dir"], title="Choose where to save music")
        if chosen:
            self.settings.data["output_dir"] = chosen
            self.folder_var.set(chosen)
            self._save_prefs()

    def _save_prefs(self, *_):
        self.settings.data["quality"] = self.quality_var.get()
        self.settings.data["per_playlist_folder"] = bool(self.subfolder_var.get())
        self.settings.save()

    def on_download(self):
        if self.is_downloading:
            return
        link = self._current_link()
        if not link:
            messagebox.showwarning("Missing link", "Paste a Spotify song, playlist or album link first.")
            return
        if "open.spotify.com" not in link and not link.startswith("spotify:"):
            messagebox.showwarning("Hmm", "That doesn't look like a Spotify link.")
            return
        self._start_run(link=link, songs=None)

    def on_retry_failed(self):
        if self.is_downloading or not self.failed_songs:
            return
        self._start_run(link=None, songs=list(self.failed_songs))

    def _start_run(self, link, songs):
        self._save_prefs()
        # Snapshot everything the worker needs NOW, on the main thread.
        # (Tk variables must never be touched from a background thread.)
        self.run_opts = {
            "base_dir": self.settings.data["output_dir"],
            "bitrate": QUALITY.get(self.quality_var.get(), "192k"),
            "per_playlist_folder": bool(self.subfolder_var.get()),
        }
        self.is_downloading = True
        self.cancel_event.clear()
        self.failed_songs = []
        self.session_files = []
        self.run_started = time.time()
        self.retry_btn.pack_forget()
        self.download_btn.configure(state="disabled", text="Downloading…", bg=GOLD_DIM)
        self.cancel_btn.configure(state="normal", text="✕ Cancel")
        self.open_btn.configure(state="disabled")
        self.device_btn.configure(state="disabled")
        self.progress.set(0)
        self.count_var.set("")
        self.mascot.set_mood("working")
        self._log("", "muted")
        self._log(("Retrying failed tracks…" if songs else f"Starting: {link}"), "gold")
        t = threading.Thread(target=self._worker, args=(link, songs), daemon=True)
        t.start()

    def on_cancel(self):
        if not self.is_downloading:
            return
        self.cancel_event.set()
        self.cancel_btn.configure(state="disabled", text="Cancelling…")
        self._status("Cancelling after the current track…")

    def on_open_folder(self):
        if self.last_output_folder and os.path.isdir(self.last_output_folder):
            open_in_file_manager(self.last_output_folder)

    def on_close(self):
        if self.is_downloading:
            if not messagebox.askyesno("Still downloading", "A download is still running. Quit anyway?"):
                return
        self.root.destroy()

    def _ask_ffmpeg(self):
        ok = messagebox.askyesno(
            "One-time setup",
            "Spotifilable needs to download a small helper tool (FFmpeg, about 80 MB) "
            "to convert audio.\n\nThis only happens once. Continue?")
        self.ffmpeg_granted = ok
        self.ffmpeg_event.set()

    def _finish(self, summary):
        self.is_downloading = False
        elapsed = int(time.time() - (self.run_started or time.time()))
        mins, secs = divmod(elapsed, 60)
        took = f" ({mins}m {secs}s)" if mins else f" ({secs}s)"
        self.download_btn.configure(state="normal", text="⬇  DOWNLOAD", bg=GOLD)
        self.cancel_btn.configure(state="disabled", text="✕ Cancel")
        self._status(summary["status"] + took)
        self._log(summary["status"] + took, "gold")
        if summary.get("folder"):
            self.last_output_folder = summary["folder"]
            self.open_btn.configure(state="normal")
            if self.session_files:
                self.device_btn.configure(state="normal")
        if summary.get("failed"):
            self.failed_songs = summary["failed"]
            self.retry_btn.configure(text=f"Retry Failed ({len(self.failed_songs)})")
            self.retry_btn.pack(side="left", ipady=6, padx=(8, 0))
        self.mascot.set_mood("happy" if summary.get("ok", 0) and not summary.get("failed") else
                             ("sad" if summary.get("failed") and not summary.get("ok") else "happy"), seconds=4)
        try:
            self.root.bell()
        except Exception:
            pass

    # ---------------- background worker ----------------
    def _heartbeat(self, base):
        stop = threading.Event()

        def tick():
            start = time.time()
            while not stop.is_set():
                el = int(time.time() - start)
                if el >= 3:
                    self.q.put(("status", f"{base} — still working ({el}s)"))
                stop.wait(1)

        threading.Thread(target=tick, daemon=True).start()
        return stop

    def _make_client(self, output_template, bitrate):
        return Spotdl(
            client_id=DEFAULT_CLIENT_ID,
            client_secret=DEFAULT_CLIENT_SECRET,
            downloader_settings={
                "output": output_template,
                "format": "mp3",
                "bitrate": bitrate,
                "overwrite": "skip",
                "print_errors": False,
                "simple_tui": True,
                "log_level": "ERROR",
            },
        )

    def _worker(self, link, songs):
        socket.setdefaulttimeout(20)
        opts = self.run_opts
        base_dir = opts["base_dir"]
        try:
            # --- ffmpeg (one time) ---
            if not is_ffmpeg_installed():
                self.ffmpeg_event.clear()
                self.q.put(("ask_ffmpeg", None))
                if not self.ffmpeg_event.wait(timeout=180) or not self.ffmpeg_granted:
                    self.q.put(("done", {"status": "Cancelled — the audio helper is needed to continue.", "ok": 0}))
                    return
                self.q.put(("status", "Setting up the audio helper (one time)…"))
                hb = self._heartbeat("Setting up the audio helper")
                try:
                    download_ffmpeg()
                finally:
                    hb.set()
                self.q.put(("ok", "✓ Audio helper ready. You won't see this again."))

            # --- resolve tracks ---
            client = self._make_client(os.path.join(base_dir, "{artist} - {title}.{output-ext}"), opts["bitrate"])
            if songs is None:
                self.q.put(("status", "Reading the playlist…"))
                hb = self._heartbeat("Reading the playlist")
                try:
                    songs = client.search([link])
                finally:
                    hb.set()
                if not songs:
                    self.q.put(("done", {"status": "Nothing found at that link. Is the playlist private or empty?", "ok": 0}))
                    return

            total = len(songs)
            list_name = getattr(songs[0], "list_name", None) or (songs[0].album_name if total > 1 else None)
            is_collection = total > 1 or bool(getattr(songs[0], "list_url", None))
            folder = base_dir
            if opts["per_playlist_folder"] and is_collection:
                folder = os.path.join(base_dir, safe_folder_name(list_name, "Playlist"))
            os.makedirs(folder, exist_ok=True)
            client.downloader.settings["output"] = os.path.join(folder, "{artist} - {title}.{output-ext}")

            self.q.put(("line", f"Found {total} track(s)" + (f" in \"{list_name}\"" if list_name and is_collection else "") + "."))
            self.q.put(("line", f"Saving to: {folder}"))
            self.q.put(("progress", (0, total)))
            if link and is_collection:
                self.settings.add_recent(link, list_name or "Playlist", total)

            # --- download loop ---
            ok, failed_list, cancelled = 0, [], False
            for i, song in enumerate(songs, start=1):
                if self.cancel_event.is_set():
                    cancelled = True
                    break
                base = f"{random.choice(CAPTIONS)}  ({i}/{total})"
                self.q.put(("status", base))
                hb = self._heartbeat(base)
                try:
                    _, path = client.download(song)
                    if path:
                        ok += 1
                        self.session_files.append(str(path))
                        self.q.put(("ok", f"✓ {song.artist} - {song.name}"))
                    else:
                        failed_list.append(song)
                        self.q.put(("error", f"✗ {song.artist} - {song.name} — no matching audio found"))
                except Exception as e:
                    failed_list.append(song)
                    self.q.put(("error", f"✗ {song.artist} - {song.name} — {friendly_error(e)}"))
                finally:
                    hb.set()
                self.q.put(("progress", (i, total)))

            if cancelled:
                status = f"Cancelled — {ok} downloaded before you stopped it."
            elif failed_list:
                status = f"Done — {ok} downloaded, {len(failed_list)} couldn't be fetched."
            else:
                status = f"Done — all {ok} track(s) downloaded ✦"
            self.q.put(("done", {"status": status, "ok": ok, "failed": failed_list, "folder": folder}))

        except Exception as e:
            self.q.put(("done", {"status": friendly_error(e), "ok": 0}))

    # ---------------- Send to Device ----------------
    def on_send_to_device(self):
        if self.is_downloading:
            return
        files = [f for f in self.session_files if os.path.isfile(f)]
        if not files and self.last_output_folder:
            files = [os.path.join(self.last_output_folder, f) for f in os.listdir(self.last_output_folder)
                     if f.lower().endswith((".mp3", ".m4a", ".flac", ".ogg", ".opus", ".wav"))]
        if not files:
            messagebox.showinfo("Nothing to send", "Download something first, then send it to a device.")
            return

        drives = list_removable_drives()
        win = tk.Toplevel(self.root)
        win.title("Send to Device")
        win.configure(bg=BG)
        win.resizable(False, False)
        win.transient(self.root)
        win.grab_set()
        pad = tk.Frame(win, bg=BG, padx=24, pady=20)
        pad.pack()
        tk.Label(pad, text="Send to Device", font=("Georgia", 16, "bold"), bg=BG, fg=GOLD).pack(anchor="w")
        tk.Label(pad, text=f"Copy {len(files)} track(s) onto a Walkman, USB stick or SD card.",
                 font=F_LABEL, bg=BG, fg=MUTED).pack(anchor="w", pady=(2, 12))

        choice = tk.StringVar(value=drives[0][1] if drives else "")
        if drives:
            for label, path in drives:
                tk.Radiobutton(pad, text=label, variable=choice, value=path, bg=BG, fg=IVORY,
                               selectcolor=PANEL_2, activebackground=BG, activeforeground=GOLD,
                               font=F_LABEL, cursor="hand2", bd=0, highlightthickness=0).pack(anchor="w")
        else:
            tk.Label(pad, text="No removable drive found. Plug in the device, then press Refresh —\nor pick a folder manually.",
                     font=F_LABEL, bg=BG, fg=RED, justify="left").pack(anchor="w")

        sub_var = tk.StringVar(value="MUSIC")
        srow = tk.Frame(pad, bg=BG)
        srow.pack(fill="x", pady=(12, 4))
        tk.Label(srow, text="Folder on device:", font=F_LABEL, bg=BG, fg=MUTED).pack(side="left")
        tk.Entry(srow, textvariable=sub_var, font=F_LABEL, bg=PANEL_2, fg=IVORY, insertbackground=IVORY,
                 relief="flat", width=18).pack(side="left", padx=8, ipady=4)

        brow = tk.Frame(pad, bg=BG)
        brow.pack(fill="x", pady=(14, 0))

        def refresh():
            win.destroy()
            self.on_send_to_device()

        def pick_manual():
            d = filedialog.askdirectory(title="Choose the device / folder")
            if d:
                choice.set(d)
                go()

        def go():
            dest_root = choice.get()
            if not dest_root:
                messagebox.showwarning("No device", "Pick a device or choose a folder manually.", parent=win)
                return
            sub = safe_folder_name(sub_var.get(), "MUSIC")
            playlist_folder = os.path.basename(self.last_output_folder or "") if self.last_output_folder else ""
            dest = os.path.join(dest_root, sub, playlist_folder) if playlist_folder and playlist_folder != os.path.basename(self.settings.data["output_dir"]) else os.path.join(dest_root, sub)
            win.destroy()
            self._copy_to_device(files, dest)

        self._btn(brow, "Refresh", refresh, font=F_BTN_SM, padx=10).pack(side="left", ipady=5)
        self._btn(brow, "Choose folder…", pick_manual, font=F_BTN_SM, padx=10).pack(side="left", ipady=5, padx=(6, 0))
        self._btn(brow, "Copy →", go, primary=True, font=F_BTN_SM, padx=16).pack(side="right", ipady=5)

    def _copy_to_device(self, files, dest):
        self.device_btn.configure(state="disabled")
        self.mascot.set_mood("working")
        self._log("", "muted")
        self._log(f"Sending {len(files)} track(s) to {dest}", "gold")

        def work():
            copied, skipped, errors = 0, 0, 0
            try:
                os.makedirs(dest, exist_ok=True)
            except Exception as e:
                self.q.put(("done", {"status": f"Couldn't write to the device: {friendly_error(e)}", "ok": 0}))
                return
            total = len(files)
            self.q.put(("progress", (0, total)))
            for i, src in enumerate(files, start=1):
                name = os.path.basename(src)
                target = os.path.join(dest, name)
                try:
                    if os.path.exists(target) and os.path.getsize(target) == os.path.getsize(src):
                        skipped += 1
                    else:
                        shutil.copy2(src, target)
                        copied += 1
                        self.q.put(("ok", f"→ {name}"))
                except Exception as e:
                    errors += 1
                    self.q.put(("error", f"✗ {name} — {friendly_error(e)}"))
                self.q.put(("progress", (i, total)))
                self.q.put(("status", f"Copying to device… ({i}/{total})"))
            msg = f"Sent {copied} track(s) to the device"
            if skipped:
                msg += f", {skipped} already there"
            if errors:
                msg += f", {errors} failed"
            msg += ". Safely eject before unplugging ✦"
            self.q.put(("done", {"status": msg, "ok": copied, "folder": self.last_output_folder}))

        self.is_downloading = True
        self.run_started = time.time()
        self.download_btn.configure(state="disabled")
        threading.Thread(target=work, daemon=True).start()


def main():
    root = tk.Tk()
    Spotifilable(root)
    root.mainloop()


if __name__ == "__main__":
    main()
