#!/usr/bin/env python3
# vpd.py - la VPD (Vital Product Data) di coreboot nella regione RO_VPD del
# flash: i dati di questa macchina che coreboot mette nelle tabelle SMBIOS
# (numero di serie, Machine Type Model, UUID), che il BIOS Lenovo aveva nel
# suo flash e che coreboot non conosce. Le immagini delle release hanno la
# regione vuota: la si scrive una volta sul portatile e la si copia
# nell'immagine nuova a ogni aggiornamento (docs/flashing.md).
#
#   python3 tools/vpd.py show dump.rom
#   python3 tools/vpd.py set dump.rom -o vpd.rom --serial R9-0XXXXX \
#           --mtm 20EF-001UGE --uuid serial
#   python3 tools/vpd.py copy dump.rom w541-coreboot-vX.Y.Z-mrc.rom -o w541-flash.rom
#
# I file sono immagini o dump interi (12 MiB, con la FMAP) oppure la sola
# regione RO_VPD (16 KiB). Le chiavi che coreboot legge (CONFIG_SMBIOS_*_
# FROM_VPD e patch 0046):
#
#   serial_number        tipo 1 e tipo 3: S/N dell'etichetta, es. PC0XXXXX
#   mlb_serial_number    tipo 2: numero di serie della scheda madre
#   system_uuid          tipo 1: UUID, 16 byte nell'ordine SMBIOS
#   system_product_name  tipo 1 e tipo 2: Machine Type Model, es. 20EF001UGE
#   system_sku           tipo 1: LENOVO_MT_20EF
#   system_family        tipo 1: ThinkPad W541
#
# Formato: Google VPD 2.0 (src/drivers/vpd di coreboot). Un'intestazione
# "gVpdInfo" con la lunghezza dei dati, poi coppie chiave/valore precedute
# dal tipo 0x01, con le lunghezze in gruppi di 7 bit (il bit 7 dice che ne
# segue un altro), e 0x00 in fondo. coreboot cerca l'intestazione
# all'inizio della regione, poi a 0x600 (il formato del vecchio strumento
# vpd di ChromeOS, con l'entry point SMBIOS davanti): qui si legge in
# entrambi i posti e si scrive all'inizio.
import argparse
import os
import re
import struct
import sys
import uuid

REGION = "RO_VPD"
FMAP_SIG = b"__FMAP__"
INFO_MAGIC = b"\xfe\x09\x01gVpdInfo\x04"
INFO_LEN = len(INFO_MAGIC) + 4
INFO_OFFSETS = (0, 0x600)
TYPE_TERMINATOR = 0x00
TYPE_STRING = 0x01
TYPE_INFO = 0xFE
TYPE_IMPLICIT_TERMINATOR = 0xFF

# coreboot copia al massimo 63 caratteri (CONFIG_SMBIOS_SERIAL_SIZE e
# CONFIG_SMBIOS_SYSTEM_DATA_SIZE, 64 con lo zero finale)
MAX_STRING = 63

# Machine type -> famiglia, come le scrive il BIOS Lenovo
FAMILIES = {"20EF": "ThinkPad W541", "20EG": "ThinkPad W541"}

# --uuid serial: un UUID ricavato da MTM e numero di serie (versione 5, con
# questo namespace fisso), uguale ogni volta che si riscrive la VPD
UUID_NAMESPACE = uuid.UUID("5a8f1c0e-3b7d-4e62-9a41-7c0d2e9b6f13")


def die(msg):
    print("vpd.py: " + msg, file=sys.stderr)
    sys.exit(1)


