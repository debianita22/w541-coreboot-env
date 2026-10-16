#!/usr/bin/env python3
# ui-font.py - i font della schermata di aggiornamento del firmware
# (UpdateScreenLib di patches/edk2): dai font di LVGL nel submodule LvglPkg
# di EDK2, gli stessi del setup, a un sorgente C compatto con solo i glifi
# che servono.
#
#   tools/ui-font.py LVGL_FONT_DIR OUT.c
#
# LVGL_FONT_DIR e' LvglPkg/Library/LvglLib/lvgl/src/font dell'albero di
# EDK2: i suoi lv_font_montserrat_<N>.c sono Montserrat Medium (SIL Open
# Font License 1.1) con le icone di Font Awesome 5 Free (icone CC BY 4.0),
# convertiti da lv_font_conv a 4 bit per pixel, senza compressione. Qui si
# prendono, per ogni dimensione, i glifi che la schermata disegna con quel
# font (glyph_sets): bitmap (4 bit per pixel, nibble alto prima, righe una
# dopo l'altra), avanzamento e riquadro, e la crenatura per classi, ristretta
# alle classi di quei glifi; i glifi in ordine di codice, per la ricerca
# binaria della schermata. OUT.c va accanto a UpdateScreenLib.c e
# UpdateScreenDraw.c, da cui si leggono i titoli e si controllano la riga
# delle versioni e l'uso dei font. Prepare (tools/build.sh) scrive OUT.c
# nell'albero di EDK2 dopo le patch; l'uscita dipende solo dai file di LVGL,
# dai sorgenti della schermata e da questo script.
import os
import re
import sys

# Le icone di Font Awesome che la schermata usa (UpdateScreenFont.h):
# avviso, spunta e croce nel piede, la freccia tra le versioni
ICON_WARNING, ICON_CHECK, ICON_CLOSE, ICON_NEXT = 0xF071, 0xF00C, 0xF00D, 0xF054
BULLET = 0x2022
# Nella riga delle versioni ("v1.3.6-rc3 • slot B › v1.3.6 • slot A") le
# etichette di FormatTag, il punto elenco, "slot", la lettera dello slot e
# la freccia
VERSIONS = "0123456789.-crv slotAB"


class Error(Exception):
    pass


def strings(text):
    """Le stringhe L"..." di text."""
    return re.findall(r'L"([^"\\]*)"', text)


def literals(text):
    """Il testo delle stringhe L"..." di text, senza le conversioni di
    PrintLib (%s, %c, %u, %-8Lu...)."""
    return [re.sub(r"%[-+ 0#,]*[0-9]*(?:\.[0-9]+)?[lL]*[a-zA-Z]", "", s) for s in strings(text)]


def statements(src, pattern):
    """Le istruzioni C di src (fino al ';') in cui c'e' pattern."""
    return [s for s in src.split(";") if re.search(pattern, s)]


def glyph_sets(screen_dir):
    """I codici di ogni dimensione, '?' compreso (lo disegna per un glifo che
    il font non ha): 16 px tutto il resto (stato, legenda, tempo, piede, le
    righe del boot manager, anche il testo di uno stato EFI), 20 px la riga
    delle versioni, 32 px i titoli. Controlla i sorgenti della schermata in
    screen_dir: i font di 20 e 32 px disegnano solo quelle due righe, e le
    stringhe che le compongono hanno solo i glifi che hanno."""
    src = {}
    for name in ("UpdateScreenLib.c", "UpdateScreenDraw.c"):
        path = os.path.join(screen_dir, name)
        try:
            src[name] = open(path, encoding="utf-8").read()
        except OSError as e:
            raise Error("%s: %s (OUT.c va accanto ai sorgenti della schermata)" % (path, e.strerror))
    for font, line in (("&gUpdateScreenFont32", "Model->Title"), ("&gUpdateScreenFont20", "Model->Versions")):
        uses = statements(src["UpdateScreenDraw.c"], re.escape(font))
        if not uses or any(line not in s for s in uses):
            raise Error("UpdateScreenDraw.c: %s non disegna solo %s" % (font[1:], line))
    lib = src["UpdateScreenLib.c"]
    titles = [t for s in statements(lib, r"\bmModel\.Title\b") for t in strings(s)]
    if not titles or any("%" in t for t in titles):
        raise Error("UpdateScreenLib.c: titoli non trovati, o con conversioni %")
    # la riga delle versioni: le sue stringhe e quelle di FormatTag non
    # devono avere glifi fuori da VERSIONS
    versions = [t for s in statements(lib, r"\bmModel\.Versions\b") for t in literals(s)]
    tag = re.search(r"^FormatTag \((.*?)^\}", lib, re.M | re.S)
    if not versions or not tag:
        raise Error("UpdateScreenLib.c: riga delle versioni o FormatTag non trovati")
    extra = set("".join(versions + literals(tag.group(1)))) - set(VERSIONS)
    if extra:
        raise Error("UpdateScreenLib.c: riga delle versioni con %s, che il font di 20 px non ha"
                    % " ".join(sorted(repr(c) for c in extra)))
    ascii_ = set(range(0x20, 0x7F))
    return {
        16: sorted(ascii_ | {ICON_WARNING, ICON_CHECK, ICON_CLOSE}),
        20: sorted({ord(c) for c in "?" + VERSIONS} | {BULLET, ICON_NEXT}),
        32: sorted({ord(c) for c in "?" + "".join(titles)}),
    }


