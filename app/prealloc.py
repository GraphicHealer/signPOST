"""Early heap pre-allocation — imported before anything else (see main.py).

The framebuffer, PNG/JPEG decoders and GIF working buffers all need large
contiguous heap blocks. By the time the main app's own code is compiled and
its support modules loaded, the heap is too fragmented for those allocs to
succeed — so they happen here, while the heap is still clean.
"""
import json
from array import array

import interstate75
from interstate75 import Interstate75
import pngdec
import jpegdec

DISPLAY_MODES = {n: getattr(interstate75, "DISPLAY_INTERSTATE75_" + n.upper())
                 for n in ("32x32", "64x32", "96x32", "96x48", "64x64",
                           "128x32", "128x64", "192x64", "256x64", "128x128")}
PANEL_TYPES = {"generic": Interstate75.PANEL_GENERIC,     # try fm6126a if the image is garbled
               "fm6126a": Interstate75.PANEL_FM6126A}
COLOR_ORDERS = {n: getattr(Interstate75, "COLOR_ORDER_" + n.upper())
                for n in ("rgb", "rbg", "grb", "gbr", "brg", "bgr")}

DISPLAY_CFG_FILE = "display.json"   # panel layout / driver options


def load_display_config():
    cfg = {"display": "128x128", "panel": "generic",
           "color_order": "rgb", "stb_invert": False}
    try:
        with open(DISPLAY_CFG_FILE) as f:
            cfg.update(json.loads(f.read()))
    except (OSError, ValueError):
        pass
    if cfg["display"] not in DISPLAY_MODES:
        cfg["display"] = "128x128"
    if cfg["panel"] not in PANEL_TYPES:
        cfg["panel"] = "generic"
    if cfg["color_order"] not in COLOR_ORDERS:
        cfg["color_order"] = "rgb"
    return cfg


CFG = load_display_config()
try:
    I75 = Interstate75(display=DISPLAY_MODES[CFG["display"]],
                       panel_type=PANEL_TYPES[CFG["panel"]],
                       stb_invert=bool(CFG["stb_invert"]),
                       color_order=COLOR_ORDERS[CFG["color_order"]])
    GRAPHICS = I75.display
    PNG = pngdec.PNG(GRAPHICS)
    JPEG = jpegdec.JPEG(GRAPHICS)
except Exception as _e:
    try:
        with open("errors.log", "a") as _f:
            _f.write("boot: %r\n" % (_e,))
    except OSError:
        pass
    raise

# GIF decoder working buffers (~70KB), sized for the largest DISPLAY_*
# canvas (16384 px) regardless of the configured layout.
GIF_BUFS = {
    "canvas": bytearray(16384),
    "px": bytearray(16384),
    "pngbuf": bytearray(18432),
    "pref": array("h", bytes(8192)),
    "suff": bytearray(4096),
    "stack": bytearray(4096),
}
