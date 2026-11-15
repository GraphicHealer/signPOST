"""gifdec.py — incremental GIF89a decoder for the Interstate75 REST display.

Decodes a GIF stream fed in arbitrary chunks, composites each frame onto a
DISPLAY_W x DISPLAY_H palette-index canvas, and writes each composited frame
as fNNN.png — an indexed-color PNG using zlib "stored" (uncompressed)
deflate blocks, since the firmware ships no compressor. Runs on both
MicroPython and CPython (struct/array/binascii only).

Usage:
    dec = GifDecoder(out_dir="images/.anim.gif")
    for chunk in chunks:
        dec.feed(chunk)
    dec.feed(b"")
    delays = dec.delays   # per-frame delays in ms
"""

import binascii
import gc
import struct
from array import array

DISPLAY_W = 128
DISPLAY_H = 128

# PNG layout: sig(8) + IHDR(4+4+13+4) + PLTE(4+4+768+4) + IDAT(4+4+zlib+4) + IEND(12)
# zlib = 2 hdr + 5 stored-block hdr + (1+W)*H raw rows + 4 adler
_PNG_MAX = 8 + 25 + 780 + 12 + 2 + 5 + (1 + DISPLAY_W) * DISPLAY_H + 4 + 12
_PNG_SIG = b"\x89PNG\r\n\x1a\n"


def _adler32(data):
    s1, s2 = 1, 0
    for b in data:
        s1 += b
        s2 += s1
    return (s2 % 65521) << 16 | (s1 % 65521)



class GifError(Exception):
    pass


def _deinterlace(src, dst, w, h):
    s = 0
    for start, step in ((0, 8), (4, 8), (2, 4), (1, 2)):
        for y in range(start, h, step):
            dst[y * w:(y + 1) * w] = src[s * w:(s + 1) * w]
            s += 1