def array(src, name, signed=False):
    m = re.search(r"\b%s\[\]\s*=\s*\{(.*?)\};" % re.escape(name), src, re.S)
    if not m:
        raise Error("%s non trovato" % name)
    body = re.sub(r"/\*.*?\*/", "", m.group(1), flags=re.S)
    vals = [int(v, 0) for v in re.findall(r"-?0x[0-9a-fA-F]+|-?\d+", body)]
    return vals


def field(src, name):
    m = re.search(r"\.%s\s*=\s*(-?\d+)" % re.escape(name), src)
    if not m:
        raise Error(".%s non trovato" % name)
    return int(m.group(1))


def load(path):
    src = open(path, encoding="utf-8").read()
    if field(src, "bpp") != 4 or field(src, "bitmap_format") != 0:
        raise Error("%s: atteso 4 bit per pixel senza compressione" % path)
    bitmap = array(src, "glyph_bitmap")
    dsc = []
    for m in re.finditer(r"\{\.bitmap_index = (\d+), \.adv_w = (\d+), \.box_w = (\d+), \.box_h = (\d+), "
                         r"\.ofs_x = (-?\d+), \.ofs_y = (-?\d+)\}", src):
        dsc.append(tuple(int(x) for x in m.groups()))
    cmap = {}
    for m in re.finditer(r"\.range_start = (\d+), \.range_length = (\d+), \.glyph_id_start = (\d+),\s*"
                         r"\.unicode_list = (\w+), \.glyph_id_ofs_list = (\w+), \.list_length = (\d+), "
                         r"\.type = (\w+)", src):
        start, length, gid0 = int(m[1]), int(m[2]), int(m[3])
        ulist, olist, n, kind = m[4], m[5], int(m[6]), m[7]
        if kind == "LV_FONT_FMT_TXT_CMAP_FORMAT0_TINY":
            for i in range(length):
                cmap[start + i] = gid0 + i
        elif kind == "LV_FONT_FMT_TXT_CMAP_SPARSE_TINY":
            offs = array(src, ulist)
            if len(offs) != n:
                raise Error("%s: %s di %d voci, attese %d" % (path, ulist, len(offs), n))
            for i, off in enumerate(offs):
                cmap[start + off] = gid0 + i
        else:
            raise Error("%s: cmap %s non gestita" % (path, kind))
    left = array(src, "kern_left_class_mapping")
    right = array(src, "kern_right_class_mapping")
    values = array(src, "kern_class_values", signed=True)
    lcnt, rcnt = field(src, "left_class_cnt"), field(src, "right_class_cnt")
    if len(values) != lcnt * rcnt:
        raise Error("%s: %d valori di crenatura, attesi %d" % (path, len(values), lcnt * rcnt))
    return {
        "bitmap": bitmap, "dsc": dsc, "cmap": cmap, "left": left, "right": right,
        "values": values, "rcnt": rcnt, "scale": field(src, "kern_scale"),
        "line_height": field(src, "line_height"), "base_line": field(src, "base_line"),
    }


