import os, sys, time, tempfile, types, json
import tkinter as tk
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import spotifilable as S

# ---- fake spotDL backend ----
class FakeSong:
    def __init__(self, i, fail=False):
        self.name = f"Song {i}"
        self.artist = "Artist"
        self.album_name = "Album"
        self.list_name = "My Cool Playlist"
        self.list_url = "https://open.spotify.com/playlist/abc"
        self.fail = fail

class FakeDownloader:
    def __init__(self):
        self.settings = {}

class FakeClient:
    calls = []
    def __init__(self, client_id, client_secret, downloader_settings=None):
        self.downloader = FakeDownloader()
        self.downloader.settings.update(downloader_settings or {})
    def search(self, q):
        FakeClient.calls.append(("search", q))
        return [FakeSong(i, fail=(i == 3)) for i in range(1, 6)]
    def download(self, song):
        time.sleep(0.05)
        if song.fail:
            raise ConnectionError("HTTPSConnectionPool(host='music.youtube.com'): Max retries exceeded")
        out = self.downloader.settings["output"].replace("{artist}", song.artist).replace("{title}", song.name).replace("{output-ext}", "mp3")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "wb") as f:
            f.write(b"\x00" * 1000)
        return song, out

S.Spotdl = FakeClient
S.is_ffmpeg_installed = lambda *a, **k: True

# isolate settings
tmp = tempfile.mkdtemp()
S.SETTINGS_DIR = os.path.join(tmp, "cfg"); S.SETTINGS_FILE = os.path.join(S.SETTINGS_DIR, "settings.json")
S.Settings.DEFAULTS["output_dir"] = os.path.join(tmp, "Music")

def pump(root, seconds):
    end = time.time() + seconds
    while time.time() < end:
        root.update(); time.sleep(0.02)

root = tk.Tk()
app = S.Spotifilable(root)
pump(root, 0.3)

# 1) download run with one failure
app._set_link("https://open.spotify.com/playlist/abc?si=1")
app.on_download()
pump(root, 2.5)
assert not app.is_downloading, "run should have finished"
print("status:", app.status_var.get())
assert "4 downloaded" in app.status_var.get() and "1 couldn't" in app.status_var.get()
assert len(app.failed_songs) == 1
folder = app.last_output_folder
print("folder:", folder)
assert os.path.basename(folder) == "My Cool Playlist"
assert len([f for f in os.listdir(folder) if f.endswith(".mp3")]) == 4
assert app.count_var.get() == "5 / 5"
assert app.retry_btn.winfo_manager() == "pack", "retry button should be visible"

# 2) recent list persisted
cfg = json.load(open(S.SETTINGS_FILE))
assert cfg["recent"][0]["name"] == "My Cool Playlist" and cfg["recent"][0]["count"] == 5
print("recent:", cfg["recent"][0])

# 3) retry failed (make it succeed this time)
app.failed_songs[0].fail = False
app.on_retry_failed()
pump(root, 1.5)
assert "all 1 track" in app.status_var.get(), app.status_var.get()
assert app.retry_btn.winfo_manager() == "", "retry button should be hidden after success"

# 4) cancel mid-run
FakeClient.download = lambda self, song: (time.sleep(0.3), (song, None))[1] if False else _slow(self, song)
def _slow(self, song):
    time.sleep(0.3)
    return song, None
app._set_link("https://open.spotify.com/playlist/abc")
app.on_download()
pump(root, 0.5)
app.on_cancel()
pump(root, 1.2)
assert "Cancelled" in app.status_var.get(), app.status_var.get()
print("cancel:", app.status_var.get())

# 5) send to device (manual folder path)
dev = os.path.join(tmp, "WALKMAN")
os.makedirs(dev)
app.session_files = [os.path.join(folder, f) for f in os.listdir(folder) if f.endswith(".mp3")]
app._copy_to_device(app.session_files, os.path.join(dev, "MUSIC", "My Cool Playlist"))
pump(root, 1.0)
copied = os.listdir(os.path.join(dev, "MUSIC", "My Cool Playlist"))
print("device:", copied)
assert len(copied) == 5
assert "Sent 5" in app.status_var.get()

# 6) friendly errors
assert "internet" in S.friendly_error(Exception("NameResolutionError getaddrinfo failed")).lower()
assert "private" in S.friendly_error(Exception("404 not found")).lower()

# 7) clipboard auto-grab
root.clipboard_clear(); root.clipboard_append("https://open.spotify.com/track/xyz")
app._show_placeholder(); app._maybe_grab_clipboard()
assert app._current_link() == "https://open.spotify.com/track/xyz"

root.destroy()
print("ALL TESTS PASSED")
