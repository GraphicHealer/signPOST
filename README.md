<p align="center"><img src="icon.png" width="200" alt="signPOST"></p>

# signPOST

REST-controlled digital signage firmware for the **Pimoroni Interstate 75 W
(RP2350)** driving a 128x128 HUB75 matrix (two chained 128x64 panels).

Flash the UF2, connect WiFi through the captive portal, and you have a
sign: `http://signpost.local` serves a web UI + JSON API for images,
albums, GIFs, slideshows, scrolling text, and a scheduled on/off window.

## Flashing

1. Hold BOOTSEL on the Interstate 75 W and plug in USB.
2. Drop `signPOST-<version>.uf2` from the latest release onto the `RP2350` drive.
3. On first boot the board starts an open AP **`signPOST-Setup`** —
   join it, open `http://192.168.4.1`, pick your WiFi, Connect.
4. The board reboots onto your network, shows `signpost` / SSID / IP for
   30 s, then starts the built-in demo — procedurally drawn screens, so
   it works at any panel size. Any real content (upload/show/clear)
   retires it.

## What ships on the filesystem

| File | Purpose |
|---|---|
| `main.py` | tiny launcher: imports `prealloc`, then `signpost` |
| `prealloc.py` | display hardware + big buffers, allocated while the heap is clean |
| `signpost.mpy` | the whole sign app (server, display manager, tasks), precompiled |
| `gifdec.mpy` | incremental GIF89a → PNG-frame decoder, precompiled |
| `state.json` | boot state: built-in procedural demo — "BOOM, a sign" |

`signpost.py`/`gifdec.py` are the sources; builds compile them to `.mpy`
with mpy-cross because on-device compile of the ~1900-line app churns the
heap enough to starve the framebuffer/decoder allocations at boot.

Not baked in (created at runtime): `secrets.py` (WiFi creds, saved by the
portal), `schedule.json`, `bootcount`, `errors.log`, uploaded images.

## API sketch

```
POST /api/show/<name>          show image, play album/gif, or one frame
POST /api/slideshow            {"interval": 8}                          # all root images
GET/PUT/DELETE /api/images/... upload, download, delete images/frames
POST /api/text                 {"text": "HI", "scroll": true}
GET/POST /api/schedule         on/off window, mode, show target, tz
POST /api/clear  /api/random  /api/led  /api/reboot  /api/factory-reset
```

Images must match the configured display size (default **128x128** — see
`GET /api/display`); GIFs are stream-decoded to a hidden PNG-frame album
and replayed at their saved frame rate.

Dev deploy (after editing `app/` sources):

```bash
mpy-cross app/signpost.py -o app/signpost.mpy
mpy-cross app/gifdec.py -o app/gifdec.mpy
mpremote cp app/main.py app/prealloc.py app/signpost.mpy app/gifdec.mpy :
```

Full reference: [API.md](API.md).

## Building

### GitHub Actions

`.github/workflows/build-uf2.yml` builds both UF2s on `workflow_dispatch`
or when a **Release** is created (UF2s are attached to it).

### Local

Requires cmake, ninja, arm-none-eabi-gcc, python3 + `littlefs-python`:

```bash
./ci/build.sh            # clones micropython + pimoroni-pico into ./build
```

Outputs land in `build/build-signpost_i75w_rp2350/`:

- `firmware-with-filesystem.uf2` — the full image; releases rename it to `signPOST-<version>.uf2`
- `firmware.uf2` — MicroPython + frozen modules only (no baked app/filesystem)

## Layout

```
app/                    -> baked onto the device's littlefs root
boards/signpost_i75w_rp2350/  MicroPython board definition (UF2 manifest
                              points at app/, freezing boards/modules)
modules/                pimoroni micropython modules (interstate75, ezwifi)
ci/build.sh             local build
.github/workflows/      CI build + release attach
```