def find_fmap(data):
    """Le aree della FMAP {nome: (offset, dimensione)}, o None. La firma
    compare anche come stringa nel codice di coreboot: vale la prima
    intestazione coerente."""
    pos = data.find(FMAP_SIG)
    while pos >= 0:
        if pos + 56 <= len(data):
            major = data[pos + 8]
            flash_size = struct.unpack_from("<I", data, pos + 18)[0]
            nareas = struct.unpack_from("<H", data, pos + 54)[0]
            end = pos + 56 + nareas * 42
            if major == 1 and 0 < nareas <= 128 and end <= len(data):
                areas = {}
                ok = True
                for i in range(nareas):
                    off, size, name, _ = struct.unpack_from("<II32sH", data, pos + 56 + i * 42)
                    name = name.split(b"\0")[0]
                    if off + size > flash_size or not re.fullmatch(rb"[A-Z0-9_]+", name):
                        ok = False
                        break
                    areas[name.decode()] = (off, size)
                if ok and flash_size == len(data):
                    return areas
        pos = data.find(FMAP_SIG, pos + 1)
    return None


def locate(data, path):
    """(offset, dimensione) della regione RO_VPD nel file."""
    areas = find_fmap(data)
    if areas is None:
        if len(data) == 0x4000:
            return 0, len(data)
        die("%s: nessuna FMAP valida; serve l'immagine intera da 12 MiB "
            "(sudo flashrom -p internal -r dump.rom) o la sola regione %s"
            % (path, REGION))
    if REGION not in areas:
        die("%s: la FMAP non ha la regione %s" % (path, REGION))
    return areas[REGION]


def dec_len(buf, p):
    n = 0
    while True:
        if p >= len(buf):
            raise ValueError("lunghezza oltre la fine dei dati")
        b = buf[p]
        p += 1
        n = (n << 7) | (b & 0x7F)
        if not b & 0x80:
            return n, p


def enc_len(n):
    out = [n & 0x7F]
    n >>= 7
    while n:
        out.append(0x80 | (n & 0x7F))
        n >>= 7
    return bytes(reversed(out))


def decode(body):
    """Le coppie (chiave, valore) dei dati dopo l'intestazione, come le
    legge vpd_decode_string() di coreboot."""
    entries = []
    p = 0
    while p < len(body):
        etype = body[p]
        if etype in (TYPE_TERMINATOR, TYPE_IMPLICIT_TERMINATOR):
            break
        if etype not in (TYPE_STRING, TYPE_INFO):
            raise ValueError("tipo 0x%02x sconosciuto all'offset %d" % (etype, p))
        p += 1
        klen, p = dec_len(body, p)
        key = body[p:p + klen]
        p += klen
        vlen, p = dec_len(body, p)
        value = body[p:p + vlen]
        p += vlen
        if p > len(body):
            raise ValueError("coppia oltre la fine dei dati")
        if etype == TYPE_STRING:
            entries.append((key.decode("ascii"), value))
    return entries


def parse_region(region):
    """(entries, offset dell'intestazione) di una regione RO_VPD; ([], None)
    se e' vuota."""
    for base in INFO_OFFSETS:
        if region[base:base + len(INFO_MAGIC)] == INFO_MAGIC:
            size = struct.unpack_from("<I", region, base + len(INFO_MAGIC))[0]
            start = base + INFO_LEN
            if start + size > len(region):
                raise ValueError("la lunghezza dei dati (%d) supera la regione" % size)
            return decode(region[start:start + size]), base
    if region.count(0xFF) == len(region):
        return [], None
    raise ValueError("la regione non e' vuota ma non ha l'intestazione gVpdInfo")


def encode_region(entries, size):
    body = b"".join(bytes([TYPE_STRING]) + enc_len(len(k)) + k.encode("ascii")
                    + enc_len(len(v)) + v for k, v in entries)
    body += bytes([TYPE_TERMINATOR])
    blob = INFO_MAGIC + struct.pack("<I", len(body)) + body
    if len(blob) > size:
        die("la VPD (%d byte) non entra nella regione (%d byte)" % (len(blob), size))
    return blob + b"\xff" * (size - len(blob))


