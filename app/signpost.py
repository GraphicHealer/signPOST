"""
signPOST — REST-controlled digital signage for Interstate 75 W (RP2350).

Runs a tiny HTTP API (plus a minimal web UI) on the board for uploading
PNG/JPEG images to flash and drawing them on a 128x128 HUB75 matrix
(two chained 128x64 panels in a vertical arrangement).

Works nicely with Home Assistant `rest_command`, FPP scripts, curl or
the built-in web page.

WiFi:
  - Credentials live in secrets.py (WIFI_SSID / WIFI_PASSWORD).
  - If no credentials are configured (or connecting fails), the board starts
    an open AP called "signPOST-Setup" with a captive portal at 192.168.4.1 for
    picking a network and entering its password.
  - Factory reset the WiFi config by holding the front A button for ~3s at
    power on, or by power-cycling the board 4 times within ~15s.

Setup:
  1. Copy main.py and gifdec.py to the board (eg: mpremote cp main.py gifdec.py :)
  2. Optionally copy secrets.py too, if you prefer configuring over USB.
  3. Reset the board - it shows its IP (or the setup AP details) on the matrix.
     The board also answers to MDNS_NAME.local (default: signpost.local).

All uploads must be exactly 128x128. GIFs are decoded on-board into a hidden
frame album and listed as {"type": "gif"} — show/scroll them by filename.
After a reboot the last display state (image, album, slideshow, text) resumes.

Quick API tour (http://<board-ip>):
  GET    /api/images                  list images, gifs & albums on the board
  PUT    /api/images/<name>           upload an image (raw body, ?show=1 to display it)
  PUT    /api/images/<name>.gif       upload a gif (decoded to a hidden album)
  GET    /api/images/<name>           download an image / first frame of a gif
  DELETE /api/images/<name>           delete an image, gif, or a whole album
  PUT    /api/images/<album>/<frame>  upload a frame into an album (plays as animation)
  POST   /api/show/<name>             display an image, album, gif, or album frame
  POST   /api/show                    {"image": "<name>", "interval_ms": 40, "loops": 0}
  POST   /api/slideshow               {"interval": 5, "album": "x"} cycle images/album/gif
  POST   /api/random                  display a random image
  POST   /api/text                    {"text": "hi", "x": 2, "y": 60, "scale": 1,
                                     "r": 255, "g": 255, "b": 255,
                                     "scroll": true, "speed_ms": 80, "step": 2}
  POST   /api/led                     {"r": 0, "g": 64, "b": 0}
  POST   /api/clear                   stop playback and blank the display
  GET    /api/status                  mode, current image, wifi, storage, buttons, mdns, utc
  GET    /api/schedule                read the on/off schedule config
  POST   /api/schedule                {"enabled": true, "on": "18:00", "off": "23:00",
                                     "mode": "show", "show": "demo", "off_show": "my-image.png",
                                     "tz": -5, "interval": 8}
  GET    /api/scan                    scan for nearby WiFi networks
  POST   /api/connect                 {"ssid": "...", "password": "..."} save wifi & reboot
  GET    /setup                       wifi setup page (captive portal when in AP mode)
  GET    /api/display                 panel layout + driver config
  POST   /api/display                 {"display": "64x64"} or {"panel_w","panel_h","cols","rows"}
                                      (+ "panel", "color_order", "stb_invert") — needs reboot
  GET    /settings                    display settings page (layout preview)
  POST   /api/reboot                  reboot the board
  POST   /api/factory-reset           wipe wifi credentials & reboot into setup AP

Front buttons: A = next root image, B = toggle slideshow.
Hold A+B for ~2s to open the menu (A cycles, B selects): next image,
slideshow, clear, Reset (network/factory, with confirm), Exit.
"""

import os
import gc
import json
import random
import socket
import select
import struct
import time
import asyncio
from asyncio import core as _core

import machine
import network
import ntptime

from interstate75 import Interstate75, SWITCH_A, SWITCH_B
from ezwifi import EzWiFi
from gifdec import GifDecoder, GifError
from prealloc import (DISPLAY_MODES, PANEL_TYPES, COLOR_ORDERS,
                      DISPLAY_CFG_FILE, CFG as _dhw_cfg,
                      load_display_config as _load_display_config,
                      I75 as _I75, GRAPHICS as _GRAPHICS,
                      PNG as _PNG, JPEG as _JPEG, GIF_BUFS as _GIF_BUFS)

# -- Configuration --------------------------------------------------------

PORT = 80

IMG_DIR = "images"
MAX_UPLOAD_BYTES = 512 * 1024
MAX_GIF_BYTES = 4 * 1024 * 1024   # gif bodies are stream-decoded, never stored
DEFAULT_ANIM_MS = 40        # frame delay when playing an album
DEFAULT_SLIDESHOW_S = 5.0   # seconds per image in slideshow mode
REQUEST_TIMEOUT_S = 300     # gif decode of a big upload can take a while
KEEPALIVE_IDLE_S = 30       # max idle time held on a keep-alive connection
BOOT_MESSAGE_S = 30         # how long the hostname/SSID/IP screen stays up

STATE_FILE = "state.json"       # last display mode, resumed on boot
SCHEDULE_FILE = "schedule.json" # optional on/off window for autonomous sign use
DISPLAY_CFG_FILE = "display.json"  # panel layout / driver options, applied at boot
MDNS_NAME = "signpost"         # -> http://signpost.local

# WiFi setup / factory reset
AP_SSID = "signPOST-Setup"
AP_IP = "192.168.4.1"
SECRETS_FILE = "secrets.py"
BOOT_COUNT_FILE = "bootcount"
RESET_BOOTS = 4             # power-cycles within the window that trigger a reset
RESET_STABLE_S = 15         # uptime required for the boot counter to clear
RESET_BUTTON = SWITCH_A     # front button held at power-on to trigger a reset
RESET_HOLD_S = 3

_NAME_CHARS = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"

_REASONS = {
    200: "OK", 201: "Created", 204: "No Content", 400: "Bad Request",
    404: "Not Found", 405: "Method Not Allowed", 411: "Length Required",
    413: "Payload Too Large", 415: "Unsupported Media Type",
    500: "Internal Server Error", 507: "Insufficient Storage",
}

# -- Filesystem helpers ---------------------------------------------------


def _text_w(g, text, scale):
    """measure_text() returns a (w, h) tuple on some picographics builds
    and a bare int on others."""
    w = g.measure_text(text, scale)
    try:
        return int(w)
    except TypeError:
        return int(w[0])


def _is_image_name(name):
    n = name.lower()
    return any(n.endswith(e) for e in (".png", ".jpg", ".jpeg"))


def _is_gif_name(name):
    return name.lower().endswith(".gif")


def _is_gif_frame(name):
    """Decoded gif frames: fNNN.png stored in the hidden .name.gif dir."""
    return name.startswith("f") and name.endswith(".png")


def _gif_dir(name):
    """Hidden album dir backing a gif: 'x.gif' -> 'images/.x.gif'."""
    return IMG_DIR + "/." + name


def _gif_meta(gdir):
    """Read a gif's saved timing: -> (interval_ms, [per-frame delays] | None)."""
    try:
        with open(gdir + "/meta.json") as f:
            m = json.loads(f.read())
        return m.get("interval", DEFAULT_ANIM_MS), m.get("delays")
    except (OSError, ValueError):
        return DEFAULT_ANIM_MS, None


# -- Schedule (autonomous on/off window) -------------------------------------

_SCHEDULE_DEFAULTS = {"enabled": False, "on": "18:00", "off": "23:00",
                      "mode": "show", "show": "", "off_show": "",
                      "tz": 0, "interval": 8}


def _save_display_config(cfg):
    with open(DISPLAY_CFG_FILE, "w") as f:
        f.write(json.dumps(cfg))


def _load_schedule():
    try:
        with open(SCHEDULE_FILE) as f:
            cfg = json.loads(f.read())
    except (OSError, ValueError):
        cfg = {}
    out = dict(_SCHEDULE_DEFAULTS)
    out.update(cfg)
    return out


def _save_schedule(cfg):
    with open(SCHEDULE_FILE, "w") as f:
        f.write(json.dumps(cfg))


def _hhmm(s):
    try:
        h, m = s.split(":")
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        return None


def _valid_name(name):
    return (
        0 < len(name) <= 64
        and "/" not in name
        and "\\" not in name
        and ".." not in name
        and not name.startswith(".")
        and all(c in _NAME_CHARS for c in name)
    )


def _stat(path):
    try:
        return os.stat(path)
    except OSError:
        return None


def _is_dir(path):
    st = _stat(path)
    return st is not None and (st[0] & 0x4000) != 0


def _is_file(path):
    st = _stat(path)
    return st is not None and (st[0] & 0x4000) == 0


def _ensure_dir(path):
    if not _is_dir(path):
        os.mkdir(path)


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _free_space():
    try:
        s = os.statvfs("/")
        return s[0] * s[3]
    except (OSError, AttributeError):
        return 0


def _delete_entry(path):
    if _is_dir(path):
        for f in os.listdir(path):
            os.remove(path + "/" + f)
        os.rmdir(path)
        return True
    if _is_file(path):
        os.remove(path)
        return True
    return False


def _list_images():
    out = []
    try:
        entries = sorted(os.listdir(IMG_DIR))
    except OSError:
        return out
    for name in entries:
        path = IMG_DIR + "/" + name
        if name.startswith("."):
            # hidden dir backing an uploaded gif: ".x.gif" -> "x.gif"
            if name.endswith(".gif") and _is_dir(path):
                frames = sum(1 for f in os.listdir(path) if _is_gif_frame(f))
                out.append({"name": name[1:], "type": "gif", "frames": frames})
            continue
        if _is_dir(path):
            frames = sorted(f for f in os.listdir(path) if _is_image_name(f))
            out.append({"name": name, "type": "album", "frames": len(frames)})
        elif _is_image_name(name):
            out.append({"name": name, "type": "image", "size": _stat(path)[6]})
    return out


def _list_image_files():
    try:
        names = sorted(os.listdir(IMG_DIR))
    except OSError:
        return []
    return [n for n in names if _is_image_name(n) and _is_file(IMG_DIR + "/" + n)]


def _unquote(s):
    if "%" not in s and "+" not in s:
        return s
    out = ""
    i = 0
    while i < len(s):
        c = s[i]
        if c == "%" and i + 2 < len(s):
            out += chr(int(s[i + 1:i + 3], 16))
            i += 3
        elif c == "+":
            out += " "
            i += 1
        else:
            out += c
            i += 1
    return out


