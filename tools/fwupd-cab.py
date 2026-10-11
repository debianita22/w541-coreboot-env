#!/usr/bin/env python3
# fwupd-cab.py - il pacchetto fwupd (.cab) di una capsula (docs/update.md)
#
#   tools/fwupd-cab.py metainfo --variant mrc --guid GUID --version 1.2.3 \
#       --date 2026-10-11 --url URL > firmware.metainfo.xml
#   tools/fwupd-cab.py pack --date 2026-10-11 -o OUT.cab \
#       firmware.bin firmware.metainfo.xml [firmware.jcat]
#
# Un cabinet Microsoft non compresso con:
#   firmware.bin           la capsula, cosi' com'e' (fwupd non tocca una
#                          capsula FMP: comincia con il GUID FMP)
#   firmware.metainfo.xml  il componente AppStream: il GUID dell'ESRT della
#                          variante, la versione nel formato "triplet" (quello
#                          dell'ESRT, X << 24 | Y << 16 | Z), il protocollo
#                          org.uefi.capsule
#   firmware.jcat          SHA-256 e firma PKCS#7 dei due file sopra, fatte da
#                          jcat-tool con la chiave delle capsule (tools/build.sh):
#                          fwupd installa solo pacchetti firmati da un
#                          certificato in /etc/pki/fwupd (OnlyTrusted=true, il
#                          default), e controlla anche il metainfo
#
# Solo la libreria standard di Python. Il formato: [MS-CAB], un folder senza
# compressione, blocchi CFDATA da 32 KiB con il checksum.
import argparse
import datetime
import os
import re
import struct
import sys
from xml.sax.saxutils import escape

# l'inizializzazione della RAM di ogni variante, per la descrizione
VARIANTS = {"mrc": "Intel mrc.bin", "nri": "native RAM init"}
REPO = "https://github.com/debianita22/w541-coreboot-env"
CHUNK = 32768


def cab_checksum(data, seed=0):
    """CSUMCompute di [MS-CAB] 2.6: XOR delle parole a 32 bit little endian;
    i byte che avanzano entrano nell'ordine inverso (il primo piu' in alto)."""
    csum = seed
    n = len(data) // 4
    for (w,) in struct.iter_unpack("<I", data[: n * 4]):
        csum ^= w
    tail = data[n * 4:]
    ul = 0
    for b in tail:
        ul = (ul << 8) | b
    return csum ^ ul


