# I75 REST Display - API Reference

Base URL: `http://<board-ip>` (the board shows its IP on the matrix at boot,
or see `GET /api/status` once you know it).

All endpoints return JSON (`{"ok": true|false, ...}`) except image downloads,
which return raw file bytes. CORS is enabled (`Access-Control-Allow-Origin: *`)
and `OPTIONS` preflights return `204`.

- [Display control](#display-control)
- [Image management](#image-management)
- [Albums (animations)](#albums-animations)
- [Text, LED, status](#text-led-status)
- [WiFi & system](#wifi--system)
- [Error codes](#error-codes)
- [Client examples](#client-examples)

## Endpoint summary

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/` | Control UI (web page) |
| GET | `/setup` | WiFi setup page |
| GET | `/api` | Service info |
| GET | `/api/status` | Board/display status |
| POST | `/api/show/<name>` | Display image, play album/gif |
| POST/PUT | `/api/show` | Same, name in body |
| POST | `/api/clear` | Blank display, stop playback |
| POST | `/api/random` | Show a random image |
| POST | `/api/slideshow` | Cycle all images, or one album/gif |
| POST | `/api/text` | Render text (static or scrolling) |
| POST | `/api/led` | Set onboard RGB LED |
| GET | `/api/images` | List images, gifs & albums |
| PUT/POST | `/api/images/<name>` | Upload image/gif (`?show=1` displays it) |
| GET | `/api/images/<name>` | Download image / list album or gif frames |
| DELETE | `/api/images/<name>` | Delete image/gif/album |
| PUT/POST | `/api/images/<album>/<frame>` | Upload album or gif frame |
| GET | `/api/images/<album>/<frame>` | Download album or gif frame |
| DELETE | `/api/images/<album>/<frame>` | Delete album or gif frame |
| GET | `/api/schedule` | Read the on/off schedule config |
| POST | `/api/schedule` | Update the on/off schedule config |
| GET | `/api/scan` | Scan WiFi networks |
| POST | `/api/connect` | Save WiFi creds & reboot |
| POST | `/api/reboot` | Reboot |
| POST | `/api/factory-reset` | Wipe WiFi creds & reboot into setup AP |

## Display control

### `POST /api/show/<name>`

Display an image, play an album or gif as an animation, or show one album/gif
frame statically (`<album>/<frame>`). `<name>` is an image filename, album
name, or gif name from `GET /api/images`.

```bash
curl -X POST http://signpost.local/api/show/cat.png
# {"ok": true, "showing": "cat.png", "mode": "image"}

curl -X POST http://signpost.local/api/show/demo/frame_1.png
# {"ok": true, "showing": "demo/frame_1.png", "mode": "image"}

curl -X POST http://signpost.local/api/show/anim.gif
# {"ok": true, "showing": "anim.gif", "mode": "gif"}

curl -X POST http://signpost.local/api/show/anim.gif/f010.png
# {"ok": true, "showing": "anim.gif/f010.png", "mode": "image"}
```

Gifs play at the frame rate saved from the source file unless `interval_ms`
is given in the body form. PNG decode takes ~65ms per frame, so for fast
gifs the player keeps source pacing by skipping frames whose slot has
already elapsed — motion stays at the right speed, just with fewer frames
drawn (the board tops out around 15fps for full-frame animation).

### `POST /api/show`

Same thing with the name in the body. Body may be JSON or a bare image name.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `image` (or `album`) | string | required | image or album name |
| `interval_ms` | int | 40 | frame delay for albums |
| `loops` | int | 0 | album loops (0 = forever) |

```bash
curl -X POST -d '{"image": "spin", "interval_ms": 30, "loops": 5}' \
     http://signpost.local/api/show

curl -X POST -d 'cat.png' http://signpost.local/api/show   # raw name also works
```

Also works via query params: `POST /api/show?image=cat.png`.

### `POST /api/clear`

Stop any album/slideshow playback and blank the display.
`/api/stop` is an alias. `DELETE` also accepted.

```bash
curl -X POST http://signpost.local/api/clear
# {"ok": true, "mode": "blank"}
```

### `POST /api/random`

Display a random image (files only, not albums). Returns `404` if the board
has no images.

```bash
curl -X POST http://signpost.local/api/random
# {"ok": true, "showing": "testcard.png"}
```

### `POST /api/slideshow`

Cycle through every top-level image file at `interval` seconds each. Runs
until `/api/clear`, `/api/show`, or `/api/text` is called. New uploads are
picked up on the next pass.

Pass `album` (alias `folder`) to cycle just that album's or gif's frames
instead. Frame order is the sorted filename order (`frame_1.png`,
`f001.png`, ...).

| Field | Type | Default | Meaning |
|---|---|---|---|
| `interval` | number | 5 | seconds per image (min 0.5) |
| `album` / `folder` | string | — | limit rotation to this album or gif (404 if missing) |

```bash
curl -X POST -d '{"interval": 10}' http://signpost.local/api/slideshow
# or: curl -X POST 'http://signpost.local/api/slideshow?interval=10'
# just one album:
curl -X POST -d '{"interval": 8, "album": "demo"}' http://signpost.local/api/slideshow
```

## Image management

Filenames must match `[A-Za-z0-9._-]{1,64}` (no slashes or `..`). Supported
extensions: `.png`, `.jpg`, `.jpeg`, `.gif`. Max upload size: 512 KB.

**All images and gifs must match the configured display size** — default
128x128, see `GET /api/display`. The board does no scaling: uploads of other
sizes are rejected (`400`), and stored content from a previous layout fails
to display with `400 image is WxH but the display is AxB` until re-uploaded
or the layout is changed back. Scale first, e.g.:
`convert in.gif -resize 128x128^ -gravity center -extent 128x128 out.gif`

Max upload size: **512 KB** for `.png`/`.jpg`, **4 MB** for `.gif` (the gif
body is stream-decoded to frames and never stored itself).

### `GET /api/images`

List everything stored on the board.

```bash
curl http://signpost.local/api/images
```

```json
{"ok": true, "images": [
  {"name": "cat.png", "type": "image", "size": 35778},
  {"name": "spin", "type": "album", "frames": 24},
  {"name": "anim.gif", "type": "gif", "frames": 52}
]}
```

### `PUT /api/images/<name>` (also `POST`)

Upload an image or gif. Body is the raw file bytes, `Content-Length` required
(chunked encoding is not supported). PNG/JPEG files are validated against the
image decoder on save - corrupt files get a `400` and are not kept. Dimensions
must match the configured display size or the upload is rejected (`400`).

A `.gif` upload is decoded on-board into a hidden album of PNG frames (the
original gif is not stored). It then behaves like an album under its gif
name: `show` plays it at the frame rate saved from the file, frames are
addressable as `<gif>/<frame>` (e.g. `anim.gif/f010.png`), and deleting
the gif name removes all frames. Decoding takes ~1-1.5s per frame, so a
large gif upload can take a minute or more.

| Query | Meaning |
|---|---|
| `?show=1` | display/play the upload immediately after it saves |

```bash
curl -X PUT --data-binary @cat.png http://signpost.local/api/images/cat.png
# {"ok": true, "name": "cat.png", "size": 35778}            -> 201

curl -X PUT --data-binary @anim.gif http://signpost.local/api/images/anim.gif
# {"ok": true, "name": "anim.gif", "type": "gif", "frames": 52} -> 201

curl -X PUT --data-binary @cat.png "http://signpost.local/api/images/cat.png?show=1"
```

Gif note: frames are stored as uncompressed-block PNGs (~17 KB each), so a
52-frame gif uses ~900 KB of flash — watch `free_bytes` in `/api/status`.

### `GET /api/images/<name>`

Download the raw file (handy for web `<img>` tags / backups). For an album
or gif, returns a JSON list of its frames instead.

```bash
curl -O http://signpost.local/api/images/cat.png
curl http://signpost.local/api/images/spin
# {"ok": true, "name": "spin", "type": "album", "frames": ["f_001.png", ...]}
curl http://signpost.local/api/images/anim.gif
# {"ok": true, "name": "anim.gif", "type": "gif", "frames": ["f001.png", ...]}
```

### `DELETE /api/images/<name>`

Delete an image, a gif, or an album **and all its frames**. If the deleted
item is currently being displayed, the display is cleared.

```bash
curl -X DELETE http://signpost.local/api/images/cat.png
# {"ok": true, "deleted": "cat.png"}
```

## Albums & gifs (animations)

Two ways to animate:

- **Album** — a folder under `/images` containing numbered PNG/JPEG frames.
  Upload frames one by one (see below); they play in filename-sorted order.
- **Gif** — upload a `.gif` and the board decodes it into a hidden album of
  PNG frames (`f001.png`, `f002.png`, ...). It lists as `{"type": "gif"}`,
  plays via `POST /api/show/<name>.gif` at the source framerate, and its
  frames are addressable under `<name>.gif/<frame>` for GET/PUT/DELETE/show.

### `PUT /api/images/<album>/<frame>` (also `POST`)

Upload one frame; creates the album folder automatically.

```bash
curl -X PUT --data-binary @frame_001.png http://signpost.local/api/images/spin/frame_001.png
curl -X PUT --data-binary @frame_002.png http://signpost.local/api/images/spin/frame_002.png
# ... frames play in filename-sorted order - zero-pad your numbers
```

Play it with `POST /api/show/spin` (see `interval_ms`/`loops` above).
`GET`/`DELETE`/`PUT` on a frame path work as you'd expect — for a gif use
its public name too (`/api/images/anim.gif/f010.png`).

## Text, LED, status

### `POST /api/text`

Render text onto the matrix (bitmap8 font). With `scroll` the text loops as
a marquee until something else is displayed.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `text` | string | `""` | text to draw |
| `x`, `y` | int | `2`, `64` | draw position |
| `scale` | int | `1` | font scale |
| `r`, `g`, `b` | int | `255` | colour |
| `clear` | bool | `true` | wipe display first |
| `scroll` | bool | `false` | loop the text as a marquee |
| `speed_ms` | int | `80` | ms per scroll step (min 10) |
| `step` | int | `2` | pixels per scroll step |

```bash
curl -X POST -d '{"text": "DINNER!", "scale": 2, "y": 56}' http://signpost.local/api/text
# overlay without clearing: {"text": "now", "clear": false, "x": 10, "y": 10}
# scrolling announcement:
curl -X POST -d '{"text": "Now playing: Jingle Bell Rock  ", "scroll": true, "scale": 1, "speed_ms": 60}' \
     http://signpost.local/api/text
```

### `POST /api/led`

Set the onboard RGB LED (0-255 per channel).

```bash
curl -X POST -d '{"r": 0, "g": 40, "b": 0}' http://signpost.local/api/led
```

### `GET /api/status`

```bash
curl http://signpost.local/api/status
```

```json
{
  "ok": true,
  "mode": "image",            // blank | boot | image | album | gif | slideshow | text
  "current": "cat.png",       // name being shown, or null
  "wifi_connected": true,
  "ip": "signpost.local",
  "mdns": "demo.local",  // the board also answers to this hostname
  "utc": "15:50",             // current UTC from NTP (absent until synced)
  "local": "10:50",           // UTC + the schedule's "tz" offset
  "portal_mode": false,       // true while serving the setup portal
  "ap_active": false,         // setup AP running
  "images": 3,                // images + albums + gifs stored
  "free_bytes": 1994752,      // free flash bytes
  "buttons": [false, false, false]
}
```

## Schedule (autonomous on/off)

The board syncs its clock over NTP and can switch the display between two
targets at configured local times — e.g. play the `demo` album during
evening hours and a static "show is off" image the rest of the day.

### `GET /api/schedule`

```bash
curl http://signpost.local/api/schedule
```

```json
{"ok": true, "schedule": {
  "enabled": false, "on": "18:00", "off": "23:00",
  "mode": "show", "show": "", "off_show": "", "tz": 0, "interval": 8}}
```

### `POST /api/schedule`

Update any subset of the fields; persisted to `schedule.json` on the board
and reloaded every ~20s.

| Field | Type | Meaning |
|---|---|---|
| `enabled` | bool | master switch for the schedule |
| `on` | `"HH:MM"` | local time the show window starts |
| `off` | `"HH:MM"` | local time the show window ends (wraps midnight OK) |
| `mode` | `"show"` / `"slideshow"` | how `show` plays inside the window |
| `show` | string | image/album/gif to display inside the window (in `slideshow` mode this is the source — an album/gif name, or empty for all root images) |
| `off_show` | string | image/album/gif to display outside the window |
| `tz` | int | timezone offset from UTC in hours (e.g. `-5` for EST) |
| `interval` | number | seconds per frame (album frame delay in `show` mode, per-image hold in `slideshow` mode) |

The task only acts when the window *transitions* — manual `/api/show` calls
in between are not fought, but get overridden at the next boundary.

```bash
curl -X POST -d '{"enabled": true, "on": "18:00", "off": "23:00", "show": "demo", "off_show": "LightShow-off.png", "tz": -5, "interval": 8}' \
     http://signpost.local/api/schedule
```

## WiFi & system

### `GET /api/scan`

Scan for nearby WiFi networks (used by the setup portal).

```bash
curl http://signpost.local/api/scan
# {"ok": true, "networks": [{"ssid": "HomeNet", "rssi": -42, "security": 5}, ...]}
```

### `POST /api/connect`

Save WiFi credentials to `secrets.py` and reboot onto that network.
~1.5s after responding, the board resets.

```bash
curl -X POST -d '{"ssid": "HomeNet", "password": "hunter2"}' \
     http://signpost.local/api/connect
# {"ok": true, "message": "credentials saved, rebooting"}
```

### `GET /setup`

The WiFi setup page (same one the captive portal serves). Reachable any
time - handy for reconfiguring WiFi without a factory reset.

### `POST /api/reboot`

Reboot the board (~1.5s delay so the response can flush).

```bash
curl -X POST http://signpost.local/api/reboot
```

### `POST /api/factory-reset`

Delete `secrets.py` (WiFi credentials only - images are kept) and reboot
into the `I75-Setup` access point.

```bash
curl -X POST http://signpost.local/api/factory-reset
```

## Board behaviour notes

- **State resume** — the display mode/current source is saved to `state.json`
  whenever it changes, and restored after reboot/power loss (image, album,
  gif, slideshow, text all resume automatically).
- **Boot banner** — on boot the matrix shows the board's IP for ~30s, then
  auto-clears (unless something was displayed in the meantime).
- **Front buttons** — **A**: next root image. **B**: toggle the slideshow.
- **mDNS** — the board answers to `demo.local`; prefer that over the
  raw IP in clients (the IP can change across networks).
- **NTP** — UTC is synced at boot and ~every 6h; needed by the schedule.

## Error codes

Errors return `{"ok": false, "error": "..."}` with an HTTP status:

| Code | Meaning |
|---|---|
| 400 | Bad name, missing field, corrupt image, wrong dimensions (not 128x128), bad gif |
| 404 | Image/album/gif/endpoint not found |
| 405 | Wrong HTTP method for the endpoint |
| 411 | No `Content-Length` on a body (chunked not supported) |
| 413 | Upload over cap (512 KB images / 4 MB gifs) or JSON body over 64 KB |
| 415 | Unsupported file type (not .png/.jpg/.jpeg/.gif) |
| 500 | Internal error (details also written to `errors.log` on the board) |
| 507 | Not enough free flash space (incl. decoded gif frames) |

## Client examples

### Home Assistant

```yaml
rest_command:
  i75_show:
    url: "http://signpost.local/api/show/{{ image }}"
    method: POST
  i75_clear:
    url: "http://signpost.local/api/clear"
    method: POST
  i75_random:
    url: "http://signpost.local/api/random"
    method: POST
  i75_slideshow:
    url: "http://signpost.local/api/slideshow"
    method: POST
    content_type: application/json
    payload: '{"interval": {{ interval | default(5) }}}'
  i75_text:
    url: "http://signpost.local/api/text"
    method: POST
    content_type: application/json
    payload: '{"text": "{{ text }}", "scale": {{ scale | default(1) }}}'
  i75_led:
    url: "http://signpost.local/api/led"
    method: POST
    content_type: application/json
    payload: '{"r": {{ r | default(0) }}, "g": {{ g | default(0) }}, "b": {{ b | default(0) }}}'

shell_command:
  i75_upload: "curl -X PUT --data-binary @/config/www/i75/{{ image }} http://signpost.local/api/images/{{ image }}?show=1"
```

REST sensor for status:

```yaml
sensor:
  - platform: rest
    resource: http://signpost.local/api/status
    name: "I75 Display"
    value_template: "{{ value_json.mode }} - {{ value_json.current or 'nothing' }}"
    scan_interval: 60
```

### FPP / shell scripts

```bash
#!/bin/bash
I75=signpost.local
curl -X POST http://$I75/api/show/santa.png
# later:
curl -X POST http://$I75/api/clear
```

### Python

```python
import requests

I75 = "http://signpost.local"

requests.put(f"{I75}/api/images/cat.png?show=1", data=open("cat.png", "rb"))
requests.post(f"{I75}/api/show", json={"image": "cat.png"})
requests.post(f"{I75}/api/text", json={"text": "hello", "scale": 2})
print(requests.get(f"{I75}/api/status").json())
```

## Display settings

`GET /api/display` returns the panel layout + driver config, the runtime
`active` layout/dims, and the list of valid `layouts`.

`POST /api/display` configures it (persisted in `display.json`, applied at
boot — reboot required):

```json
{"display": "64x64"}
```

or describe the physical build and let the backend resolve the mode:

```json
{"panel_w": 64, "panel_h": 64, "cols": 2, "rows": 2}
// -> 4x 64x64 in a 2x2 grid -> display "128x128"
```

Optional driver knobs: `"panel": "generic"|"fm6126a"`,
`"color_order": "rgb"|"rbg"|"grb"|"gbr"|"brg"|"bgr"`, `"stb_invert": bool`.

`GET /settings` serves a settings page with a live panel-layout preview.

The HUB75 driver only sees one serial chain — panel boundaries are a
physical/labeling concern; any tiling that produces a supported canvas
size shares the same display mode. Chain order is row-major (A, B, C...
left to right, top to bottom). Images and GIFs must match the active
canvas size.
