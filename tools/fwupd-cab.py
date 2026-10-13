#!/usr/bin/env python3
# fwupd-cab.py - il pacchetto fwupd (.cab) di una capsula, e i metadati del
# remote fwupd del progetto (docs/update.md)
#
#   tools/fwupd-cab.py metainfo --variant mrc --guid GUID --version 1.2.3 \
#       --date 2026-10-11 --url URL > firmware.metainfo.xml
#   tools/fwupd-cab.py pack --date 2026-10-11 -o OUT.cab \
#       firmware.bin firmware.metainfo.xml [firmware.jcat]
#   tools/fwupd-cab.py metadata -o ourobios.xml.gz --cab CAB URL [--cab CAB URL]
#   tools/fwupd-cab.py check-metadata --cert CERT ourobios.xml.gz [CAB...]
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
# I metadati del remote (configs/fwupd/ourobios.conf): ourobios.xml.gz, i
# componenti AppStream dei .cab di una release (il metainfo di ciascuno, con
# l'indirizzo del .cab, i suoi checksum e le dimensioni), e accanto
# ourobios.xml.gz.jcat, SHA-256 e firma PKCS#7 fatte da jcat-tool con la
# chiave delle capsule (tools/build.sh): fwupd li carica solo con un
# certificato in /etc/pki/fwupd-metadata. Li pubblica la release mrc, e il
# remote li scarica da releases/latest/download/.
#
# Solo la libreria standard di Python e tools/update.py, che legge e
# controlla i .cab. Il formato: [MS-CAB], un folder senza compressione,
# blocchi CFDATA da 32 KiB con il checksum.
import argparse
import datetime
import gzip
import hashlib
import os
import re
import struct
import sys
import urllib.parse
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import update  # noqa: E402