def dos_datetime(d):
    date = ((d.year - 1980) << 9) | (d.month << 5) | d.day
    time = (d.hour << 11) | (d.minute << 5) | (d.second // 2)
    return date, time


def make_cab(files, when):
    """files: [(nome, bytes)], tutti in un folder non compresso."""
    date, time = dos_datetime(when)
    folder = b"".join(data for _, data in files)
    chunks = [folder[i:i + CHUNK] for i in range(0, len(folder), CHUNK)] or [b""]
    entries = b""
    off = 0
    for name, data in files:
        entries += struct.pack("<IIHHHH", len(data), off, 0, date, time, 0x20)
        entries += name.encode("ascii") + b"\0"
        off += len(data)
    hdr_size = 36
    folder_size = 8
    coff_files = hdr_size + folder_size
    coff_data = coff_files + len(entries)
    blocks = b""
    for c in chunks:
        sizes = struct.pack("<HH", len(c), len(c))
        csum = cab_checksum(sizes, cab_checksum(c))
        blocks += struct.pack("<I", csum) + sizes + c
    total = coff_data + len(blocks)
    header = struct.pack("<4sIIIIIBBHHHHH", b"MSCF", 0, total, 0, coff_files, 0,
                         3, 1, 1, len(files), 0, 0x1234, 0)
    cfolder = struct.pack("<IHH", coff_data, len(chunks), 0)
    out = header + cfolder + entries + blocks
    assert len(out) == total
    return out


def metainfo(variant, guid, version, date, url):
    vid = "io.github.debianita22.w541coreboot.%s.firmware" % variant
    return """<?xml version="1.0" encoding="UTF-8"?>
<!-- Copyright 2026 w541-coreboot-env contributors -->
<component type="firmware">
  <id>{vid}</id>
  <name>ThinkPad W541 coreboot ({variant})</name>
  <name_variant_suffix>{variant}</name_variant_suffix>
  <summary>coreboot and EDK2 system firmware for the ThinkPad W541</summary>
  <description>
    <p>
      w541-coreboot-env system firmware, {variant} variant (memory
      initialisation: {descen}). The update rewrites the coreboot region of
      the 4 MiB flash chip and clears the memory training cache; UEFI
      settings, VPD and the event log are kept.
    </p>
  </description>
  <provides>
    <firmware type="flashed">{guid}</firmware>
  </provides>
  <url type="homepage">{repo}</url>
  <metadata_license>CC0-1.0</metadata_license>
  <project_license>GPL-2.0-only</project_license>
  <developer_name>w541-coreboot-env</developer_name>
  <categories>
    <category>X-System</category>
  </categories>
  <custom>
    <value key="LVFS::VersionFormat">triplet</value>
    <value key="LVFS::UpdateProtocol">org.uefi.capsule</value>
  </custom>
  <requires>
    <id compare="ge" version="1.7.0">org.freedesktop.fwupd</id>
  </requires>
  <releases>
    <release urgency="medium" version="{version}" date="{date}" install_duration="120">
      <checksum filename="firmware.bin" target="content"/>
      <url type="details">{url}</url>
      <description>
        <p>Release notes: {url}</p>
      </description>
    </release>
  </releases>
</component>
""".format(vid=vid, variant=variant, descen=VARIANTS[variant],
           guid=guid, repo=REPO, version=version, date=date, url=escape(url))


def main():
    ap = argparse.ArgumentParser(description="pacchetto fwupd (.cab) di una capsula")
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("metainfo", help="firmware.metainfo.xml su stdout")
    m.add_argument("--variant", required=True, choices=sorted(VARIANTS))
    m.add_argument("--guid", required=True)
    m.add_argument("--version", required=True, help="X.Y.Z")
    m.add_argument("--date", required=True, help="AAAA-MM-GG")
    m.add_argument("--url", required=True, help="la pagina della release")
    p = sub.add_parser("pack", help="il .cab da firmware.bin, firmware.metainfo.xml [, firmware.jcat]")
    p.add_argument("--date", required=True, help="AAAA-MM-GG, la data dei file nel cabinet")
    p.add_argument("-o", "--output", required=True)
    p.add_argument("files", nargs="+")
    a = ap.parse_args()

    if a.cmd == "metainfo":
        if not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", a.guid):
            sys.exit("GUID non valido: %s" % a.guid)
        if not re.fullmatch(r"\d{1,3}\.\d{1,3}\.\d{1,5}", a.version):
            sys.exit("versione non valida (X.Y.Z): %s" % a.version)
        datetime.datetime.strptime(a.date, "%Y-%m-%d")
        sys.stdout.write(metainfo(a.variant, a.guid, a.version, a.date, a.url))
        return

    when = datetime.datetime.strptime(a.date, "%Y-%m-%d")
    names = [os.path.basename(f) for f in a.files]
    want = ["firmware.bin", "firmware.metainfo.xml"]
    if names[:2] != want or names[2:] not in ([], ["firmware.jcat"]):
        sys.exit("file: firmware.bin firmware.metainfo.xml [firmware.jcat], in quest'ordine")
    data = []
    for f in a.files:
        with open(f, "rb") as fh:
            data.append(fh.read())
    # una capsula FMP: fwupd la scrive com'e' solo se comincia con questo GUID
    if data[0][:16] != bytes.fromhex("edd5cb6d2de8444cbda17194199ad92a"):
        sys.exit("%s: non e' una capsula FMP" % a.files[0])
    with open(a.output, "wb") as f:
        f.write(make_cab(list(zip(names, data)), when))


if __name__ == "__main__":
    main()