def _ctype_for(name):
    n = name.lower()
    if n.endswith(".png"):
        return "image/png"
    if n.endswith(".bin"):
        return "application/octet-stream"
    return "image/jpeg"


def _json(data):
    return json.dumps(data)


def _log_exc(e):
    try:
        import sys
        with open("errors.log", "a") as f:
            sys.print_exception(e, f)
    except OSError:
        pass


class HttpError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


# -- WiFi credential / factory reset helpers -------------------------------


def _load_wifi_creds():
    try:
        from secrets import WIFI_SSID, WIFI_PASSWORD
    except ImportError:
        return None
    if not WIFI_SSID:
        return None
    return WIFI_SSID, WIFI_PASSWORD


def _save_wifi_creds(ssid, password):
    def esc(s):
        return s.replace("\\", "\\\\").replace('"', '\\"')
    with open(SECRETS_FILE, "w") as f:
        f.write('WIFI_SSID = "{}"\nWIFI_PASSWORD = "{}"\n'.format(esc(ssid), esc(password)))


def _read_bootcount():
    try:
        with open(BOOT_COUNT_FILE) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return 0


def _write_bootcount(n):
    try:
        with open(BOOT_COUNT_FILE, "w") as f:
            f.write(str(n))
    except OSError:
        pass


def _factory_reset():
    _remove(SECRETS_FILE)
    _write_bootcount(0)


def _full_factory_reset():
    """Back to stock: creds, state, schedule, display config, all images."""
    for f in (SECRETS_FILE, STATE_FILE, SCHEDULE_FILE, DISPLAY_CFG_FILE):
        _remove(f)
    if _is_dir(IMG_DIR):
        _delete_entry(IMG_DIR)
    _write_bootcount(0)


def _check_bootcount_reset():
    """Returns True when the board has been power-cycled RESET_BOOTS times
    without reaching RESET_STABLE_S seconds of uptime."""
    count = _read_bootcount() + 1
    _write_bootcount(count)
    return count >= RESET_BOOTS


async def _clear_bootcount_later():
    await asyncio.sleep(RESET_STABLE_S)
    _write_bootcount(0)


def _check_button_reset(dm):
    """True when RESET_BUTTON is held for RESET_HOLD_S seconds at boot."""
    if not dm.i75.switch_pressed(RESET_BUTTON):
        return False
    for i in range(RESET_HOLD_S, 0, -1):
        dm.status_message("WiFi reset", "keep holding...", "{}...".format(i))
        if not dm.i75.switch_pressed(RESET_BUTTON):
            return False
        time.sleep(1)
    return True


async def _reboot_later(delay=1.5):
    await asyncio.sleep(delay)
    machine.reset()


def _dns_response(query, ip):
    """Build a DNS A-record response pointing every query at `ip`
    (captive portal wildcard)."""
    if len(query) < 13:
        return None
    i = 12
    while i < len(query):
        label_len = query[i]
        if label_len == 0:
            i += 5   # root label + qtype(2) + qclass(2)
            break
        i += label_len + 1
    else:
        return None
    if i > len(query):
        return None
    return (query[0:2] + b"\x81\x80" + query[4:6] + b"\x00\x01\x00\x00\x00\x00"
            + query[12:i]
            + b"\xc0\x0c\x00\x01\x00\x01\x00\x00\x00\x3c\x00\x04" + ip)