# l'inizializzazione della RAM di ogni variante, per la descrizione
VARIANTS = {"mrc": "Intel mrc.bin", "nri": "native RAM init"}
REPO = "https://github.com/debianita22/ourobios"
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
    vid = "io.github.debianita22.ourobios.%s.firmware" % variant
    return """<?xml version="1.0" encoding="UTF-8"?>
<!-- Copyright 2026 ouroBIOS contributors -->
<component type="firmware">
  <id>{vid}</id>
  <name>ouroBIOS ({variant})</name>
  <name_variant_suffix>{variant}</name_variant_suffix>
  <summary>ouroBIOS: coreboot and EDK II system firmware for the ThinkPad W541</summary>
  <description>
    <p>
      ouroBIOS, the coreboot firmware of the ThinkPad W541, {variant} variant (memory
      initialisation: {descen}), verified by vboot. The update writes the RW
      slot that is not running, and the firmware tries it on the next boot:
      if the write is cut short or the new slot fails, the old one boots.
      The read-only region, UEFI settings, VPD and the event log are kept.
    </p>
  </description>
  <provides>
    <firmware type="flashed">{guid}</firmware>
  </provides>
  <url type="homepage">{repo}</url>
  <metadata_license>CC0-1.0</metadata_license>
  <project_license>GPL-2.0-only</project_license>
  <developer_name>ouroBIOS</developer_name>
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


def read_file(path):
    with open(path, "rb") as f:
        return f.read()


def load_cab(path, data):
    try:
        return update.load_cab(path, data)
    except update.Error as e:
        raise update.Error("%s: %s" % (path, e))


def url_name(url):
    return os.path.basename(urllib.parse.urlparse(url).path)


def release_component(path, url):
    """Il componente del .cab path nei metadati, scaricato da url."""
    data = read_file(path)
    name = os.path.basename(path)
    if not url.startswith("https://") or url_name(url) != name:
        raise update.Error("%s: l'indirizzo %s non e' https o non finisce con %s" % (path, url, name))
    # il .cab come lo controlla update.py info: capsula e metainfo d'accordo
    capsule, cap = load_cab(path, data)
    files = cap["cab"]["files"]
    if "firmware.jcat" not in files:
        raise update.Error("%s: senza firmware.jcat fwupd non lo installa" % path)
    comp = ET.fromstring(files["firmware.metainfo.xml"])
    rels = comp.findall("releases/release")
    if comp.tag != "component" or comp.get("type") != "firmware" or len(rels) != 1:
        raise update.Error("%s: il metainfo non e' un componente firmware con una release" % path)
    rel = rels[0]
    # il checksum vuoto del metainfo lascia il posto a quelli veri
    for c in rel.findall("checksum"):
        rel.remove(c)
    try:
        day = datetime.datetime.strptime(rel.get("date", ""), "%Y-%m-%d")
    except ValueError:
        raise update.Error("%s: release senza data AAAA-MM-GG" % path)
    rel.set("timestamp", str(int(day.replace(tzinfo=datetime.timezone.utc).timestamp())))
    loc = ET.Element("location")
    loc.text = url
    rel.insert(0, loc)
    for target, fname, blob in (("container", name, data), ("content", "firmware.bin", capsule)):
        for kind in ("sha1", "sha256"):
            c = ET.SubElement(rel, "checksum", type=kind, filename=fname, target=target)
            c.text = hashlib.new(kind, blob).hexdigest()
    ET.SubElement(rel, "size", type="download").text = str(len(data))
    ET.SubElement(rel, "size", type="installed").text = str(len(capsule))
    return comp


def make_metadata(cabs):
    """ourobios.xml.gz dai .cab: [(file, url)]."""
    root = ET.Element("components", origin="ourobios", version="0.9")
    seen = set()
    for comp in sorted((release_component(p, u) for p, u in cabs), key=lambda c: c.findtext("id")):
        cid = comp.findtext("id")
        if cid in seen:
            raise update.Error("due .cab per %s" % cid)
        seen.add(cid)
        root.append(comp)
    ET.indent(root)
    xml = ET.tostring(root, encoding="utf-8", xml_declaration=True) + b"\n"
    # mtime=0: lo stesso file dagli stessi .cab
    return gzip.compress(xml, mtime=0)


def check_metadata(path, cert, cabs):
    """Firma, formato e .cab di ourobios.xml.gz, come li usa fwupd."""
    data = read_file(path)
    name = os.path.basename(path)
    try:
        jcat = read_file(path + ".jcat")
    except OSError as e:
        raise update.Error("%s.jcat: %s" % (path, e.strerror))
    update.verify_jcat(jcat, {name: data}, cert, label=name + ".jcat")
    print("firma:     %s.jcat valido per %s" % (name, cert))
    try:
        root = ET.fromstring(gzip.decompress(data))
    except (OSError, ET.ParseError) as e:
        raise update.Error("%s: non e' XML compresso con gzip: %s" % (path, e))
    if root.tag != "components":
        raise update.Error("%s: manca <components>" % path)
    rels = {}
    for comp in root.findall("component"):
        guids = [e.text for e in comp.iter("firmware") if e.get("type") == "flashed"]
        for rel in comp.findall("releases/release"):
            loc = rel.findtext("location") or ""
            sums = {c.get("type"): c.text for c in rel.findall("checksum") if c.get("target") == "container"}
            if not loc.startswith("https://") or set(sums) != {"sha1", "sha256"} or len(guids) != 1:
                raise update.Error("%s: release %s senza indirizzo https, checksum o GUID"
                                   % (path, rel.get("version")))
            if url_name(loc) in rels:
                raise update.Error("%s: %s due volte" % (path, url_name(loc)))
            rels[url_name(loc)] = (comp.findtext("id"), guids[0], rel.get("version"), sums, loc)
    if not rels:
        raise update.Error("%s: nessuna release" % path)
    for cab in cabs:
        n = os.path.basename(cab)
        if n not in rels:
            raise update.Error("%s: %s non c'e'" % (path, n))
        cid, guid, version, sums, loc = rels.pop(n)
        d = read_file(cab)
        if sums != {"sha1": hashlib.sha1(d).hexdigest(), "sha256": hashlib.sha256(d).hexdigest()}:
            raise update.Error("%s: checksum di %s diversi dal file" % (path, n))
        meta = load_cab(cab, d)[1]["cab"]["meta"]
        if (meta["id"], str(meta["guid"]), meta["version"]) != (cid, guid, version):
            raise update.Error("%s: %s %s %s, il metainfo di %s dice %s %s %s"
                               % (path, cid, guid, version, n, meta["id"], meta["guid"], meta["version"]))
        print("release:   %s %s (%s), %s" % (cid, version, guid, loc))
    if cabs and rels:
        raise update.Error("%s: anche %s, che non e' tra i .cab" % (path, ", ".join(sorted(rels))))
    for n, (cid, guid, version, _, loc) in sorted(rels.items()):
        print("release:   %s %s (%s), %s, .cab non controllato" % (cid, version, guid, loc))


def main():
    ap = argparse.ArgumentParser(description="pacchetto fwupd (.cab) di una capsula e metadati del remote")
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
    md = sub.add_parser("metadata", help="i metadati del remote (.xml.gz) dai .cab di una release")
    md.add_argument("-o", "--output", required=True)
    md.add_argument("--cab", nargs=2, action="append", required=True, metavar=("CAB", "URL"),
                    help="un .cab e l'indirizzo da cui fwupd lo scarica")
    cm = sub.add_parser("check-metadata", help="firma (FILE.jcat), formato e .cab dei metadati")
    cm.add_argument("--cert", required=True, help="il certificato della firma (keys/capsule-signing.pem)")
    cm.add_argument("metadata")
    cm.add_argument("cabs", nargs="*", help="i .cab della release: checksum e metainfo")
    a = ap.parse_args()

    if a.cmd in ("metadata", "check-metadata"):
        try:
            if a.cmd == "metadata":
                data = make_metadata(a.cab)
                with open(a.output, "wb") as f:
                    f.write(data)
            else:
                check_metadata(a.metadata, a.cert, a.cabs)
        except update.Error as e:
            sys.exit("errore: %s" % e)
        except OSError as e:
            sys.exit("errore: %s: %s" % (e.filename, e.strerror))
        return

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