def show_value(key, value):
    if key == "system_uuid":
        if len(value) == 16:
            return str(uuid.UUID(bytes_le=value)).upper()
        return "(%d byte, non e' un UUID: coreboot lo ignora)" % len(value)
    try:
        text = value.decode("ascii")
        if text.isprintable():
            return text
    except UnicodeDecodeError:
        pass
    return "(binario) " + value.hex()


def read_file(path):
    try:
        with open(path, "rb") as f:
            return bytearray(f.read())
    except OSError as e:
        die(str(e))


def load(path):
    data = read_file(path)
    off, size = locate(data, path)
    try:
        entries, _ = parse_region(bytes(data[off:off + size]))
    except ValueError as e:
        die("%s: %s non leggibile: %s" % (path, REGION, e))
    return data, off, size, entries


def save(data, path):
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def print_entries(entries, where):
    if not entries:
        print("%s: %s vuota" % (where, REGION))
        return
    print("%s: %s, %d chiavi" % (where, REGION, len(entries)))
    for k, v in entries:
        print("  %-20s %s" % (k, show_value(k, v)))


def check_string(key, text):
    if not text or not text.isprintable() or not text.isascii():
        die("%s: serve testo ASCII stampabile, non %r" % (key, text))
    if len(text) > MAX_STRING:
        die("%s: al massimo %d caratteri (coreboot tronca il resto)" % (key, MAX_STRING))
    return text.encode("ascii")


def cmd_show(args):
    _, off, size, entries = load(args.image)
    print_entries(entries, "%s @0x%x" % (args.image, off))


def cmd_set(args):
    if not args.output and not args.in_place:
        die("indica il file da scrivere con -o (o --in-place per modificare %s)" % args.image)
    data, off, size, entries = load(args.image)
    entries = [] if args.clear else list(entries)
    current = dict(entries)

    updates = []
    if args.serial is not None:
        serial = args.serial.strip()
        # Sull'etichetta Lenovo il S/N ha un trattino dopo i primi due
        # caratteri (R9-0ABCDE), che il BIOS Lenovo non scrive
        m = re.fullmatch(r"([0-9A-Za-z]{2})-([0-9A-Za-z]{6})", serial)
        if m:
            serial = (m.group(1) + m.group(2)).upper()
            print("numero di serie %s (senza il trattino dell'etichetta)" % serial)
        updates.append(("serial_number", check_string("serial_number", serial)))
    if args.board_serial is not None:
        updates.append(("mlb_serial_number",
                        check_string("mlb_serial_number", args.board_serial.strip())))
    if args.mtm is not None:
        mtm = re.sub(r"[\s-]", "", args.mtm).upper()
        if not re.fullmatch(r"[0-9A-Z]{10}", mtm):
            die("MTM: 10 caratteri come sull'etichetta (Type 20EF-001UGE -> 20EF001UGE), non %r"
                % args.mtm)
        family = args.family or FAMILIES.get(mtm[:4])
        if family is None:
            die("machine type %s: non e' un W541 (20EF, 20EG); indica --family" % mtm[:4])
        updates += [("system_product_name", mtm.encode()),
                    ("system_sku", ("LENOVO_MT_" + mtm[:4]).encode()),
                    ("system_family", check_string("system_family", family))]
    elif args.family is not None:
        updates.append(("system_family", check_string("system_family", args.family)))
    if args.uuid is not None:
        if args.uuid.lower() == "serial":
            merged = dict(current)
            merged.update(updates)
            serial = merged.get("serial_number")
            mtm = merged.get("system_product_name")
            if not serial or not mtm:
                die("--uuid serial: servono numero di serie e MTM (--serial, --mtm)")
            updates.append(("system_uuid", uuid.uuid5(
                UUID_NAMESPACE, "%s/%s" % (mtm.decode(), serial.decode())).bytes_le))
        elif args.uuid.lower() == "random":
            old = current.get("system_uuid")
            if old is not None and len(old) == 16 and not args.clear:
                print("system_uuid gia' presente, resta %s" % show_value("system_uuid", old))
            else:
                updates.append(("system_uuid", uuid.uuid4().bytes_le))
        else:
            try:
                updates.append(("system_uuid", uuid.UUID(args.uuid).bytes_le))
            except ValueError:
                die("UUID non valido: %r (forma 01234567-89AB-CDEF-0123-456789ABCDEF, "
                    "serial o random)" % args.uuid)
    for item in args.key:
        if "=" not in item:
            die("coppia senza '=': %r (CHIAVE=VALORE)" % item)
        key, value = item.split("=", 1)
        updates.append((key, check_string(key, value)))

    for key, value in updates:
        if not re.fullmatch(r"[A-Za-z0-9_]+", key):
            die("chiave %r: solo lettere, cifre e _ (il kernel scarta le altre)" % key)
        for i, (k, _) in enumerate(entries):
            if k == key:
                entries[i] = (key, value)
                break
        else:
            entries.append((key, value))
    for key in args.delete:
        entries = [(k, v) for k, v in entries if k != key]

    if not (args.serial or args.board_serial or args.mtm or args.family or args.uuid
            or args.key or args.delete or args.clear):
        die("niente da scrivere: --serial, --mtm, --uuid, --board-serial, --key CHIAVE=VALORE, "
            "--delete o --clear")
    data[off:off + size] = encode_region(entries, size)
    out = args.image if args.in_place else args.output
    save(data, out)
    print_entries(parse_region(bytes(data[off:off + size]))[0], "%s @0x%x" % (out, off))