async def _dns_task(server):
    """Wildcard DNS responder for the setup AP - sends all lookups to AP_IP
    so captive-portal detection kicks in on phones/laptops."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setblocking(False)
        s.bind(("0.0.0.0", 53))
    except OSError:
        return
    poll = select.poll()
    poll.register(s, select.POLLIN)
    ip = bytes(int(o) for o in AP_IP.split("."))
    try:
        while server.portal_mode:
            for _sock, _ev in poll.poll(200):
                try:
                    data, addr = s.recvfrom(512)
                except OSError:
                    continue
                resp = _dns_response(data, ip)
                if resp:
                    s.sendto(resp, addr)
            await asyncio.sleep(0.02)
    finally:
        s.close()


# -- Network manager --------------------------------------------------------


class NetManager:
    """Wraps EzWiFi (STA) plus the setup AP and network scanning."""

    def __init__(self):
        self.wifi = EzWiFi(verbose=True)
        self.sta = network.WLAN(network.STA_IF)
        self.ap = network.WLAN(network.AP_IF)
        self.creds = _load_wifi_creds()

    async def connect(self, retries=3):
        if not self.creds:
            return False
        ssid, password = self.creds
        return await self.wifi.connect(ssid=ssid, password=password, retries=retries)

    def start_ap(self):
        # security=0 keeps the setup network open - the CYW43 firmware
        # defaults to WPA2 when no password is given
        try:
            self.ap.config(essid=AP_SSID, security=0)
        except TypeError:
            self.ap.config(ssid=AP_SSID)
        try:
            self.ap.ifconfig((AP_IP, "255.255.255.0", AP_IP, AP_IP))
        except (OSError, TypeError, ValueError):
            pass
        self.ap.active(True)

    def stop_ap(self):
        try:
            self.ap.active(False)
        except OSError:
            pass

    def scan(self):
        nets = {}
        try:
            results = self.sta.scan()
        except (OSError, RuntimeError):
            return []
        for t in results:
            try:
                ssid = t[0].decode()
            except (UnicodeError, AttributeError, TypeError):
                continue
            if not ssid:
                continue
            rssi = t[3]
            if ssid not in nets or rssi > nets[ssid]["rssi"]:
                nets[ssid] = {"ssid": ssid, "rssi": rssi, "security": t[4]}
        return sorted(nets.values(), key=lambda n: n["rssi"], reverse=True)


# -- Display manager --------------------------------------------------------


class DisplayManager:
    """Owns the HUB75 display state and any running animation task."""

    def __init__(self):
        gc.collect()
        self.layout = _dhw_cfg["display"]
        # decoder buffers were preallocated at import (see _GIF_BUFS);
        # reset() per upload, never reallocated
        w, h = self.layout.split("x")
        self.gifdec = GifDecoder(width=int(w), height=int(h), buffers=_GIF_BUFS)
        self.i75 = _I75            # hardware already up — see import-time init
        self.graphics = _GRAPHICS
        self.png = _PNG
        self.jpeg = _JPEG
        self.width, self.height = self.graphics.get_bounds()
        self.black = self.graphics.create_pen(0, 0, 0)
        self.white = self.graphics.create_pen(255, 255, 255)
        self.mode = "blank"   # blank | boot | image | album | gif | slideshow | text
        self.current = None
        self._player = None
        self.last_spec = {"t": "blank"}

    def _spec(self, spec):
        """Remember what is on screen so it can be restored after a reboot."""
        self.last_spec = spec
        try:
            with open(STATE_FILE, "w") as f:
                f.write(json.dumps(spec))
        except OSError:
            pass

    def decoder_for(self, path):
        return self.png if path.lower().endswith(".png") else self.jpeg

    def _check_size(self, path):
        d = self.decoder_for(path)
        if not d.open_file(path):
            raise ValueError("cannot decode " + path)
        w, h = d.get_width(), d.get_height()
        if (w, h) != (self.width, self.height):
            raise ValueError(
                "{} is {}x{} but the display is {}x{} - re-upload an image "
                "of that size or change the layout in /settings".format(
                    path, w, h, self.width, self.height))

    def _check_dir_size(self, dpath):
        """First frame standing in for a whole album/gif frame set."""
        frames = sorted(f for f in os.listdir(dpath) if _is_image_name(f))
        if frames:
            self._check_size(dpath + "/" + frames[0])

    def _draw(self, path):
        d = self.decoder_for(path)
        self._check_size(path)
        d.decode(0, 0)
        self.i75.update()

    def _wipe(self):
        self.graphics.set_pen(self.black)
        self.graphics.clear()
        self.i75.update()

    async def _stop_player(self):
        if self._player is not None:
            self._player.cancel()
            try:
                await self._player
            except asyncio.CancelledError:
                pass
            self._player = None

    async def show(self, name, interval_ms=None, loops=0):
        """Show a single image, play an album/gif, or draw one album frame."""
        await self._stop_player()
        path = IMG_DIR + "/" + name
        if "/" in name:
            album = name.split("/", 1)[0]
            if _is_gif_name(album):
                path = _gif_dir(album) + "/" + name.split("/", 1)[1]
        gdir = _gif_dir(name)
        if _is_gif_name(name) and _is_dir(gdir):
            self._check_dir_size(gdir)
            gc.collect()          # gif decode left the heap fragmented
            interval, delays = _gif_meta(gdir)
            self.mode, self.current = "gif", name
            self._player = asyncio.create_task(
                self._album_loop(gdir, int(interval_ms or interval),
                                 int(loops or 0), delays))
        elif _is_dir(path):
            self._check_dir_size(path)
            self.mode, self.current = "album", name
            self._player = asyncio.create_task(
                self._album_loop(path, int(interval_ms or DEFAULT_ANIM_MS), int(loops or 0)))
        elif _is_file(path) and _is_image_name(name):
            self.graphics.set_pen(self.black)
            self.graphics.clear()
            self._draw(path)
            self.mode, self.current = "image", name
        else:
            raise KeyError(name)
        self._spec({"t": "show", "name": name,
                    "interval_ms": interval_ms, "loops": loops})

    async def show_random(self):
        files = _list_image_files()
        if not files:
            raise KeyError("no images on board")
        await self.show(random.choice(files))

    async def show_next(self):
        """Advance to the next root-level image (used by the front button)."""
        files = _list_image_files()
        if not files:
            return
        try:
            i = files.index(self.current) + 1
        except ValueError:
            i = 0
        await self.show(files[i % len(files)])

    async def slideshow(self, interval_s, source=None):
        await self._stop_player()
        if source and _is_gif_name(source) and not source.startswith("."):
            source = "." + source      # "x.gif" -> hidden album ".x.gif"
        self.mode, self.current = "slideshow", None
        self._player = asyncio.create_task(
            self._slideshow_loop(max(float(interval_s), 0.5), source))
        self._spec({"t": "slideshow", "interval": float(interval_s), "source": source})

    async def show_text(self, params):
        await self._stop_player()
        text = str(params.get("text", ""))
        if params.get("scroll"):
            self._player = asyncio.create_task(self._scroll_loop(text, params))
        else:
            g = self.graphics
            if params.get("clear", True):
                g.set_pen(self.black)
                g.clear()
            g.set_pen(g.create_pen(int(params.get("r", 255)),
                                   int(params.get("g", 255)),
                                   int(params.get("b", 255))))
            g.set_font("bitmap8")
            g.text(text, int(params.get("x", 2)), int(params.get("y", self.height // 2)),
                   -1, int(params.get("scale", 1)))
            self.i75.update()
        self.mode, self.current = "text", text
        self._spec({"t": "text", "params": dict(params)})

    async def _scroll_loop(self, text, params):
        g = self.graphics
        pen = g.create_pen(int(params.get("r", 255)),
                           int(params.get("g", 255)),
                           int(params.get("b", 255)))
        scale = int(params.get("scale", 2))
        y = int(params.get("y", self.height // 2))
        delay = max(int(params.get("speed_ms", 80)), 10) / 1000
        step = max(int(params.get("step", 2)), 1)
        g.set_font("bitmap8")
        try:
            w = _text_w(g, text, scale)
        except (AttributeError, TypeError):
            w = len(text) * 8 * scale
        while True:
            x = self.width
            while x > -w:
                g.set_pen(self.black)
                g.clear()
                g.set_pen(pen)
                g.text(text, x, y, -1, scale)
                self.i75.update()
                x -= step
                await asyncio.sleep(delay)

    async def demo(self):
        """Built-in first-boot demo: procedurally drawn screens, so it works
        at any configured canvas size. Retired by any real content."""
        await self._stop_player()
        self.mode, self.current = "demo", "demo"
        self._spec({"t": "demo"})
        self._player = asyncio.create_task(self._demo_loop())

    async def _demo_loop(self):
        g = self.graphics
        W, H = self.width, self.height
        cx = W // 2
        amber = g.create_pen(255, 210, 90)
        green = g.create_pen(90, 220, 140)
        g.set_font("bitmap8")

        def card(rows):
            g.set_pen(self.black)
            g.clear()
            th = sum(8 * s + 4 for _, s, _ in rows) - 4
            y = (H - th) // 2
            for t, s, pen in rows:
                g.set_pen(pen)
                g.text(t, int(cx - _text_w(g, t, s) / 2), y, -1, s)
                y += 8 * s + 4
            self.i75.update()

        while True:
            self.status_message("", "", "")                 # the signpost logo
            await asyncio.sleep(6)
            card([("WELCOME", 2, self.white), ("to signPOST", 1, amber)])
            await asyncio.sleep(6)
            card([("your sign is", 1, self.white), ("online", 1, green),
                  ("signpost.local", 1, amber)])
            await asyncio.sleep(6)
            card([("REST API:", 1, green), ("PUT images", 1, self.white),
                  ("POST /api/show", 1, self.white)])
            await asyncio.sleep(6)
            card([("upload your own", 1, self.white), ("images to begin", 1, self.white)])
            await asyncio.sleep(6)

    async def clear(self):
        """Stop any playback and blank the matrix."""
        await self._stop_player()
        self._wipe()
        self.mode, self.current = "blank", None
        self._spec({"t": "blank"})

    def status_message(self, line1, line2="", line3=""):
        """Boot/status screen: the signPOST arrow logo on a post, with each
        status line drawn as a small sign hanging off it. Layout scales
        with the configured canvas size."""
        g = self.graphics
        brown = g.create_pen(150, 105, 60)
        green = g.create_pen(90, 220, 140)
        amber = g.create_pen(255, 210, 90)
        board = g.create_pen(38, 50, 66)
        g.set_font("bitmap8")
        g.set_pen(self.black)
        g.clear()
        W, H = self.width, self.height
        cx = W // 2
        # ground shadow behind the pole base, then pole + finial
        g.set_pen(g.create_pen(30, 40, 52))
        g.rectangle(cx - max(8, W // 8), H - 5, max(16, W // 4), 3)
        g.set_pen(brown)
        g.rectangle(cx - 2, 6, 5, H - 8)
        g.circle(cx, 8, 4)

        def sign(y, parts, h):
            tip, pad = 9, 4
            tw = int(sum(_text_w(g, t, s) for t, _, s in parts))
            # signs are centered on the post, tip right - shrink padding
            # then the tip until the centered sign actually fits
            while (pad > 1 or tip > 3) and \
                    cx + (tw + 2 * pad + 1) // 2 + tip > W - 1:
                if pad > 1:
                    pad -= 1
                else:
                    tip -= 1
            fw = tw + 2 * pad
            if cx + (fw + 1) // 2 + tip > W - 1:
                # still too wide: fill edge to edge, drop the arrow tip
                x0, xf, tip = 1, W - 1, 0
            else:
                x0 = cx - fw // 2
                xf = x0 + fw
            fw = xf - x0
            txp = xf + tip
            g.set_pen(board)
            g.rectangle(x0, y, fw, h)
            if tip:
                g.triangle(xf - 1, y, xf - 1, y + h - 1, txp, y + h // 2)
            g.set_pen(green)
            g.line(x0, y, xf, y)
            g.line(x0, y + h - 1, xf, y + h - 1)
            g.line(x0, y, x0, y + h)
            if tip:
                g.line(xf, y, txp, y + h // 2)
                g.line(xf, y + h - 1, txp, y + h // 2)
            else:
                g.line(xf, y, xf, y + h)
            tx = x0 + pad
            for t, pen, s in parts:
                g.set_pen(pen)
                g.text(t, int(tx), y + (h - 8 * s) // 2, -1, s)
                tx += _text_w(g, t, s)

        # logo board across the top of the pole
        yl = max(2, int(H * 0.12))
        lw = _text_w(g, "sign", 1) + _text_w(g, "POST", 1)
        if lw + 21 <= W - 2:
            sign(yl, [("sign", amber, 1), ("POST", green, 1)], 18)
            yl += 22
        else:
            # narrow panel: stacked two-line board
            bw = max(_text_w(g, "sign", 1), _text_w(g, "POST", 1)) + 10
            bh = 24
            bx = cx - bw // 2
            g.set_pen(board)
            g.rectangle(bx, yl, bw, bh)
            g.set_pen(green)
            g.line(bx, yl, bx + bw - 1, yl)
            g.line(bx, yl + bh - 1, bx + bw - 1, yl + bh - 1)
            g.line(bx, yl, bx, yl + bh - 1)
            g.line(bx + bw - 1, yl, bx + bw - 1, yl + bh - 1)
            g.set_pen(amber)
            g.text("sign", int(cx - _text_w(g, "sign", 1) / 2), yl + 3, -1, 1)
            g.set_pen(green)
            g.text("POST", int(cx - _text_w(g, "POST", 1) / 2), yl + 13, -1, 1)
            yl += 28

        # status lines: signs spaced evenly between logo and the ground
        lines = []
        if line1:
            s = 2 if H >= 96 and _text_w(g, line1, 2) <= W - 24 else 1
            lines.append((line1, s))
        if line2:
            lines.append((line2, 1))
        if line3:
            lines.append((line3, 1))
        n = len(lines)
        for i, (t, s) in enumerate(lines):
            h = 8 * s + 6
            span = max(0, H - 6 - yl - h)
            sign(yl + (span * i // max(n - 1, 1)) if n > 1 else yl + span // 2,
                 [(t, self.white, s)], h)
        self.i75.update()

    async def _album_loop(self, path, interval_ms, loops, delays=None):
        frames = sorted(f for f in os.listdir(path) if _is_image_name(f))
        if not frames:
            return
        self.graphics.set_pen(self.black)
        self.graphics.clear()
        n = len(frames)
        rounds = 0
        while loops == 0 or rounds < loops:
            # deadline scheduling: each frame is due at its cumulative delay.
            # Drawing a frame takes ~65ms, so when it overruns we skip the
            # frames whose slot already passed — content keeps source pacing
            # (slightly choppier) instead of running slow.
            base = time.ticks_ms()
            cum = 0
            i = 0
            while i < n:
                try:
                    self._draw(path + "/" + frames[i])
                except Exception:       # noqa: BLE001 - a dead player kills the event loop
                    gc.collect()
                cum += delays[i] if delays and i < n else interval_ms
                i += 1
                now = time.ticks_diff(time.ticks_ms(), base)
                while i < n:
                    ms = delays[i] if delays and i < n else interval_ms
                    if cum + ms > now:
                        break
                    cum += ms
                    i += 1
                wait = cum - now
                # always yield: a single-frame album hits i>=n immediately
                # and would otherwise spin in _draw, starving the event loop
                await asyncio.sleep(max(wait, 1) / 1000)
                if i >= n:
                    break
            rounds += 1

    async def _slideshow_loop(self, interval_s, source):
        while True:
            if source:
                try:
                    images = [source + "/" + f for f in
                              sorted(os.listdir(IMG_DIR + "/" + source))
                              if _is_image_name(f) or _is_gif_frame(f)]
                except OSError:
                    images = []
            else:
                images = _list_image_files()
            if not images:
                self.status_message("No images", "upload via the API")
                await asyncio.sleep(2)
                continue
            for name in images:
                self.graphics.set_pen(self.black)
                self.graphics.clear()
                try:
                    self._draw(IMG_DIR + "/" + name)
                    self.current = name
                except Exception:       # noqa: BLE001 - a dead player kills the event loop
                    gc.collect()
                await asyncio.sleep(interval_s)


# -- HTTP server ------------------------------------------------------------


class RestServer:
    def __init__(self, dm, net):
        self.dm = dm
        self.net = net
        self.portal_mode = False
        self._consumed = {}   # id(reader) -> request-body bytes read

    async def handle(self, reader, writer):
        try:
            while True:
                request = await asyncio.wait_for(
                    self._read_request(reader), KEEPALIVE_IDLE_S)
                if request is None:
                    break
                if not await asyncio.wait_for(
                        self._serve(request, reader, writer),
                        REQUEST_TIMEOUT_S):
                    break
        except asyncio.TimeoutError:
            pass
        except Exception as e:  # noqa: BLE001 - never let one request kill the server
            _log_exc(e)
            try:
                await self._send(writer, 500, _json({"ok": False, "error": "internal: {}".format(e)}), False)
            except Exception:                        # noqa: BLE001
                pass
        finally:
            # If this task died while registered in the asyncio io_queue
            # (exception mid-read/write), its poll registration leaks and
            # the reused fd later corrupts the queue - drop it explicitly.
            try:
                _core._io_queue.remove(_core.cur_task)
            except Exception:                        # noqa: BLE001
                pass
            self._consumed.pop(id(reader), None)
            try:
                await asyncio.sleep(0.1)   # let lwIP flush before FIN
                writer.close()
                await writer.wait_closed()
            except (OSError, AttributeError):
                pass

    async def _serve(self, request, reader, writer):
        method, target, headers, version = request
        if headers.get("expect", "").lower() == "100-continue":
            writer.write(b"HTTP/1.1 100 Continue\r\n\r\n")
            await writer.drain()
        try:
            cl = int(headers.get("content-length", "0"))
        except ValueError:
            cl = 0
        status, ctype, body = await self._route(reader, method, target, headers)
        conn = headers.get("connection", "").lower()
        keep = ((version == "HTTP/1.1" and conn != "close")
                or conn == "keep-alive")
        # an unread request body would poison the next request on this
        # socket - drop keep-alive so the connection gets closed instead
        if cl > self._consumed.get(id(reader), 0):
            keep = False
        if isinstance(body, tuple):
            await self._send_file(writer, status, body[1], ctype, keep)
        else:
            await self._send(writer, status, body, ctype, keep)
        return keep

    async def _read_request(self, reader):
        line = await reader.readline()
        while line in (b"\r\n", b"\n"):
            line = await reader.readline()
        if not line:
            return None
        parts = line.decode().split()
        if len(parts) < 2:
            raise HttpError(400, "malformed request line")
        version = parts[2] if len(parts) > 2 else "HTTP/0.9"
        headers = {}
        while True:
            hl = await reader.readline()
            if hl in (b"\r\n", b"\n", b""):
                break
            k, _, v = hl.decode().partition(":")
            if k:
                headers[k.strip().lower()] = v.strip()
        return parts[0].upper(), parts[1], headers, version

    async def _read_body(self, reader, headers, limit):
        if "chunked" in headers.get("transfer-encoding", "").lower():
            raise HttpError(411, "chunked bodies not supported, send Content-Length")
        try:
            length = int(headers.get("content-length", "0"))
        except ValueError:
            length = 0
        if length <= 0:
            return b""
        if length > limit:
            raise HttpError(413, "body too large ({} bytes max)".format(limit))
        buf = bytearray()
        while len(buf) < length:
            chunk = await reader.read(min(2048, length - len(buf)))
            if not chunk:
                break
            buf += chunk
        if len(buf) < length:
            raise HttpError(400, "incomplete body")
        self._consumed[id(reader)] = self._consumed.get(id(reader), 0) + len(buf)
        return bytes(buf)

    async def _params(self, reader, query, headers):
        params = dict(query)
        body = await self._read_body(reader, headers, 64 * 1024)
        if body:
            try:
                data = json.loads(body.decode())
            except ValueError:
                data = {"image": body.decode().strip()}
            if isinstance(data, dict):
                params.update(data)
        return params

    # -- routing --

    async def _route(self, reader, method, target, headers):
        path, _, qs = target.partition("?")
        query = {}
        for pair in qs.split("&"):
            if "=" in pair:
                k, _, v = pair.partition("=")
                query[_unquote(k)] = _unquote(v)
        segs = [_unquote(s) for s in path.split("/") if s]

        if method == "OPTIONS":
            return 204, "text/plain", b""

        if segs and segs[0] == "api":
            try:
                return await self._api(reader, method, segs[1:], query, headers)
            except HttpError as e:
                return e.status, "application/json", _json({"ok": False, "error": e.message})
            except KeyError as e:
                return 404, "application/json", _json({"ok": False, "error": "not found: {}".format(e)})
            except ValueError as e:
                return 400, "application/json", _json({"ok": False, "error": str(e)})
            except OSError as e:
                return 500, "application/json", _json({"ok": False, "error": "fs error: {}".format(e)})

        if method != "GET":
            return 404, "application/json", _json({"ok": False, "error": "not found"})

        # GET fallbacks
        if self.portal_mode or segs == ["setup"]:
            # captive portal: every page (including OS connectivity checks)
            # returns the wifi setup page while in setup mode
            return 200, "text/html", _PORTAL_HTML
        if segs == ["settings"]:
            return 200, "text/html", _SETTINGS_HTML
        if not segs:
            return 200, "text/html", _INDEX_HTML
        return 404, "application/json", _json({"ok": False, "error": "try /api"})

    async def _api(self, reader, method, segs, query, headers):
        if not segs:
            return 200, "application/json", _json({"ok": True, "service": "i75-rest-display"})

        cmd = segs[0]

        if cmd == "status":
            return 200, "application/json", _json(self._status())

        if cmd == "images":
            return await self._images(reader, method, segs[1:], query, headers)

        if cmd == "show":
            if method not in ("POST", "PUT"):
                raise HttpError(405, "use POST")
            params = await self._params(reader, query, headers)
            name = "/".join(segs[1:]) if len(segs) > 1 else params.get("image", params.get("album"))
            if not name:
                raise HttpError(400, "missing image name")
            await self.dm.show(name, params.get("interval_ms"), params.get("loops"))
            return 200, "application/json", _json({"ok": True, "showing": name, "mode": self.dm.mode})

        if cmd == "clear" or cmd == "stop":
            if method not in ("POST", "DELETE"):
                raise HttpError(405, "use POST")
            await self.dm.clear()
            return 200, "application/json", _json({"ok": True, "mode": "blank"})

        if cmd == "random":
            if method != "POST":
                raise HttpError(405, "use POST")
            await self.dm.show_random()
            return 200, "application/json", _json({"ok": True, "showing": self.dm.current})

        if cmd == "slideshow":
            if method != "POST":
                raise HttpError(405, "use POST")
            params = await self._params(reader, query, headers)
            source = params.get("album", params.get("folder"))
            if source is not None:
                source = str(source)
                check = ("." + source) if _is_gif_name(source) else source
                if not _valid_name(source) or not _is_dir(IMG_DIR + "/" + check):
                    raise HttpError(404, "no such album")
            await self.dm.slideshow(params.get("interval", DEFAULT_SLIDESHOW_S), source)
            return 200, "application/json", _json({"ok": True, "mode": "slideshow"})

        if cmd == "text":
            if method != "POST":
                raise HttpError(405, "use POST")
            params = await self._params(reader, query, headers)
            await self.dm.show_text(params)
            return 200, "application/json", _json({"ok": True, "mode": "text"})

        if cmd == "led":
            if method != "POST":
                raise HttpError(405, "use POST")
            params = await self._params(reader, query, headers)
            self.dm.i75.set_led(int(params.get("r", 0)), int(params.get("g", 0)), int(params.get("b", 0)))
            return 200, "application/json", _json({"ok": True})

        if cmd == "scan":
            if method != "GET":
                raise HttpError(405, "use GET")
            return 200, "application/json", _json({"ok": True, "networks": self.net.scan()})

        if cmd == "connect":
            if method != "POST":
                raise HttpError(405, "use POST")
            params = await self._params(reader, query, headers)
            ssid = str(params.get("ssid", "")).strip()
            if not ssid:
                raise HttpError(400, "missing ssid")
            _save_wifi_creds(ssid, str(params.get("password", "")))
            asyncio.create_task(_reboot_later())
            return 200, "application/json", _json({"ok": True, "message": "credentials saved, rebooting"})

        if cmd == "reboot":
            if method != "POST":
                raise HttpError(405, "use POST")
            asyncio.create_task(_reboot_later())
            return 200, "application/json", _json({"ok": True, "message": "rebooting"})

        if cmd == "factory-reset":
            if method != "POST":
                raise HttpError(405, "use POST")
            _factory_reset()
            asyncio.create_task(_reboot_later())
            return 200, "application/json", _json({"ok": True, "message": "wifi credentials wiped, rebooting into setup mode"})

        if cmd == "move":
            if method != "POST":
                raise HttpError(405, "use POST")
            params = await self._params(reader, query, headers)
            return await self._move(str(params.get("from", "")),
                                    str(params.get("to", "")))

        if cmd == "schedule":
            if method == "GET":
                return 200, "application/json", _json({"ok": True, "schedule": _load_schedule()})
            if method != "POST":
                raise HttpError(405, "use POST")
            params = await self._params(reader, query, headers)
            cfg = _load_schedule()
            for k in ("enabled", "on", "off", "mode", "show", "off_show",
                      "tz", "interval"):
                if k in params:
                    cfg[k] = params[k]
            _save_schedule(cfg)
            return 200, "application/json", _json({"ok": True, "schedule": cfg})

        if cmd == "display":
            if method == "GET":
                cfg = _load_display_config()
                cfg.update({"ok": True, "active": self.dm.layout,
                            "width": self.dm.width, "height": self.dm.height,
                            "layouts": sorted(DISPLAY_MODES)})
                return 200, "application/json", _json(cfg)
            if method != "POST":
                raise HttpError(405, "use GET or POST")
            params = await self._params(reader, query, headers)
            cfg = _load_display_config()
            for k in ("display", "panel", "color_order", "stb_invert"):
                if k in params:
                    cfg[k] = params[k]
            # physical description -> resolved display constant
            if all(k in params for k in ("panel_w", "panel_h", "cols", "rows")):
                try:
                    pw, ph = int(params["panel_w"]), int(params["panel_h"])
                    cols, rows = int(params["cols"]), int(params["rows"])
                except (TypeError, ValueError):
                    raise HttpError(400, "panel/grid values must be integers")
                cfg["display"] = "{}x{}".format(pw * cols, ph * rows)
                cfg["phys"] = {"panel_w": pw, "panel_h": ph,
                               "cols": cols, "rows": rows}
            if cfg["display"] not in DISPLAY_MODES:
                raise HttpError(400, "unsupported layout {} - canvas must be one of {}".format(
                    cfg["display"], ", ".join(sorted(DISPLAY_MODES))))
            if cfg["panel"] not in PANEL_TYPES:
                raise HttpError(400, "panel must be generic|fm6126a")
            if cfg["color_order"] not in COLOR_ORDERS:
                raise HttpError(400, "color_order must be rgb|rbg|grb|gbr|brg|bgr")
            cfg["stb_invert"] = cfg["stb_invert"] in (True, "true", "1", "on")
            _save_display_config(cfg)
            return 200, "application/json", _json(
                {"ok": True, "display": cfg["display"], "reboot_required": True})

        raise HttpError(404, "unknown endpoint")

    async def _images(self, reader, method, segs, query, headers):
        if not segs:
            if method != "GET":
                raise HttpError(405, "use GET")
            return 200, "application/json", _json({"ok": True, "images": _list_images()})

        name = segs[0]
        if not _valid_name(name):
            raise HttpError(400, "invalid name")
        path = IMG_DIR + "/" + name

        if len(segs) == 1:
            gdir = _gif_dir(name)
            if method == "GET":
                if _is_dir(gdir):
                    frames = sorted(f for f in os.listdir(gdir) if _is_gif_frame(f))
                    if not frames:
                        raise HttpError(404, "not found: " + name)
                    return 200, "application/json", _json(
                        {"ok": True, "name": name, "type": "gif", "frames": frames})
                if _is_dir(path):
                    frames = sorted(f for f in os.listdir(path) if _is_image_name(f))
                    return 200, "application/json", _json({"ok": True, "name": name, "type": "album", "frames": frames})
                if not _is_file(path):
                    raise HttpError(404, "not found: " + name)
                return 200, _ctype_for(name), ("file", path)
            if method in ("PUT", "POST"):
                result = await self._upload(reader, headers, path, name)
                if result[0] in (200, 201) and str(query.get("show", "")) in ("1", "true", "yes"):
                    await self.dm.show(name)
                return result
            if method == "DELETE":
                deleted = False
                if _is_dir(gdir):
                    deleted = _delete_entry(gdir)
                elif _delete_entry(path):
                    deleted = True
                if not deleted:
                    raise HttpError(404, "not found: " + name)
                if self.dm.current == name:
                    await self.dm.clear()
                return 200, "application/json", _json({"ok": True, "deleted": name})

        if len(segs) == 2:
            frame = segs[1]
            if not _valid_name(frame):
                raise HttpError(400, "invalid frame name")
            fpath = (_gif_dir(name) if _is_gif_name(name) else path) + "/" + frame
            if method in ("PUT", "POST"):
                _ensure_dir(path)
                return await self._upload(reader, headers, fpath, frame)
            if method == "GET":
                if not _is_file(fpath):
                    raise HttpError(404, "not found: " + frame)
                return 200, _ctype_for(frame), ("file", fpath)
            if method == "DELETE":
                if not _is_file(fpath):
                    raise HttpError(404, "not found: " + frame)
                os.remove(fpath)
                return 200, "application/json", _json({"ok": True, "deleted": frame})

        raise HttpError(404, "unknown endpoint")

    async def _move(self, src, dst):
        """Move an image between root and albums. Destination '' = root, an
        existing album, or a new album name (created on the fly)."""
        segs = src.split("/")
        if len(segs) > 2 or not all(_valid_name(s) for s in segs):
            raise HttpError(400, "bad source name")
        if not _is_image_name(segs[-1]):
            raise HttpError(400, "can only move image files")
        if len(segs) == 2 and _is_gif_name(segs[0]):
            raise HttpError(400, "gif frames are managed - delete and re-upload")
        spath = IMG_DIR + "/" + src
        if not _is_file(spath):
            raise HttpError(404, "not found: " + src)
        fname = segs[-1]
        if not dst:
            ddir = IMG_DIR
        else:
            if not _valid_name(dst) or _is_gif_name(dst):
                raise HttpError(400, "bad destination name")
            ddir = IMG_DIR + "/" + dst
            if not _is_dir(ddir):
                _ensure_dir(ddir)          # creates the album on the fly
        dpath = ddir + "/" + fname
        newname = dpath[len(IMG_DIR) + 1:]
        if dpath == spath:
            return 200, "application/json", _json({"ok": True, "moved": src, "to": newname})
        if _is_file(dpath) or _is_dir(dpath):
            raise HttpError(400, fname + " already exists at destination")
        os.rename(spath, dpath)
        if len(segs) == 2:                     # clean up a now-empty album
            apath = IMG_DIR + "/" + segs[0]
            if _is_dir(apath) and not os.listdir(apath):
                _delete_entry(apath)
        cur = self.dm.current
        if cur and (cur == src or (len(segs) == 2 and cur == segs[0])):
            await self.dm.clear()
        return 200, "application/json", _json({"ok": True, "moved": src, "to": newname})

    async def _upload(self, reader, headers, path, name):
        is_gif = _is_gif_name(name)
        if not is_gif and not _is_image_name(name):
            raise HttpError(415, "unsupported type - use .png/.jpg/.jpeg/.gif")
        try:
            length = int(headers.get("content-length", "0"))
        except ValueError:
            length = 0
        if length <= 0:
            raise HttpError(411, "Content-Length required")
        cap = MAX_GIF_BYTES if is_gif else MAX_UPLOAD_BYTES
        if length > cap:
            raise HttpError(413, "file too large ({} bytes max)".format(cap))
        if is_gif:
            return await self._upload_gif(reader, length, name)
        if length > _free_space() - 32768:
            raise HttpError(507, "not enough free space")

        remaining = length
        f = open(path, "wb")
        while remaining:
            chunk = await reader.read(min(2048, remaining))
            if not chunk:
                break
            f.write(chunk)
            remaining -= len(chunk)
        f.close()

        self._consumed[id(reader)] = (self._consumed.get(id(reader), 0)
                                      + length - remaining)
        if remaining:
            _remove(path)
            raise HttpError(400, "incomplete body")

        try:
            d = self.dm.decoder_for(path)
            ok = d.open_file(path)
            wh = (d.get_width(), d.get_height()) if ok else None
        except (OSError, ValueError, RuntimeError):
            ok, wh = False, None
        if not ok:
            _remove(path)
            raise HttpError(400, "not a valid PNG/JPEG file")
        if wh != (self.dm.width, self.dm.height):
            _remove(path)
            raise HttpError(400, "image must be {}x{} (got {}x{})".format(
                self.dm.width, self.dm.height, wh[0], wh[1]))

        return 201, "application/json", _json({"ok": True, "name": name, "size": length})

    async def _upload_gif(self, reader, length, name):
        """Stream-decode an uploaded GIF into its hidden album of PNG frames."""
        gdir = _gif_dir(name)
        _ensure_dir(IMG_DIR)
        if _is_dir(gdir):
            _delete_entry(gdir)
        _ensure_dir(gdir)
        dec = self.dm.gifdec
        dec.reset(gdir)
        try:
            remaining = length
            while remaining:
                chunk = await reader.read(min(4096, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                dec.feed(chunk)
            dec.feed(b"")
            self._consumed[id(reader)] = (self._consumed.get(id(reader), 0)
                                          + length - remaining)
        except GifError as e:
            _delete_entry(gdir)
            raise HttpError(400, "bad gif: {}".format(e))
        except OSError:
            _delete_entry(gdir)
            raise HttpError(507, "no space for decoded frames")
        except Exception:
            _delete_entry(gdir)
            raise
        written = dec.frames
        if not written:
            _delete_entry(gdir)
            raise HttpError(400, "gif produced no frames")

        delays = dec.delays
        dec = None
        gc.collect()             # release the decoder's ~80KB of buffers
        meta = {"interval": delays[0] if delays else DEFAULT_ANIM_MS,
                "frames": written}
        if any(d != delays[0] for d in delays):
            meta["delays"] = delays
        f = open(gdir + "/meta.json", "w")
        f.write(json.dumps(meta))
        f.close()
        return 201, "application/json", _json(
            {"ok": True, "name": name, "type": "gif", "frames": written})

    def _status(self):
        connected = self.net.wifi.isconnected()
        ip = self.net.wifi.ipv4() if connected else None
        st = {
            "ok": True,
            "mode": self.dm.mode,
            "current": self.dm.current,
            "wifi_connected": connected,
            "ip": ip,
            "portal_mode": self.portal_mode,
            "ap_active": bool(self.net.ap.active()),
            "display": {"layout": self.dm.layout,
                        "width": self.dm.width, "height": self.dm.height},
            "images": len(_list_images()),
            "free_bytes": _free_space(),
            "buttons": [self.dm.i75.switch_pressed(i) for i in range(Interstate75.NUM_SWITCHES)],
        }
        if connected:
            st["mdns"] = MDNS_NAME + ".local"
        t = time.localtime()
        if t[0] >= 2025:
            st["utc"] = "{:02d}:{:02d}".format(t[3], t[4])
            st["local"] = "{:02d}:{:02d}".format(
                *divmod(_local_minutes(_load_schedule().get("tz", 0)), 60))
        return st

    # -- responses --

    async def _send(self, writer, status, body=b"", ctype="application/json",
                    keep=False):
        if isinstance(body, str):
            body = body.encode()
        writer.write((
            "HTTP/1.1 {} {}\r\n"
            "Content-Type: {}\r\n"
            "Content-Length: {}\r\n"
            "Access-Control-Allow-Origin: *\r\n"
            "Access-Control-Allow-Methods: GET, PUT, POST, DELETE, OPTIONS\r\n"
            "Access-Control-Allow-Headers: Content-Type\r\n"
            "Connection: {}\r\n\r\n"
        ).format(status, _REASONS.get(status, "OK"), ctype, len(body),
                 "keep-alive" if keep else "close").encode())
        if body:
            writer.write(body)
        await writer.drain()

    async def _send_file(self, writer, status, path, ctype, keep=False):
        size = _stat(path)[6]
        writer.write((
            "HTTP/1.1 {} {}\r\n"
            "Content-Type: {}\r\n"
            "Content-Length: {}\r\n"
            "Access-Control-Allow-Origin: *\r\n"
            "Connection: {}\r\n\r\n"
        ).format(status, _REASONS.get(status, "OK"), ctype, size,
                 "keep-alive" if keep else "close").encode())
        f = open(path, "rb")
        while True:
            chunk = f.read(2048)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
        f.close()


# -- Built-in web UI ----------------------------------------------------------

_INDEX_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>signPOST</title>
<style>
body{font-family:system-ui,sans-serif;background:#181820;color:#eee;max-width:680px;margin:1em auto;padding:0 1em}
button{background:#3a3a55;color:#eee;border:1px solid #555;border-radius:6px;padding:.4em .8em;margin:.15em;cursor:pointer}
select,input{background:#22222e;color:#eee;border:1px solid #555;border-radius:6px;padding:.35em;margin:.15em}
button:hover{background:#4a4a70}
.card{background:#22222e;border-radius:8px;padding:.6em .8em;margin:.6em 0}
.item{display:flex;align-items:center;gap:.7em;background:#2a2a38;border-radius:6px;padding:.4em .6em;margin:.35em 0}
.sub{margin-left:3em}
img{width:48px;height:48px;image-rendering:pixelated;background:#000}
img.sm{width:28px;height:28px}
.nm{flex:1;word-break:break-all}
small{opacity:.6}
a{color:#9cf}
#st{opacity:.8;font-size:.9em;margin:.4em 0 .6em}
h1{margin:.2em 0 .5em}
.flag{display:inline-block;position:relative;padding:.16em .85em .16em .55em;
  background:#5adc8c;font-family:ui-monospace,Menlo,monospace;font-weight:700;
  letter-spacing:.05em;
  clip-path:polygon(0 0,calc(100% - .75em) 0,100% 50%,calc(100% - .75em) 100%,0 100%)}
.flag::before{content:"";position:absolute;inset:2px;background:#263242;
  clip-path:polygon(0 0,calc(100% - .75em) 0,100% 50%,calc(100% - .75em) 100%,0 100%)}
.flag i{font-style:normal;color:#ffd75a;position:relative}
.flag b{color:#5adc8c;position:relative}
.num{width:3.8em}
</style></head><body>
<h1><span class="flag"><i>sign</i><b>POST</b></span></h1>
<div id="st">loading...</div>
<div class="card"><b>Display</b><br>
<button onclick="cmd('random')">Random</button>
<button onclick="cmd('clear')">Clear</button>
slideshow:
<select id="ss"><option value="">all images</option></select>
every <input id="ssint" class="num" type="number" value="8" min="0.5" step="0.5">s
<button onclick="slideshow()">Start</button>
</div>
<div class="card"><b>Upload</b><br>
<input type="file" id="f" accept=".png,.jpg,.jpeg,.gif">
to <select id="updst"><option value="">(root)</option></select>
<button onclick="upload()">Upload</button>
</div>
<div class="card"><b>Library</b> <small id="li"></small>
<div id="list"></div>
</div>
<div class="card" id="sched"><b>Schedule</b>
<label><input type="checkbox" id="schen"> enabled</label><br>
on <input id="schon" value="18:00" class="num"> &ndash; off <input id="schoff" value="23:00" class="num"><br>
mode <select id="schmode"><option value="show">show</option><option value="slideshow">slideshow</option></select>
<select id="schshow"><option value="">all images (root)</option></select>
every <input id="schint" class="num" type="number" value="8" min="0.5" step="0.5">s<br>
when off, show <select id="schoffshow"></select><br>
<button onclick="setschedule()">Save</button> <small id="tm"></small>
</div>
<p><small><a href="/settings">settings</a> &middot; <a href="/setup">wifi setup</a> &middot;
<a href="#" onclick="reboot();return false">reboot</a></small></p>
<script>
const E=encodeURIComponent,EP=p=>p.split('/').map(encodeURIComponent).join('/'),
      expanded=new Set(),frames={};
function btn(t,fn){const b=document.createElement('button');b.textContent=t;b.onclick=fn;return b}
async function refresh(){
  const s=await (await fetch('/api/status')).json();
  document.getElementById('st').textContent='mode: '+s.mode+(s.current?' / '+s.current:'')+' — '+s.ip+' — '+Math.round(s.free_bytes/1024)+'KB free'+(s.local?' — board '+s.local:'');
  const sc=await (await fetch('/api/schedule')).json();
  const l=await (await fetch('/api/images')).json();
  const el=document.getElementById('list');el.innerHTML='';
  document.getElementById('li').textContent=l.images.length+' item(s)';
  const albums=l.images.filter(i=>i.type==='album').map(i=>i.name);
  for(const i of l.images){
    const d=document.createElement('div');d.className='item';
    d.innerHTML=(i.type==='image'?'<img src="/api/images/'+EP(i.name)+'">':'<img>')
      +'<span class="nm">'+i.name+' <small>'+i.type
      +(i.size?' '+(i.size/1024).toFixed(1)+'KB':'')+(i.frames?' '+i.frames+'f':'')+'</small></span>';
    d.append(btn('Show',()=>cmd('show/'+EP(i.name))));
    if(i.type==='image')d.append(moveSel(i.name,albums,true));
    if(i.type==='album')d.append(expBtn(i.name));
    d.append(btn('Del',async()=>{await fetch('/api/images/'+EP(i.name),{method:'DELETE'});refresh()}));
    el.append(d);
    if(i.type==='album'&&expanded.has(i.name))el.append(await frameRows(i.name,albums));
  }
  const sel=document.getElementById('ss'),tgt=document.getElementById('schshow'),
        off=document.getElementById('schoffshow'),up=document.getElementById('updst');
  const cur=[sel.value,tgt.value,off.value,up.value];
  sel.innerHTML=tgt.innerHTML='<option value="">all images (root)</option>';
  off.innerHTML='';up.innerHTML='<option value="">(root)</option>';
  for(const i of l.images){
    if(i.type==='album'||i.type==='gif'){
      for(const s of [sel,tgt]){const o=document.createElement('option');o.value=i.name;o.textContent=i.type+': '+i.name;s.append(o)}
      if(i.type==='album'){const o=document.createElement('option');o.value=i.name;o.textContent='album: '+i.name;up.append(o)}
    }else{const o=document.createElement('option');o.value=i.name;o.textContent=i.name;off.append(o)}
  }
  {const o=document.createElement('option');o.value='__new__';o.textContent='+ new album';up.append(o)}
  sel.value=cur[0];tgt.value=cur[1];off.value=cur[2];up.value=cur[3];
  const sp=document.getElementById('sched');
  if(!sp.contains(document.activeElement)){
    const c=sc.schedule;
    document.getElementById('schen').checked=!!c.enabled;
    document.getElementById('schon').value=c.on;
    document.getElementById('schoff').value=c.off;
    document.getElementById('schmode').value=c.mode||'show';
    document.getElementById('schint').value=c.interval;
    document.getElementById('tm').textContent=s.local?('board time '+s.local):'';
    tgt.value=c.show||'';off.value=c.off_show||'';
  }
}
function moveSel(from,albums,skipRoot){
  const s=document.createElement('select');
  s.innerHTML='<option value="">move…</option>';
  if(!skipRoot){const o=document.createElement('option');o.value='';o.textContent='(root)';s.append(o)}
  for(const a of albums){const o=document.createElement('option');o.value=a;o.textContent=a;s.append(o)}
  const n=document.createElement('option');n.value='__new__';n.textContent='+ new album';s.append(n);
  s.onchange=()=>mv(from,s);return s;
}
function expBtn(name){
  const b=btn(expanded.has(name)?'▾ frames':'▸ frames',async()=>{
    if(expanded.has(name))expanded.delete(name);else expanded.add(name);refresh()});return b}
async function frameRows(name,albums){
  if(!frames[name])frames[name]=(await (await fetch('/api/images/'+E(name))).json()).frames||[];
  const w=document.createElement('div');
  for(const f of frames[name]){
    const d=document.createElement('div');d.className='item sub';
    const path=name+'/'+f;
    d.innerHTML='<img class="sm" src="/api/images/'+EP(path)+'"><span class="nm">'+f+'</span>';
    d.append(btn('Show',()=>cmd('show/'+EP(path))));
    d.append(moveSel(path,albums.filter(a=>a!==name),false));
    d.append(btn('Del',async()=>{await fetch('/api/images/'+EP(path),{method:'DELETE'});delete frames[name];refresh()}));
    w.append(d);
  }
  return w;
}
async function mv(from,sel){
  let to=sel.value;
  if(to==='__new__'){to=prompt('new album name:');if(!to){refresh();return}}
  const r=await fetch('/api/move',{method:'POST',body:JSON.stringify({from:from,to:to})});
  const j=await r.json();if(!j.ok)alert(j.error||'move failed');
  for(const k in frames)delete frames[k];
  refresh();
}
async function newAlbumIfNeeded(v){return v==='__new__'?(prompt('new album name:')||''):v}
async function cmd(p){await fetch('/api/'+p,{method:'POST'});setTimeout(refresh,300)}
async function slideshow(){const i=document.getElementById('ssint').value||'5';
  const a=await newAlbumIfNeeded(document.getElementById('ss').value),b={interval:+i};if(a)b.album=a;
  await fetch('/api/slideshow',{method:'POST',body:JSON.stringify(b)});refresh()}
async function setschedule(){
  const b={enabled:document.getElementById('schen').checked,on:document.getElementById('schon').value,
    off:document.getElementById('schoff').value,mode:document.getElementById('schmode').value,
    show:document.getElementById('schshow').value,off_show:document.getElementById('schoffshow').value,
    interval:+document.getElementById('schint').value};
  await fetch('/api/schedule',{method:'POST',body:JSON.stringify(b)});refresh()}
async function reboot(){if(!confirm('reboot the board?'))return;
  await fetch('/api/reboot',{method:'POST'});alert('rebooting — back in ~15s')}
async function upload(){
  const f=document.getElementById('f').files[0];if(!f)return alert('pick a file first');
  let dst=await newAlbumIfNeeded(document.getElementById('updst').value);
  if(dst===null||dst===undefined)dst='';
  const name=(dst?dst+'/':'')+f.name;
  const r=await fetch('/api/images/'+EP(name),{method:'PUT',body:f});
  alert(r.ok?'uploaded':'upload failed: '+(await r.text()));refresh()}
refresh();
</script></body></html>"""

