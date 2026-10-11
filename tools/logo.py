#!/usr/bin/env python3
"""logo.py - il logo ouroBIOS (assets/ourobios.svg) in altre forme.

  tools/logo.py c   [assets/ourobios.svg]   l'immagine LVGL (lv_image_dsc_t,
                                            ARGB8888 16x16) per la barra dei
                                            menu del setup (patch lvglpkg)
  tools/logo.py png OUT.png [SCALA] [SFONDO] un PNG, pixel ingranditi di SCALA
                                            (default 32), sfondo esadecimale
                                            (default trasparente)
  tools/logo.py splash OUT.bmp               il boot splash (assets/bootsplash.bmp):
                                            logo, nome e motto su nero, BMP 24 bit
                                            che EDK2 centra sullo schermo (serve
                                            Pillow, con i font DejaVu)

Il logo e' pixel art 16x16: nel SVG ogni <rect> e' un pixel (o una fila di
pixel) con il suo colore. Solo la libreria standard, piu' zlib per il PNG.
"""
import re
import struct
import sys
import zlib

SIZE = 16

# Il boot splash: sfondo nero (EDK2 cancella lo schermo in nero attorno
# all'immagine), logo a 12x, poi il nome e il motto nei colori del tema
SPLASH_W, SPLASH_H = 640, 470
SPLASH_LOGO_SCALE = 12
SPLASH_BG = 0x000000
SPLASH_NAME = "ouroBIOS"
SPLASH_NAME_COLOR = 0xF5F5F7
SPLASH_TAGLINE = "REBORN BY DESIGN."
SPLASH_TAGLINE_COLOR = 0xA1A1A6
SPLASH_LINE = "COREBOOT  \u00b7  EDK II  \u00b7  THINKPAD W541"
SPLASH_LINE_COLOR = 0x636366
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


def pixels(svg):
    """La griglia 16x16: per ogni pixel il colore 0xRRGGBB o None."""
    grid = [[None] * SIZE for _ in range(SIZE)]
    for m in re.finditer(r'<rect x="(\d+)" y="(\d+)" width="(\d+)" height="(\d+)" fill="#([0-9A-Fa-f]{6})"', svg):
        x, y, w, h = (int(m.group(i)) for i in range(1, 5))
        for yy in range(y, y + h):
            for xx in range(x, x + w):
                grid[yy][xx] = int(m.group(5), 16)
    return grid


def as_c(grid):
    out = ["//", "// ouroBIOS, 16x16, from assets/ourobios.svg (tools/logo.py c)", "//",
           "STATIC CONST UINT8  mOuroBiosLogoData[%d * %d * 4] = {" % (SIZE, SIZE)]
    for row in grid:
        vals = []
        for p in row:
            if p is None:
                vals += [0, 0, 0, 0]
            else:
                vals += [p & 0xff, (p >> 8) & 0xff, p >> 16, 0xff]  # B G R A
        out.append("  " + ", ".join("0x%02x" % v for v in vals) + ",")
    out.append("};")
    out.append("")
    out.append("STATIC CONST lv_image_dsc_t  mOuroBiosLogo = {")
    out.append("  .header.magic  = LV_IMAGE_HEADER_MAGIC,")
    out.append("  .header.cf     = LV_COLOR_FORMAT_ARGB8888,")
    out.append("  .header.flags  = 0,")
    out.append("  .header.w      = %d," % SIZE)
    out.append("  .header.h      = %d," % SIZE)
    out.append("  .header.stride = %d * 4," % SIZE)
    out.append("  .data_size     = sizeof (mOuroBiosLogoData),")
    out.append("  .data          = mOuroBiosLogoData,")
    out.append("};")
    return "\n".join(out) + "\n"


def as_png(grid, scale, bg):
    w = h = SIZE * scale
    rows = b""
    for y in range(h):
        row = b"\0"
        for x in range(w):
            p = grid[y // scale][x // scale]
            if p is None:
                row += struct.pack("BBBB", (bg >> 16) & 255, (bg >> 8) & 255, bg & 255, 255) if bg is not None else b"\0\0\0\0"
            else:
                row += struct.pack("BBBB", p >> 16, (p >> 8) & 255, p & 255, 255)
        rows += row

    def chunk(kind, data):
        c = kind + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xffffffff)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 9)) + chunk(b"IEND", b""))


def spaced(draw, xy, text, font, fill, spacing):
    """Testo con spaziatura tra le lettere, centrato su xy[0]."""
    widths = [draw.textlength(ch, font=font) for ch in text]
    total = sum(widths) + spacing * (len(text) - 1)
    x = xy[0] - total / 2
    for ch, w in zip(text, widths):
        draw.text((x, xy[1]), ch, font=font, fill=fill)
        x += w + spacing


def as_splash(grid):
    from PIL import Image, ImageDraw, ImageFont

    def rgb(c):
        return ((c >> 16) & 255, (c >> 8) & 255, c & 255)

    img = Image.new("RGB", (SPLASH_W, SPLASH_H), rgb(SPLASH_BG))
    draw = ImageDraw.Draw(img)
    size = SIZE * SPLASH_LOGO_SCALE
    x0 = (SPLASH_W - size) // 2
    y0 = 48
    for y, row in enumerate(grid):
        for x, p in enumerate(row):
            if p is not None:
                draw.rectangle([x0 + x * SPLASH_LOGO_SCALE, y0 + y * SPLASH_LOGO_SCALE,
                                x0 + (x + 1) * SPLASH_LOGO_SCALE - 1, y0 + (y + 1) * SPLASH_LOGO_SCALE - 1], fill=rgb(p))
    name = ImageFont.truetype(FONT_BOLD, 56)
    tag = ImageFont.truetype(FONT, 18)
    line = ImageFont.truetype(FONT, 14)
    y = y0 + size + 36
    draw.text((SPLASH_W / 2, y), SPLASH_NAME, font=name, fill=rgb(SPLASH_NAME_COLOR), anchor="mt")
    y += 56 + 22
    spaced(draw, (SPLASH_W / 2, y), SPLASH_TAGLINE, tag, rgb(SPLASH_TAGLINE_COLOR), 4)
    y += 18 + 18
    spaced(draw, (SPLASH_W / 2, y), SPLASH_LINE, line, rgb(SPLASH_LINE_COLOR), 2)
    return img


def main(argv):
    if len(argv) < 2 or argv[1] not in ("c", "png", "splash"):
        sys.exit(__doc__)
    if argv[1] == "splash":
        as_splash(pixels(open("assets/ourobios.svg").read())).save(argv[2], "BMP")
    elif argv[1] == "c":
        svg = argv[2] if len(argv) > 2 else "assets/ourobios.svg"
        sys.stdout.write(as_c(pixels(open(svg).read())))
    else:
        out = argv[2]
        scale = int(argv[3]) if len(argv) > 3 else 32
        bg = int(argv[4], 16) if len(argv) > 4 else None
        open(out, "wb").write(as_png(pixels(open("assets/ourobios.svg").read()), scale, bg))


if __name__ == "__main__":
    main(sys.argv)