class GifDecoder:
    """Feed bytes; completed composited frames are written to out_dir as
    fNNN.png (stored-block indexed PNG). The big buffers live for the
    decoder's lifetime — construct it once at app start (while the heap is
    fresh) and call reset() for each new gif."""

    def __init__(self, out_dir=None):
        # fixed working buffers, allocated once
        self.canvas = bytearray(DISPLAY_W * DISPLAY_H)
        self.px = bytearray(DISPLAY_W * DISPLAY_H)
        self.pngbuf = bytearray(_PNG_MAX)     # whole frame assembled in place
        self.pref = array("h", bytes(8192))   # LZW prefix chains (int16)
        self.suff = bytearray(4096)           # LZW suffix bytes
        self.stack = bytearray(4096)          # LZW string expansion stack
        # rare-case buffers, allocated lazily on first need
        self.scratch = None                   # deinterlace target
        self.undo = None                      # disposal-3 snapshot
        self.reset(out_dir)

    def reset(self, out_dir=None):
        """Re-arm the decoder for a new gif stream, reusing the buffers."""
        self.out_dir = out_dir
        self.buf = bytearray()
        self.pos = 0
        self.stage = "header"
        self.done = False
        self.width = self.height = 0
        self.gct = None
        self.bg = 0
        self.gce_disposal = 0
        self.gce_delay = 10
        self.gce_tidx = -1
        self.frames = 0
        self.delays = []
        # per-image-block working state
        self.img_x = self.img_y = self.img_w = self.img_h = 0
        self.img_interlace = False
        self.img_lct = None
        self.sub_left = 0          # bytes left in current sub-block
        # incremental LZW state (per frame)
        self.lzw_size = self.lzw_nxt = self.lzw_prev = 0
        self.lzw_min_code = 0
        self.lzw_acc = self.lzw_nbits = 0
        self.lzw_o = self.lzw_npix = 0
        self.lzw_clear = self.lzw_eoi = 0
        self.lzw_done = False

    def feed(self, data):
        # compact first so the buffer never needs a large contiguous realloc
        if self.pos:
            self.buf = self.buf[self.pos:]
            self.pos = 0
        self.buf += data
        out = []
        while not self.done:
            ev = self._step()
            if ev is None:
                break
            if ev is not True:
                out.append(ev)
        return out

    # -- helpers --

    def _need(self, n):
        if len(self.buf) - self.pos < n:
            return None
        r = bytes(self.buf[self.pos:self.pos + n])
        self.pos += n
        return r

    # -- parser state machine --

    def _step(self):
        st = self.stage
        if st == "header":
            h = self._need(6)
            if h is None:
                return None
            if h[:3] != b"GIF" or h[3:6] not in (b"87a", b"89a"):
                raise GifError("not a GIF")
            self.stage = "lsd"
            return True
        if st == "lsd":
            d = self._need(7)
            if d is None:
                return None
            self.width, self.height, packed, self.bg, _ = struct.unpack("<HHBBB", d)
            if (self.width, self.height) != (DISPLAY_W, DISPLAY_H):
                raise GifError("gif must be {}x{} (got {}x{})".format(
                    DISPLAY_W, DISPLAY_H, self.width, self.height))
            fill = self.bg
            for i in range(DISPLAY_W * DISPLAY_H):
                self.canvas[i] = fill
            self.stage = "gct" if packed & 0x80 else "block"
            self.gct_size = 2 << (packed & 7) if packed & 0x80 else 0
            return True
        if st == "gct":
            d = self._need(3 * self.gct_size)
            if d is None:
                return None
            self.gct = d
            self.stage = "block"
            return True
        if st == "block":
            d = self._need(1)
            if d is None:
                return None
            if d == b";":                # trailer
                self.done = True
                return True
            if d == b"!":
                self.stage = "ext_label"
                return True
            if d == b",":
                self.stage = "img_desc"
                return True
            raise GifError("bad block marker 0x{:02x}".format(d[0]))
        if st == "ext_label":
            d = self._need(1)
            if d is None:
                return None
            if d == b"\xf9":
                self.stage = "gce_len"
            else:
                self.stage = "skip_sub"
            return True
        if st == "gce_len":
            d = self._need(1)
            if d is None:
                return None
            self.sub_left = d[0]
            self.stage = "gce_data"
            return True
        if st == "gce_data":
            d = self._need(self.sub_left + 1)   # payload + terminator
            if d is None:
                return None
            packed = d[0]
            self.gce_disposal = (packed >> 2) & 7
            self.gce_tidx = d[3] if packed & 1 else -1
            self.gce_delay = d[1] | (d[2] << 8)
            self.stage = "block"
            return True
        if st == "skip_sub":
            d = self._need(1)
            if d is None:
                return None
            n = d[0]
            if n == 0:
                self.stage = "block"
            else:
                self.sub_left = n
                self.stage = "skip_data"
            return True
        if st == "skip_data":
            d = self._need(self.sub_left)
            if d is None:
                return None
            self.stage = "skip_sub"
            return True
        if st == "img_desc":
            d = self._need(9)
            if d is None:
                return None
            (self.img_x, self.img_y, self.img_w, self.img_h,
             packed) = struct.unpack("<HHHHB", d)
            self.img_interlace = bool(packed & 0x40)
            if packed & 0x80:
                self.img_lct_size = 2 << (packed & 7)
                self.stage = "img_lct"
            else:
                self.img_lct = None
                self.stage = "img_min"
            return True
        if st == "img_lct":
            d = self._need(3 * self.img_lct_size)
            if d is None:
                return None
            self.img_lct = d
            self.stage = "img_min"
            return True
        if st == "img_min":
            d = self._need(1)
            if d is None:
                return None
            min_code = d[0]
            if not 2 <= min_code <= 8:
                raise GifError("bad LZW min code")
            npix = self.img_w * self.img_h
            if npix > DISPLAY_W * DISPLAY_H:
                raise GifError("frame too large")
            self.lzw_clear = 1 << min_code
            self.lzw_eoi = self.lzw_clear + 1
            self.lzw_min_code = min_code
            self.lzw_size = min_code + 1
            self.lzw_nxt = self.lzw_eoi + 1
            self.lzw_prev = -1
            self.lzw_acc = 0
            self.lzw_nbits = 0
            self.lzw_o = 0
            self.lzw_npix = npix
            self.lzw_done = False
            self.stage = "img_sub"
            return True
        if st == "img_sub":
            d = self._need(1)
            if d is None:
                return None
            n = d[0]
            if n == 0:
                return self._finish_frame()
            self.sub_left = n
            self.stage = "img_data"
            return True
        if st == "img_data":
            d = self._need(self.sub_left)
            if d is None:
                return None
            self._lzw_feed(d)      # decoded straight into self.px — no storage
            self.stage = "img_sub"
            return True
        raise GifError("bad parser state " + st)

    # -- compositing + output --

    def _lzw_feed(self, data):
        """Consume one sub-block of compressed image data. GIF-variant LZW,
        fully incremental across calls — output lands directly in self.px."""
        if self.lzw_done:
            return
        clear = self.lzw_clear
        eoi = self.lzw_eoi
        size = self.lzw_size
        nxt = self.lzw_nxt
        prev = self.lzw_prev
        acc = self.lzw_acc
        nbits = self.lzw_nbits
        o = self.lzw_o
        npix = self.lzw_npix
        pref = self.pref
        suff = self.suff
        stack = self.stack
        out = self.px
        pos = 0
        dlen = len(data)

        while o < npix:
            while nbits < size:
                if pos >= dlen:
                    self.lzw_size = size
                    self.lzw_nxt = nxt
                    self.lzw_prev = prev
                    self.lzw_acc = acc
                    self.lzw_nbits = nbits
                    self.lzw_o = o
                    return
                acc |= data[pos] << nbits
                nbits += 8
                pos += 1
            code = acc & ((1 << size) - 1)
            acc >>= size
            nbits -= size

            if code == clear:
                nxt = eoi + 1
                size = self.lzw_min_code + 1
                prev = -1
                continue
            if code == eoi:
                self.lzw_done = True
                self.lzw_o = o
                return
            if prev < 0:
                out[o] = code
                o += 1
                prev = code
                continue

            top = 0
            c = code if code < nxt else prev
            while c > eoi:
                stack[top] = suff[c]
                top += 1
                c = pref[c]
            first = c
            if code >= nxt:
                # KwKwK case: code is the entry being created right now
                stack[top] = first
                top += 1

            out[o] = first
            o += 1
            while top:
                top -= 1
                out[o] = stack[top]
                o += 1

            if nxt < 4096:
                pref[nxt] = prev
                suff[nxt] = first
                nxt += 1
            if nxt == (1 << size) and size < 12:
                size += 1
            prev = code

        self.lzw_done = True
        self.lzw_o = o

    def _finish_frame(self):
        fw, fh = self.img_w, self.img_h
        gc.collect()
        n = self.lzw_o
        if n < fw * fh:
            self.px[n:fw * fh] = bytes(fw * fh - n)
        src = self.px
        if self.img_interlace:
            if self.scratch is None:
                self.scratch = bytearray(DISPLAY_W * DISPLAY_H)
            _deinterlace(self.px, self.scratch, fw, fh)
            src = self.scratch

        disposal = self.gce_disposal
        if disposal == 3:
            if self.undo is None:
                self.undo = bytearray(DISPLAY_W * DISPLAY_H)
            self.undo[:] = self.canvas

        x, y = self.img_x, self.img_y
        cw = min(fw, DISPLAY_W - x)
        ch = min(fh, DISPLAY_H - y)
        tidx = self.gce_tidx
        if tidx < 0:
            for r in range(ch):
                dst = (y + r) * DISPLAY_W + x
                self.canvas[dst:dst + cw] = src[r * fw:(r * fw) + cw]
        else:
            for r in range(ch):
                base = r * fw
                dbase = (y + r) * DISPLAY_W + x
                for c in range(cw):
                    p = src[base + c]
                    if p != tidx:
                        self.canvas[dbase + c] = p

        palette = self.img_lct or self.gct
        self.frames += 1
        self.delays.append(self.gce_delay * 10)   # centiseconds -> ms
        if self.out_dir:
            f = open("{}/f{:03d}.png".format(self.out_dir, self.frames), "wb")
            f.write(self._png_bytes(palette))
            f.close()
            ret = True          # keep stepping; frame went straight to disk
        else:
            ret = bytes(self.canvas)

        # disposal of the frame just emitted, before the next one draws
        if disposal == 2:
            for r in range(ch):
                dst = (y + r) * DISPLAY_W + x
                for c in range(cw):
                    self.canvas[dst + c] = self.bg
        elif disposal == 3:
            self.canvas[:] = self.undo

        self.gce_disposal = 0
        self.gce_delay = 10
        self.gce_tidx = -1
        self.stage = "block"
        return ret

    def _png_bytes(self, palette):
        """Assemble an indexed-color PNG of self.canvas into self.pngbuf.
        zlib stream uses a single "stored" deflate block — no compressor
        needed — so the cost is just row copies plus CRC/Adler."""
        b = self.pngbuf
        o = 0
        b[o:o + 8] = _PNG_SIG
        o += 8

        struct.pack_into(">I", b, o, 13)        # IHDR
        o += 4
        at = o
        b[o:o + 4] = b"IHDR"
        o += 4
        struct.pack_into(">IIBBBBB", b, o, DISPLAY_W, DISPLAY_H, 8, 3, 0, 0, 0)
        o += 13
        struct.pack_into(">I", b, o, binascii.crc32(b[at:o]))
        o += 4

        struct.pack_into(">I", b, o, 768)       # PLTE
        o += 4
        at = o
        b[o:o + 4] = b"PLTE"
        o += 4
        if palette:
            b[o:o + len(palette)] = palette
            o += len(palette)
            pad = 768 - len(palette)
        else:
            pad = 768
        if pad:
            b[o:o + pad] = bytes(pad)
            o += pad
        struct.pack_into(">I", b, o, binascii.crc32(b[at:o]))
        o += 4

        idat_len_at = o                         # IDAT
        b[o + 4:o + 8] = b"IDAT"
        o += 8
        data_at = o
        b[o:o + 2] = b"\x78\x01"                # zlib: stored, no dict
        o += 2
        raw_n = (1 + DISPLAY_W) * DISPLAY_H
        b[o] = 1                                # deflate: final stored block
        struct.pack_into("<HH", b, o + 1, raw_n, raw_n ^ 0xFFFF)
        o += 5
        raw_at = o
        canvas = self.canvas
        for r in range(DISPLAY_H):
            b[o] = 0                            # filter: none
            o += 1
            s = r * DISPLAY_W
            b[o:o + DISPLAY_W] = canvas[s:s + DISPLAY_W]
            o += DISPLAY_W
        struct.pack_into(">I", b, o, _adler32(memoryview(b)[raw_at:o]))
        o += 4
        struct.pack_into(">I", b, idat_len_at, o - data_at)
        struct.pack_into(">I", b, o, binascii.crc32(memoryview(b)[data_at - 4:o]))
        o += 4

        b[o:o + 4] = b"\x00\x00\x00\x00"        # IEND
        o += 4
        b[o:o + 4] = b"IEND"
        o += 4
        struct.pack_into(">I", b, o, binascii.crc32(b[o - 4:o]))
        o += 4
        return memoryview(b)[:o]