def subset(font, size, codepoints):
    glyphs, bitmap = [], []
    lmap, rmap = {}, {}
    for cp in codepoints:
        gid = font["cmap"].get(cp)
        if gid is None:
            raise Error("Montserrat %d: manca U+%04X" % (size, cp))
        index, adv, w, h, ox, oy = font["dsc"][gid]
        nbytes = (w * h * 4 + 7) // 8
        data = font["bitmap"][index:index + nbytes]
        if len(data) != nbytes:
            raise Error("Montserrat %d: bitmap di U+%04X troncata" % (size, cp))
        lc, rc = font["left"][gid], font["right"][gid]
        if lc and lc not in lmap:
            lmap[lc] = len(lmap) + 1
        if rc and rc not in rmap:
            rmap[rc] = len(rmap) + 1
        glyphs.append((cp, adv, len(bitmap), w, h, ox, oy, lmap.get(lc, 0), rmap.get(rc, 0)))
        bitmap.extend(data)
    kern = [0] * (len(lmap) * len(rmap))
    for lc, nl in lmap.items():
        for rc, nr in rmap.items():
            v = font["values"][(lc - 1) * font["rcnt"] + (rc - 1)]
            kern[(nl - 1) * len(rmap) + (nr - 1)] = (v * font["scale"]) >> 4
    for v in kern:
        if not -128 <= v <= 127:
            raise Error("Montserrat %d: crenatura %d fuori da INT8" % (size, v))
    for g in glyphs:
        if not (0 <= g[1] < 65536 and 0 <= g[3] < 256 and 0 <= g[4] < 256 and -128 <= g[5] < 128 and -128 <= g[6] < 128):
            raise Error("Montserrat %d: U+%04X fuori dai limiti del formato" % (size, g[0]))
    return glyphs, bitmap, len(lmap), len(rmap), kern


def c_bytes(vals, fmt, per_line=16):
    lines = []
    for i in range(0, len(vals), per_line):
        lines.append("  " + ", ".join(fmt % v for v in vals[i:i + per_line]) + ",")
    return "\n".join(lines) if lines else "  0"


def generate(font_dir, sets):
    out = ["""/** @file
  Fonts of the firmware update screen: Montserrat Medium (Copyright 2011 The
  Montserrat Project Authors, SIL Open Font License 1.1) and icons of Font
  Awesome 5 Free (Fonticons, Inc., icons CC BY 4.0, font SIL OFL 1.1), the
  glyphs the screen draws with each size, as LVGL converted them for LvglPkg
  (lvgl/src/font/lv_font_montserrat_<size>.c): 4 bits per pixel, high nibble
  first, rows one after the other; advances and kerning in 1/16 px.

  Generated by tools/ui-font.py of ouroBIOS from those files and the strings
  of UpdateScreenLib.c: do not edit.
**/

#include "UpdateScreenFont.h"
"""]
    for size in sorted(sets):
        font = load("%s/lv_font_montserrat_%d.c" % (font_dir, size))
        glyphs, bitmap, lcnt, rcnt, kern = subset(font, size, sets[size])
        out.append("STATIC CONST UINT8  mBitmap%d[] = {\n%s\n};\n" % (size, c_bytes(bitmap, "0x%02x")))
        rows = ["  { 0x%04x, %4d, %6d, %3d, %3d, %3d, %3d, %3d, %3d }," % g for g in glyphs]
        out.append("STATIC CONST UPDATE_SCREEN_GLYPH  mGlyphs%d[] = {\n%s\n};\n" % (size, "\n".join(rows)))
        out.append("STATIC CONST INT8  mKern%d[] = {\n%s\n};\n" % (size, c_bytes(kern, "%d", 20)))
        out.append("""CONST UPDATE_SCREEN_FONT  gUpdateScreenFont%d = {
  %d,
  %d,
  %d,
  mGlyphs%d,
  mBitmap%d,
  %d,
  %d,
  mKern%d
};
""" % (size, font["line_height"], font["base_line"], len(glyphs), size, size, lcnt, rcnt, size))
    return "\n".join(out)


def main():
    if len(sys.argv) != 3:
        sys.exit("uso: ui-font.py LVGL_FONT_DIR OUT.c")
    out = sys.argv[2]
    try:
        sets = glyph_sets(os.path.dirname(os.path.abspath(out)))
        text = generate(sys.argv[1], sets)
    except (Error, OSError) as e:
        sys.exit("ui-font.py: %s" % e)
    # i sorgenti di EDK2 sono CRLF
    with open(out, "w", encoding="ascii", newline="\r\n") as f:
        f.write(text)


if __name__ == "__main__":
    main()