def cmd_copy(args):
    if not args.output and not args.in_place:
        die("indica il file da scrivere con -o (o --in-place per modificare %s)" % args.image)
    _, _, _, entries = load(args.source)
    if not entries:
        die("%s: %s vuota, niente da copiare" % (args.source, REGION))
    data, off, size, old = load(args.image)
    if old and not args.force:
        die("%s: %s non e' vuota (--force per sostituirla)" % (args.image, REGION))
    data[off:off + size] = encode_region(entries, size)
    out = args.image if args.in_place else args.output
    save(data, out)
    print_entries(parse_region(bytes(data[off:off + size]))[0], "%s @0x%x" % (out, off))


def main():
    ap = argparse.ArgumentParser(
        description="La VPD di coreboot (regione RO_VPD) in un'immagine o in un dump del flash.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("show", help="mostra le chiavi")
    p.add_argument("image")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("set", help="scrive o cambia chiavi")
    p.add_argument("image")
    p.add_argument("-o", "--output", help="file da scrivere")
    p.add_argument("--in-place", action="store_true", help="modifica l'immagine stessa")
    p.add_argument("--serial", help="numero di serie (S/N dell'etichetta)")
    p.add_argument("--board-serial", help="numero di serie della scheda madre")
    p.add_argument("--mtm", help="Machine Type Model, es. 20EF001UGE: anche SKU e famiglia")
    p.add_argument("--family", help="famiglia, se non e' un W541")
    p.add_argument("--uuid", help="UUID; serial: ricavato da MTM e numero di serie, sempre "
                   "lo stesso; random: uno nuovo, se manca")
    p.add_argument("--key", action="append", default=[], metavar="CHIAVE=VALORE",
                   help="una chiave qualsiasi, come testo")
    p.add_argument("--delete", action="append", default=[], metavar="CHIAVE",
                   help="toglie una chiave")
    p.add_argument("--clear", action="store_true", help="parte da una VPD vuota")
    p.set_defaults(func=cmd_set)

    p = sub.add_parser("copy", help="copia la VPD di un dump in un'immagine nuova")
    p.add_argument("source", help="dump con la VPD (sudo flashrom -p internal -r)")
    p.add_argument("image", help="immagine da flashare (una release)")
    p.add_argument("-o", "--output", help="file da scrivere")
    p.add_argument("--in-place", action="store_true", help="modifica l'immagine stessa")
    p.add_argument("--force", action="store_true", help="sostituisce una VPD gia' presente")
    p.set_defaults(func=cmd_copy)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