_SETTINGS_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>signPOST settings</title>
<style>
body{font-family:system-ui,sans-serif;background:#181820;color:#eee;max-width:680px;margin:1em auto;padding:0 1em}
button{background:#3a3a55;color:#eee;border:1px solid #555;border-radius:6px;padding:.4em .8em;margin:.2em;cursor:pointer}
select,input{background:#22222e;color:#eee;border:1px solid #555;border-radius:6px;padding:.4em;margin:.2em}
button:hover{background:#4a4a70}
.card{background:#22222e;border-radius:8px;padding:.7em .9em;margin:.6em 0}
small{opacity:.6}
a{color:#9cf}
#pv svg{max-width:100%;height:auto;background:#101014;border-radius:6px;display:block;margin:.4em 0}
h1{margin:.2em 0 .5em}
.flag{display:inline-block;position:relative;padding:.16em .85em .16em .55em;
  background:#5adc8c;font-family:ui-monospace,Menlo,monospace;font-weight:700;
  letter-spacing:.05em;
  clip-path:polygon(0 0,calc(100% - .75em) 0,100% 50%,calc(100% - .75em) 100%,0 100%)}
.flag::before{content:"";position:absolute;inset:2px;background:#263242;
  clip-path:polygon(0 0,calc(100% - .75em) 0,100% 50%,calc(100% - .75em) 100%,0 100%)}
.flag i{font-style:normal;color:#ffd75a;position:relative}
.flag b{color:#5adc8c;position:relative}
</style></head><body>
<h1><span class="flag"><i>sign</i><b>POST</b></span> <small>settings</small></h1>
<p><a href="/">&larr; back to sign</a></p>
<div class="card">
<b>Physical build</b><br>
panel size
<select id="pw" onchange="draw()">
<option value="32x32">32&times;32</option>
<option value="64x32">64&times;32</option>
<option value="64x64">64&times;64</option>
<option value="96x32">96&times;32</option>
<option value="96x48">96&times;48</option>
<option value="128x32">128&times;32</option>
<option value="128x64" selected>128&times;64</option>
</select>
grid <input id="cols" type="number" value="1" min="1" max="8" style="width:3.5em" onchange="draw()">
&times; <input id="rows" type="number" value="2" min="1" max="4" style="width:3.5em" onchange="draw()">
<div id="pv"></div>
<small id="pvmsg"></small><br>
<small>Letters are ribbon-chain order: A is first (data in), row by row. Different
panel sizes giving the same total canvas use the same display mode &mdash; the
firmware only sees the chain, not panel boundaries.</small>
</div>
<div class="card">
<b>Panel driver</b><br>
type <select id="ptype"><option value="generic">generic</option>
<option value="fm6126a">FM6126A</option></select>
<small>garbled image? try FM6126A</small><br>
color order <select id="cord"><option>rgb</option><option>rbg</option><option>grb</option>
<option>gbr</option><option>brg</option><option>bgr</option></select>
<small>swapped colours?</small><br>
<label><input type="checkbox" id="stb"> stb_invert <small>glitchy/inverted output?</small></label>
</div>
<p><button onclick="save()">Save &amp; Reboot</button> <small id="msg"></small></p>
<div class="card">
<b>Time zone</b><br>
UTC offset <input id="tz" type="number" step="1" min="-12" max="14" style="width:4.5em">
<button onclick="savetz()">Save</button> <small id="tzmsg"></small><br>
<small>applies immediately; used by the schedule and status clock</small>
</div>
<script>
const L='ABCDEFGH';let VALID=new Set();
function pv(i){return document.getElementById(i)}
function phys(){const[pw,ph]=pv('pw').value.split('x').map(Number);
  return{panel_w:pw,panel_h:ph,cols:+pv('cols').value,rows:+pv('rows').value}}
function draw(){const{panel_w:pw,panel_h:ph,cols,rows}=phys();
  const cw=pw*cols,ch=ph*rows,key=cw+'x'+ch,ok=VALID.has(key);
  let s='<svg viewBox="0 0 '+cw+' '+ch+'" width="280">';
  for(let r=0;r<rows;r++)for(let c=0;c<cols;c++){const i=r*cols+c,x=c*pw,y=r*ph;
    s+='<rect x="'+(x+2)+'" y="'+(y+2)+'" width="'+(pw-4)+'" height="'+(ph-4)+'" rx="4" fill="#26323f" stroke="#5adc8c" stroke-width="3"/>';
    s+='<text x="'+(x+pw/2)+'" y="'+(y+ph/2)+'" fill="#ffd75a" font-size="'+Math.min(pw,ph)/2.4+'" font-family="monospace" font-weight="bold" text-anchor="middle" dominant-baseline="middle">'+L[i]+'</text>'}
  pv('pv').innerHTML=s+'</svg>';
  pv('pvmsg').innerHTML=ok?'canvas '+cw+'&times;'+ch+' &rarr; uses display mode <b>'+key+'</b>'
    :'canvas '+cw+'&times;'+ch+' &mdash; <b>not a supported layout</b>'}
async function save(){const b=phys();b.panel=pv('ptype').value;b.color_order=pv('cord').value;
  b.stb_invert=pv('stb').checked;
  const j=await(await fetch('/api/display',{method:'POST',body:JSON.stringify(b)})).json();
  if(!j.ok){pv('msg').textContent='error: '+(j.error||'failed');return}
  await fetch('/api/reboot',{method:'POST'});
  pv('msg').textContent='saved — rebooting, back in ~15s'}
async function savetz(){
  const j=await(await fetch('/api/schedule',{method:'POST',
    body:JSON.stringify({tz:+pv('tz').value})})).json();
  pv('tzmsg').textContent=j.ok?'saved':'error: '+(j.error||'failed')}
(async()=>{const c=await(await fetch('/api/display')).json();
  VALID=new Set(c.layouts);pv('ptype').value=c.panel;pv('cord').value=c.color_order;
  pv('stb').checked=!!c.stb_invert;
  if(c.phys){pv('pw').value=c.phys.panel_w+'x'+c.phys.panel_h;
    pv('cols').value=c.phys.cols;pv('rows').value=c.phys.rows}
  const s=await(await fetch('/api/schedule')).json();
  pv('tz').value=s.schedule.tz;
  draw()})();
</script></body></html>"""

_PORTAL_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>signPOST wifi setup</title>
<style>
body{font-family:system-ui,sans-serif;background:#181820;color:#eee;max-width:420px;margin:2em auto;padding:0 1em}
input,select{width:100%;box-sizing:border-box;padding:.6em;margin:.3em 0;background:#22222e;color:#eee;border:1px solid #555;border-radius:6px}
button{width:100%;background:#3a6a3a;color:#eee;border:0;border-radius:6px;padding:.7em;font-size:1em;cursor:pointer}
button:hover{background:#4a7a4a}
#msg{margin-top:1em;opacity:.85}
h1{margin:.2em 0 .5em}
.flag{display:inline-block;position:relative;padding:.16em .85em .16em .55em;
  background:#5adc8c;font-family:ui-monospace,Menlo,monospace;font-weight:700;
  letter-spacing:.05em;
  clip-path:polygon(0 0,calc(100% - .75em) 0,100% 50%,calc(100% - .75em) 100%,0 100%)}
.flag::before{content:"";position:absolute;inset:2px;background:#263242;
  clip-path:polygon(0 0,calc(100% - .75em) 0,100% 50%,calc(100% - .75em) 100%,0 100%)}
.flag i{font-style:normal;color:#ffd75a;position:relative}
.flag b{color:#5adc8c;position:relative}
</style></head><body>
<h1><span class="flag"><i>sign</i><b>POST</b></span> <small>wifi setup</small></h1>
<select id="nets" onchange="document.getElementById('ssid').value=this.value">
<option value="">scanning for networks...</option>
</select>
<input id="ssid" placeholder="WiFi name (SSID)" autocomplete="off">
<input id="pass" type="password" placeholder="Password" autocomplete="off">
<button onclick="save()">Connect</button>
<div id="msg"></div>
<script>
async function scan(){
  try{
    const r=await (await fetch('/api/scan')).json();
    const s=document.getElementById('nets');
    s.innerHTML='<option value="">choose a network...</option>';
    for(const n of r.networks){
      const o=document.createElement('option');
      o.value=n.ssid;o.textContent=n.ssid+' ('+n.rssi+'dBm)';
      s.append(o);
    }
  }catch(e){document.getElementById('nets').innerHTML='<option value="">scan failed - type it below</option>'}
}
async function save(){
  const ssid=document.getElementById('ssid').value.trim();
  const password=document.getElementById('pass').value;
  if(!ssid)return alert('enter an SSID');
  document.getElementById('msg').textContent='saving...';
  const r=await fetch('/api/connect',{method:'POST',body:JSON.stringify({ssid:ssid,password:password})});
  const j=await r.json();
  document.getElementById('msg').textContent=j.ok?'Saved! The board is rebooting - it will join your network and show its IP.':'failed: '+j.error;
}
scan();setInterval(scan,15000);
</script></body></html>"""


# -- Entry point --------------------------------------------------------------


_extra_tasks = []


def _start_extras(dm, net):
    """mDNS and schedule — started once, on first connect."""
    if not _extra_tasks:
        _extra_tasks.append(asyncio.create_task(_schedule_task(dm)))
        _extra_tasks.append(asyncio.create_task(_mdns_task(net)))


def _on_connected(dm, server, net):
    """STA is up: kill the portal, banner for a bit, then restore + extras."""
    net.stop_ap()
    server.portal_mode = False
    dm.mode = "boot"
    dm.status_message(net.creds[0] if net.creds else "", net.wifi.ipv4())
    asyncio.create_task(_boot_then_restore(dm))
    _start_extras(dm, net)


async def _wifi_watchdog(dm, server, net):
    """Keep retrying a saved network while the portal is up, and reconnect
    after drops when running normally."""
    while True:
        await asyncio.sleep(10)
        if not net.wifi.isconnected() and net.creds:
            if not server.portal_mode:
                dm.status_message("WiFi lost", "reconnecting...")
            if await net.connect(retries=1):
                _on_connected(dm, server, net)


def _sync_time():
    try:
        ntptime.settime()
        return time.localtime()[0] >= 2025
    except (OSError, AttributeError):
        return False


def _local_minutes(tz):
    """Minutes since midnight = RTC (UTC) + tz offset in hours."""
    t = time.localtime()
    return (t[3] * 60 + t[4] + int(float(tz) * 60)) % 1440


async def _schedule_task(dm):
    """Switch between the 'show' and 'off_show' targets at the configured
    on/off times. Only acts on window transitions, so manual API calls are
    not fought in between."""
    synced = _sync_time()
    ticks = 0
    was_in = None
    while True:
        await asyncio.sleep(20)
        ticks += 1
        if ticks >= 1080:            # resync RTC roughly every 6h
            ticks = 0
            synced = _sync_time() or synced
        cfg = _load_schedule()
        if not cfg.get("enabled") or not synced:
            was_in = None
            continue
        on = _hhmm(str(cfg["on"]))
        off = _hhmm(str(cfg["off"]))
        if on is None or off is None:
            continue
        now = _local_minutes(cfg.get("tz", 0))
        inside = on <= now < off if on <= off else (now >= on or now < off)
        if inside == was_in:
            continue
        was_in = inside
        try:
            if inside and cfg.get("mode") == "slideshow":
                # cycle a source (empty = all root images) with interval-sec holds
                await dm.slideshow(float(cfg.get("interval", 8)),
                                   str(cfg.get("show") or "") or None)
            else:
                target = cfg["show"] if inside else cfg["off_show"]
                if target:
                    await dm.show(str(target),
                                  interval_ms=int(float(cfg.get("interval", 8)) * 1000))
        except (KeyError, ValueError, OSError):
            pass


_MDNS_QUERY = b"\x09" + MDNS_NAME.encode() + b"\x05local\x00"


async def _mdns_task(net):
    """Minimal mDNS responder so the board answers to MDNS_NAME.local."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("0.0.0.0", 5353))
        grp = socket.inet_aton("224.0.0.251")
        for opt in (getattr(socket, "IP_ADD_MEMBERSHIP", None), 3, 12):
            if opt is None:
                continue
            try:
                s.setsockopt(socket.IPPROTO_IP, opt,
                             grp + socket.inet_aton(net.wifi.ipv4()))
                break
            except OSError:
                continue
        s.setblocking(False)
    except OSError:
        return
    hdr = struct.pack(">HHHHHH", 0, 0x8400, 0, 1, 0, 0)
    p = select.poll()
    p.register(s, select.POLLIN)
    ip = ""
    resp = b""
    while True:
        if net.wifi.isconnected() and net.wifi.ipv4() != ip:
            ip = net.wifi.ipv4()
            resp = (hdr + _MDNS_QUERY +
                    struct.pack(">HHIH", 1, 0x8001, 120, 4) +
                    socket.inet_aton(ip))
        if p.poll(400):
            try:
                data, _ = s.recvfrom(512)
            except OSError:
                data = None
            if data and _MDNS_QUERY in data.lower():
                try:
                    s.sendto(resp, ("224.0.0.251", 5353))
                except OSError:
                    pass
        await asyncio.sleep(0.05)


# -- Button menu -------------------------------------------------------------

_MENU_HOLD_MS = 2000        # A+B held this long opens the menu
_MENU_TIMEOUT_MS = 15000    # idle menu exits itself

_MENU_TREE = [
    ("Next image", "next"),
    ("Slideshow", "slide"),
    ("Clear display", "clear"),
    ("Reset >", [
        ("Back", "back"),          # safe option first - defaults the cursor
        ("Network reset", "cfm:net"),
        ("Factory reset", "cfm:full"),
    ]),
    ("Exit", "exit"),
]


def _draw_menu(dm, title, items, sel):
    g = dm.graphics
    W, H = dm.width, dm.height
    g.set_font("bitmap8")
    g.set_pen(dm.black)
    g.clear()
    g.set_pen(dm.white)
    g.text(title, int((W - _text_w(g, title, 1)) // 2), 3, -1, 1)
    g.line(0, 13, W - 1, 13)
    y = 18
    for i, (label, _) in enumerate(items):
        if i == sel:
            g.set_pen(dm.white)
            g.rectangle(0, y - 1, W, 11)
            g.set_pen(dm.black)
        else:
            g.set_pen(dm.white)
        g.text(label, 4, y, -1, 1)
        y += 12
    dm.i75.update()


class _Menu:
    """On-screen menu: hold A+B to open, A cycles, B selects. Destructive
    entries confirm first; the menu restores whatever was on screen when it
    exits (unless an action replaced the display or a reset reboots)."""

    def __init__(self, dm, server):
        self.dm = dm
        self.server = server
        self.active = False
        self.last = time.ticks_ms()
        self._stack = []

    async def open(self):
        await self.dm._stop_player()
        self.active = True
        self._stack = [[_MENU_TREE, 0, "MENU"]]
        self.last = time.ticks_ms()
        self._draw()

    def _draw(self):
        items, sel, title = self._stack[-1]
        _draw_menu(self.dm, title, items, sel)

    def move(self):
        self.last = time.ticks_ms()
        top = self._stack[-1]
        top[1] = (top[1] + 1) % len(top[0])
        self._draw()

    async def select(self):
        self.last = time.ticks_ms()
        items, sel, title = self._stack[-1]
        act = items[sel][1]
        if isinstance(act, list):
            self._stack.append([act, 0, items[sel][0].rstrip(" >").upper()])
            self._draw()
        elif act == "back":
            self._stack.pop()
            self._draw()
        elif act == "exit":
            await self.close()
        elif act.startswith("cfm:"):
            kind = act[4:]
            title = "NETWORK RESET?" if kind == "net" else "FACTORY RESET?"
            self._stack.append([[("Cancel", "back"),
                                 ("Confirm", "do:" + kind)], 0, title])
            self._draw()
        elif act.startswith("do:"):
            self._reset(act[3:])
        else:
            await self.close(restore=False)
            await self._action(act)

    async def _leave(self):
        """Put back whatever the menu covered: the setup screen while the
        portal is up, otherwise the saved display state."""
        if getattr(self.server, "portal_mode", False):
            self.dm.status_message("SETUP MODE", AP_SSID, AP_IP)
        else:
            await _restore_state(self.dm)

    async def close(self, restore=True):
        self.active = False
        if restore:
            await self._leave()

    def _reset(self, kind):
        if kind == "net":
            _factory_reset()
            self.dm.status_message("Network reset", "rebooting...")
        else:
            _full_factory_reset()
            self.dm.status_message("Factory reset", "rebooting...")
        self.active = False
        asyncio.create_task(_reboot_later())

    async def _action(self, act):
        try:
            if act == "next":
                await self.dm.show_next()
            elif act == "slide":
                if self.dm.mode == "slideshow":
                    await self.dm.clear()
                else:
                    spec = self.dm.last_spec
                    if spec.get("t") == "slideshow":
                        await self.dm.slideshow(spec.get("interval", DEFAULT_SLIDESHOW_S),
                                                spec.get("source"))
                    else:
                        await self.dm.slideshow(DEFAULT_SLIDESHOW_S)
            elif act == "clear":
                await self.dm.clear()
        except (KeyError, ValueError, OSError):
            await self._leave()


async def _button_task(dm, server):
    """Front buttons: A = next image, B = toggle slideshow (last config).
    Hold A+B together to open the menu: A cycles, B selects."""
    menu = _Menu(dm, server)
    prev = [False, False]
    hold = 0
    while True:
        await asyncio.sleep(0.05)
        a = dm.i75.switch_pressed(SWITCH_A)
        b = dm.i75.switch_pressed(SWITCH_B)
        if menu.active:
            if a and not prev[0]:
                menu.move()
            elif b and not prev[1]:
                await menu.select()
            elif time.ticks_diff(time.ticks_ms(), menu.last) > _MENU_TIMEOUT_MS:
                await menu.close()
        elif a and b:
            hold += 50
            if hold >= _MENU_HOLD_MS:
                await menu.open()
                hold = -1 << 30        # latched - ignore until both released
        else:
            hold = 0
            if a and not prev[0]:
                try:
                    await dm.show_next()
                except (KeyError, ValueError, OSError):
                    pass
            elif b and not prev[1]:
                try:
                    if dm.mode == "slideshow":
                        await dm.clear()
                    else:
                        spec = dm.last_spec
                        if spec.get("t") == "slideshow":
                            await dm.slideshow(spec.get("interval", DEFAULT_SLIDESHOW_S),
                                               spec.get("source"))
                        else:
                            await dm.slideshow(DEFAULT_SLIDESHOW_S)
                except (KeyError, ValueError, OSError):
                    pass
        prev[0], prev[1] = a, b


async def _restore_state(dm):
    """Re-apply whatever was on screen before a reboot/power loss."""
    try:
        with open(STATE_FILE) as f:
            spec = json.loads(f.read())
    except (OSError, ValueError):
        return False
    t = spec.get("t")
    try:
        if t == "show":
            await dm.show(spec["name"], spec.get("interval_ms"), spec.get("loops"))
        elif t == "slideshow":
            await dm.slideshow(spec.get("interval", DEFAULT_SLIDESHOW_S),
                               spec.get("source"))
        elif t == "text":
            await dm.show_text(spec.get("params", {}))
        elif t == "demo":
            await dm.demo()
        else:
            return False
        return True
    except (KeyError, ValueError, OSError):
        return False


async def main():
    _ensure_dir(IMG_DIR)

    dm = DisplayManager()

    # Factory reset triggers: rapid power cycling, or a held button at boot.
    boot_reset = _check_bootcount_reset()
    button_reset = False if boot_reset else _check_button_reset(dm)
    if boot_reset or button_reset:
        _factory_reset()
        dm.status_message("WiFi reset", "creds wiped")

    net = NetManager()
    server = RestServer(dm, net)

    connected = False if (boot_reset or button_reset) else await _try_connect(dm, net)

    if connected:
        _on_connected(dm, server, net)
    else:
        server.portal_mode = True
        net.start_ap()
        asyncio.create_task(_dns_task(server))
        dm.status_message("SETUP MODE", AP_SSID, AP_IP)

    asyncio.create_task(_serve_forever(server))
    print("listening on port {}".format(PORT))
    asyncio.create_task(_wifi_watchdog(dm, server, net))
    asyncio.create_task(_button_task(dm, server))   # also works in portal mode
    asyncio.create_task(_clear_bootcount_later())
    if connected:
        _start_extras(dm, net)

    while True:
        await asyncio.sleep(3600)


async def _serve_forever(server):
    """Keep the HTTP listener alive: asyncio's accept task dies silently if
    it raises (e.g. MemoryError creating a per-connection task during a
    request burst) - log it and rebuild the listener instead."""
    while True:
        srv = await asyncio.start_server(server.handle, "0.0.0.0", PORT)
        while not srv.task.done():
            await asyncio.sleep(1)
        _log_exc(RuntimeError("accept task ended"))   # dead listener
        await asyncio.sleep(1)


async def _try_connect(dm, net):
    if not net.creds:
        return False
    dm.status_message("WiFi", "connecting...")
    ok = await net.connect(retries=3)
    if not ok:
        dm.status_message("WiFi", "FAILED")
    return ok


async def _boot_then_restore(dm):
    """Show the banner for BOOT_MESSAGE_S, then replay the saved display
    state (image/album/gif/slideshow/text). An API call during the banner
    cancels the restore; nothing saved or a failed restore -> blank."""
    await asyncio.sleep(BOOT_MESSAGE_S)
    if dm.mode != "boot":
        return
    if not await _restore_state(dm):
        await dm.clear()


def _loop_exc_handler(ctx):
    """Surface silently-dying task exceptions to errors.log."""
    try:
        _log_exc(ctx.get("exception")
                 or RuntimeError(str(ctx.get("message"))))
    except Exception:
        pass


try:
    _core.Loop.set_exception_handler(_loop_exc_handler)
except Exception:
    pass

try:
    asyncio.run(main())
    _log_exc(RuntimeError("asyncio.run returned cleanly"))
except BaseException as _e:                         # noqa: BLE001
    _log_exc(_e)
    raise
