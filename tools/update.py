#!/usr/bin/env python3
# update.py - aggiorna ouroBIOS, il firmware del W541, da Linux con una capsula UEFI
# (docs/update.md). Ogni release ha, per variante, una capsula firmata
# (w541-coreboot-vX.Y.Z-mrc.cap): la si copia in \EFI\UpdateCapsule
# dell'ESP, si accende il bit "capsule su disco" di OsIndications e al
# riavvio il firmware la verifica e la scrive, anche con il BIOS Lock acceso.
# Dalla v1.3.0 (vboot, docs/vboot.md) scrive solo lo slot RW che non gira e
# poi lo prova; la regione RO, le variabili UEFI, la VPD e il registro degli
# eventi restano.
#
#   sudo python3 tools/update.py status
#   sudo python3 tools/update.py stage --latest        # ultima release della variante che gira
#   sudo python3 tools/update.py stage w541-coreboot-v1.2.1-mrc.cap
#   sudo python3 tools/update.py cancel
#   python3 tools/update.py info w541-coreboot-v1.2.1-mrc.cap
#   python3 tools/update.py vboot flash.bin            # da sudo flashrom -p internal -r flash.bin
#
# Al posto della .cap va bene anche il pacchetto fwupd della release (.cab,
# tools/fwupd-cab.py): se ne usa la capsula che contiene.
#
# Il firmware che gira deve avere la stessa mappa del flash della capsula: le
# capsule della v1.3.0 in su (con vboot) si installano su un firmware dalla
# v1.3.0, quelle della v1.2.x su un firmware v1.2.x; da v1.2.x a v1.3.0 si
# passa una volta con flashrom (docs/flashing.md). Il firmware applica solo
# capsule della sua variante (mrc o nri, un GUID ciascuna nell'ESRT), firmate
# dalla chiave di keys/capsule-signing.pem e non piu' vecchie della versione
# minima (LSV); qui si controlla prima, per non riavviare per niente.
#
# Solo la libreria standard di Python; openssl, se c'e', verifica anche la
# firma (con --cert, o keys/capsule-signing.pem accanto a questo script).
import argparse
import fcntl
import gzip
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import uuid
import xml.etree.ElementTree as ET

REPO = "debianita22/ourobios"

# Il GUID dell'ESRT (DRIVERS_EFI_MAIN_FW_GUID nei defconfig) di ogni variante,
# con la mappa del flash di vboot (dalla v1.3.0)
VARIANTS = {
    "mrc": uuid.UUID("1a1d806e-1dec-436f-a428-17e010b08c58"),
    "nri": uuid.UUID("ef384a6d-1e1d-4344-abf9-b26f39d9e80a"),
}
# e quelli della mappa senza vboot (v1.2.x), per riconoscere un firmware o una
# capsula di prima
LEGACY_VARIANTS = {
    "mrc": uuid.UUID("b774aa46-be0e-4ec1-bb1b-b7c0bf2722aa"),
    "nri": uuid.UUID("d945239f-4a5a-4089-8597-6bd72edd1a1f"),
}
# Le regioni che scrive una capsula (CAPSULE_REGIONS di tools/build.sh): il
# firmware scrive il training della RAM e lo slot che non gira
REGIONS = ["RW_MRC_CACHE", "RW_SECTION_A", "RW_SECTION_B"]
LEGACY_REGIONS = ["RW_MRC_CACHE", "COREBOOT"]
MIGRATION = "docs/flashing.md, \"From v1.2.x to v1.3.0\""
ROM_SIZE = 12 * 1024 * 1024

FMP_CAPSULE_GUID = uuid.UUID("6dcbd5ed-e82d-4c44-bda1-7194199ad92a")
PKCS7_CERT_GUID = uuid.UUID("4aafd29d-68df-49ee-8aa9-347d375665a7")
WIN_CERT_TYPE_EFI_GUID = 0x0EF1
FMP_PAYLOAD_SIGNATURE = b"MSS1"
CAPSULE_SUPPORT_DEPENDENCY = 0x2
RMAP_SIGNATURE = 0x50414D52  # "RMAP"
RMAP_ENTRY = 16

GLOBAL_GUID = "8be4df61-93ca-11d2-aa0d-00e098032b8c"
# W541_UPDATE_SYSROOT: un'altra radice per /sys e /proc, solo per le prove
SYSROOT = os.environ.get("W541_UPDATE_SYSROOT", "/")
EFIVARS = os.path.join(SYSROOT, "sys/firmware/efi/efivars")
ESRT = os.path.join(SYSROOT, "sys/firmware/efi/esrt/entries")
POWER_SUPPLY = os.path.join(SYSROOT, "sys/class/power_supply")
BIOS_VERSION = os.path.join(SYSROOT, "sys/class/dmi/id/bios_version")
MOUNTS = os.path.join(SYSROOT, "proc/self/mounts")
FILE_CAPSULE_DELIVERY = 0x4  # EFI_OS_INDICATIONS_FILE_CAPSULE_DELIVERY_SUPPORTED
VAR_ATTRS = 0x7              # non volatile, boot service, runtime
ESP_TYPES = ("c12a7328-f81f-11d2-ba4b-00a0c93ec93b", "0xef")
CAPSULE_DIR = os.path.join("EFI", "UpdateCapsule")
STAGED_NAME = "w541-coreboot.cap"

FS_IOC_GETFLAGS = 0x80086601
FS_IOC_SETFLAGS = 0x40086602
FS_IMMUTABLE_FL = 0x10

# LastAttemptStatus dell'ESRT (UEFI 2.10, 23.4), e i codici di
# FmpDeviceSmmLib del payload (patch edk2 0008), da 0x1800
ATTEMPT = {
    0: "riuscito",
    1: "non riuscito",
    2: "risorse insufficienti",
    3: "versione non accettata",
    4: "formato non valido",
    5: "firma non valida",
    6: "alimentatore",
    7: "batteria",
    8: "dipendenze non soddisfatte",
    0x1800: "immagine non valida (senza manifest delle regioni, o di un'altra dimensione)",
    0x1801: "mappa del flash diversa da quella del firmware che gira",
    0x1802: "scrittura del flash fallita",
}


class Error(Exception):
    pass


def version_str(v):
    """La versione dell'ESRT come quella delle release: (X << 24) | (Y << 16) | Z."""
    return "v%d.%d.%d" % (v >> 24, (v >> 16) & 0xFF, v & 0xFFFF)


def attempt_str(status):
    if status in ATTEMPT:
        return ATTEMPT[status]
    if 0x1000 <= status <= 0x17FF:
        return "errore 0x%x di FmpDxe" % status
    if 0x1800 <= status <= 0x1FFF:
        return "errore 0x%x di FmpDeviceSmmLib" % status
    return "errore 0x%x" % status


def variant_of(guid):
    for name, g in VARIANTS.items():
        if g == guid:
            return name
    return None


def legacy_variant_of(guid):
    for name, g in LEGACY_VARIANTS.items():
        if g == guid:
            return name
    return None


# --- capsule -----------------------------------------------------------------

def parse_capsule(data):
    """Le parti di una capsula FMP firmata con una sola immagine, come la
    scrive GenerateCapsule di EDK2 (tools/build.sh)."""
    if len(data) < 28:
        raise Error("troppo corta per una capsula")
    guid = uuid.UUID(bytes_le=data[0:16])
    hdr_size, flags, size = struct.unpack_from("<III", data, 16)
    if guid != FMP_CAPSULE_GUID:
        raise Error("non e' una capsula FMP (%s)" % guid)
    if size > len(data) or hdr_size < 28 or hdr_size + 16 > size:
        raise Error("intestazione della capsula non valida")
    fmp = hdr_size
    fver, ndrivers, nitems = struct.unpack_from("<IHH", data, fmp)
    if fver != 1 or ndrivers != 0 or nitems != 1:
        raise Error("attesa una sola immagine senza driver (versione %d, %d driver, %d immagini)"
                    % (fver, ndrivers, nitems))
    item = fmp + struct.unpack_from("<Q", data, fmp + 8)[0]
    if item + 48 > size:
        raise Error("immagine fuori dalla capsula")
    iver = struct.unpack_from("<I", data, item)[0]
    if iver < 3:
        raise Error("intestazione dell'immagine versione %d, attesa 3" % iver)
    type_id = uuid.UUID(bytes_le=data[item + 4:item + 20])
    index = data[item + 20]
    isize, vsize = struct.unpack_from("<II", data, item + 24)
    support = struct.unpack_from("<Q", data, item + 40)[0]
    start = item + 48
    end = start + isize
    if end > size or vsize != 0:
        raise Error("dimensioni dell'immagine non valide")
    # EFI_FIRMWARE_IMAGE_AUTHENTICATION: contatore, WIN_CERTIFICATE_UEFI_GUID
    if start + 32 > end:
        raise Error("immagine senza firma")
    monotonic = struct.unpack_from("<Q", data, start)[0]
    length, revision, cert_type = struct.unpack_from("<IHH", data, start + 8)
    if cert_type != WIN_CERT_TYPE_EFI_GUID or revision != 0x200 or length < 24:
        raise Error("firma di tipo sconosciuto")
    if uuid.UUID(bytes_le=data[start + 16:start + 32]) != PKCS7_CERT_GUID:
        raise Error("firma non PKCS#7")
    signed = start + 8 + length
    if signed + 16 > end:
        raise Error("firma fuori dall'immagine")
    signature = data[start + 32:signed]
    pos = signed
    if support & CAPSULE_SUPPORT_DEPENDENCY:
        raise Error("capsula con dipendenze: non e' di questo progetto")
    sig, phsize, fw_version, lsv = struct.unpack_from("<4sIII", data, pos)
    if sig != FMP_PAYLOAD_SIGNATURE or phsize < 16 or pos + phsize > end:
        raise Error("intestazione FMP_PAYLOAD_HEADER non valida")
    payload = data[pos + phsize:end]
    # RMAP: le regioni da scrivere, in coda all'immagine (AppendRmapManifest.py)
    regions = []
    image = payload
    if len(payload) >= 8:
        rsig, rver, count = struct.unpack_from("<IHH", payload, len(payload) - 8)
        if rsig == RMAP_SIGNATURE and rver == 1 and len(payload) >= 8 + count * RMAP_ENTRY:
            base = len(payload) - 8 - count * RMAP_ENTRY
            for i in range(count):
                name = payload[base + i * RMAP_ENTRY:base + (i + 1) * RMAP_ENTRY]
                regions.append(name.rstrip(b"\0").decode("ascii", "replace"))
            image = payload[:base]
    return {
        "flags": flags,
        "type_id": type_id,
        "variant": variant_of(type_id),
        "index": index,
        "monotonic": monotonic,
        "signature": signature,
        "signed": data[signed:end] + struct.pack("<Q", monotonic),
        "version": fw_version,
        "lsv": lsv,
        "regions": regions,
        "image": image,
        "size": size,
    }


def openssl(*args, data=None):
    return subprocess.run(["openssl"] + list(args), input=data, capture_output=True)


def fingerprint(pem):
    """L'impronta SHA-256 di un certificato PEM, come la stampa openssl."""
    r = openssl("x509", "-noout", "-fingerprint", "-sha256", data=pem)
    m = re.search(rb"=([0-9A-F:]+)", r.stdout)
    return m.group(1).decode() if m else None


def signer_of(cap):
    """Soggetto e impronta del certificato che ha firmato, se openssl c'e'."""
    if not shutil.which("openssl"):
        return None
    with tempfile.TemporaryDirectory() as tmp:
        sig = os.path.join(tmp, "sig.der")
        with open(sig, "wb") as f:
            f.write(cap["signature"])
        r = openssl("pkcs7", "-inform", "DER", "-in", sig, "-print_certs")
    m = re.search(rb"subject=\s*(.*)", r.stdout)
    pem = re.search(rb"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", r.stdout, re.S)
    if not m or not pem:
        return None
    return m.group(1).decode(errors="replace").strip(), fingerprint(pem.group(0))


def verify_signature(cap, cert):
    """La firma come la verifica EDK2 (Pkcs7Verify: il certificato fidato puo'
    essere quello che firma, nessuna data, qualsiasi uso)."""
    if not shutil.which("openssl"):
        raise Error("serve openssl per verificare la firma")
    with tempfile.TemporaryDirectory() as tmp:
        sig = os.path.join(tmp, "sig.der")
        content = os.path.join(tmp, "content.bin")
        with open(sig, "wb") as f:
            f.write(cap["signature"])
        with open(content, "wb") as f:
            f.write(cap["signed"])
        r = openssl("smime", "-verify", "-binary", "-inform", "DER", "-in", sig, "-content", content,
                    "-CAfile", cert, "-purpose", "any", "-partial_chain", "-no_check_time",
                    "-out", os.devnull)
        if r.returncode != 0:
            err = r.stderr.decode(errors="replace")
            m = re.search(r"Verify error:\s*(.*)", err)
            why = m.group(1).strip() if m else (err.strip().splitlines() or ["?"])[-1]
            raise Error("firma non valida per %s (%s): la chiave non e' quella di cui il firmware "
                        "si fida" % (cert, why))


def default_cert():
    here = os.path.dirname(os.path.realpath(__file__))
    cert = os.path.normpath(os.path.join(here, os.pardir, "keys", "capsule-signing.pem"))
    return cert if os.path.isfile(cert) else None


# --- il pacchetto fwupd ------------------------------------------------------

JCAT_SHA256 = 1
JCAT_PKCS7 = 3


def cab_checksum(data, seed=0):
    """CSUMCompute di [MS-CAB] (come tools/fwupd-cab.py)."""
    csum = seed
    n = len(data) // 4
    for (w,) in struct.iter_unpack("<I", data[:n * 4]):
        csum ^= w
    ul = 0
    for b in data[n * 4:]:
        ul = (ul << 8) | b
    return csum ^ ul


def read_cab(data):
    """I file di un cabinet senza compressione, come lo fa tools/fwupd-cab.py."""
    if len(data) < 36 or data[:4] != b"MSCF":
        raise Error("non e' un cabinet")
    (_, _, total, _, coff_files, _, _, _, nfolders, nfiles, flags, _, _) = \
        struct.unpack_from("<4sIIIIIBBHHHHH", data, 0)
    if flags != 0 or total != len(data):
        raise Error("cabinet multi-volume, con dati riservati o troncato")
    folders = []
    pos = 36
    for _ in range(nfolders):
        coff, ndata, comp = struct.unpack_from("<IHH", data, pos)
        pos += 8
        if comp != 0:
            raise Error("cabinet compresso: estrai firmware.bin con gcab -x")
        parts = []
        p = coff
        for _ in range(ndata):
            csum, cb, cbu = struct.unpack_from("<IHH", data, p)
            chunk = data[p + 8:p + 8 + cb]
            if len(chunk) != cb or cb != cbu:
                raise Error("blocco CFDATA non valido")
            if csum and cab_checksum(data[p + 4:p + 8], cab_checksum(chunk)) != csum:
                raise Error("checksum di un blocco CFDATA sbagliato: cabinet corrotto")
            parts.append(chunk)
            p += 8 + cb
        folders.append(b"".join(parts))
    files = {}
    pos = coff_files
    for _ in range(nfiles):
        size, off, ifolder = struct.unpack_from("<IIH", data, pos)
        pos += 16
        end = data.index(b"\0", pos)
        name = data[pos:end].decode("ascii", "replace")
        pos = end + 1
        if ifolder >= len(folders) or off + size > len(folders[ifolder]):
            raise Error("%s fuori dal cabinet" % name)
        files[name] = folders[ifolder][off:off + size]
    return files


def parse_metainfo(xml):
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as e:
        raise Error("firmware.metainfo.xml non valido: %s" % e)
    guids = [e.text.strip().lower() for e in root.iter("firmware") if e.get("type") == "flashed" and e.text]
    rels = root.findall("releases/release")
    fmt = [e.text for e in root.iter("value") if e.get("key") == "LVFS::VersionFormat"]
    if len(guids) != 1 or len(rels) != 1:
        raise Error("metainfo: attesi un GUID e una release")
    return {"id": root.findtext("id"), "guid": uuid.UUID(guids[0]), "version": rels[0].get("version"),
            "format": fmt[0] if fmt else None}


def verify_jcat(jcat, files, cert):
    """SHA-256 e firma PKCS#7 di ogni file del pacchetto, come le controlla
    fwupd con il certificato in /etc/pki/fwupd."""
    if not shutil.which("openssl"):
        raise Error("serve openssl per verificare la firma")
    try:
        items = {i["Id"]: i["Blobs"] for i in json.loads(gzip.decompress(jcat))["Items"]}
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise Error("firmware.jcat non valido: %s" % e)
    for name in ("firmware.bin", "firmware.metainfo.xml"):
        blobs = items.get(name, [])
        sums = [b["Data"] for b in blobs if b.get("Kind") == JCAT_SHA256]
        sigs = [b["Data"] for b in blobs if b.get("Kind") == JCAT_PKCS7]
        if sums != [hashlib.sha256(files[name]).hexdigest()]:
            raise Error("firmware.jcat: SHA-256 di %s mancante o sbagliato" % name)
        if len(sigs) != 1:
            raise Error("firmware.jcat: %s senza firma PKCS#7" % name)
        with tempfile.TemporaryDirectory() as tmp:
            sig = os.path.join(tmp, "sig.pem")
            content = os.path.join(tmp, "content.bin")
            with open(sig, "w") as f:
                f.write(sigs[0])
            with open(content, "wb") as f:
                f.write(files[name])
            r = openssl("smime", "-verify", "-binary", "-inform", "PEM", "-in", sig, "-content", content,
                        "-CAfile", cert, "-purpose", "any", "-partial_chain", "-out", os.devnull)
        if r.returncode != 0:
            raise Error("firmware.jcat: firma di %s non valida per %s" % (name, cert))


def load_cab(path, data):
    files = read_cab(data)
    for name in ("firmware.bin", "firmware.metainfo.xml"):
        if name not in files:
            raise Error("pacchetto fwupd senza %s" % name)
    meta = parse_metainfo(files["firmware.metainfo.xml"])
    cap = parse_capsule(files["firmware.bin"])
    v = cap["version"]
    if meta["guid"] != cap["type_id"]:
        raise Error("metainfo per %s, capsula per %s" % (meta["guid"], cap["type_id"]))
    if meta["format"] != "triplet" or meta["version"] != version_str(v)[1:]:
        raise Error("metainfo: versione %s (%s), la capsula e' %s" % (meta["version"], meta["format"],
                                                                    version_str(v)[1:]))
    cap["cab"] = {"files": files, "meta": meta}
    return files["firmware.bin"], cap


def load_capsule(path):
    """Una capsula, o la capsula di un pacchetto fwupd (.cab)."""
    with open(path, "rb") as f:
        data = f.read()
    try:
        if data[:4] == b"MSCF":
            return load_cab(path, data)
        return data, parse_capsule(data)
    except Error as e:
        raise Error("%s: %s" % (path, e))


def check_capsule(cap, fw=None):
    """Quello che il firmware fw (esrt_entry(), o nessuno: solo la capsula)
    controllerebbe a parte la firma."""
    legacy = legacy_variant_of(cap["type_id"])
    variant = cap["variant"] or legacy
    if variant is None:
        raise Error("capsula per un firmware sconosciuto (%s)" % cap["type_id"])
    if fw and fw["legacy"] and not legacy:
        raise Error("qui gira la %s, senza vboot: la v1.3.0 cambia la mappa del flash e si installa una "
                    "volta con flashrom (%s), poi con le capsule" % (version_str(fw["version"]), MIGRATION))
    if fw and not fw["legacy"] and legacy:
        raise Error("capsula della v1.2.x (mappa del flash senza vboot): da un firmware v1.3.0 in su "
                    "si torna alla v1.2.x solo con flashrom")
    if fw and variant != fw["variant"]:
        raise Error("capsula della variante %s, qui gira la %s: per cambiare variante si usa "
                    "flashrom (docs/flashing.md)" % (variant, fw["variant"]))
    want = LEGACY_REGIONS if legacy else REGIONS
    if cap["regions"] != want:
        raise Error("la capsula scrive %s, attese %s" % (cap["regions"] or "tutto il flash", want))
    if len(cap["image"]) != ROM_SIZE:
        raise Error("immagine di %d byte, attesi %d" % (len(cap["image"]), ROM_SIZE))


# --- il sistema --------------------------------------------------------------

def read_text(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def esrt_entry():
    """La voce dell'ESRT del firmware coreboot, se c'e'."""
    if not os.path.isdir(ESRT):
        return None
    for e in sorted(os.listdir(ESRT)):
        d = os.path.join(ESRT, e)
        try:
            guid = uuid.UUID(read_text(os.path.join(d, "fw_class")))
        except (TypeError, ValueError):
            continue
        name = variant_of(guid)
        legacy = legacy_variant_of(guid)
        if name is None and legacy is None:
            continue
        num = lambda f: int(read_text(os.path.join(d, f)) or "0", 0)
        return {
            "variant": name or legacy,
            "legacy": name is None,
            "guid": guid,
            "version": num("fw_version"),
            "lsv": num("lowest_supported_fw_version"),
            "last_version": num("last_attempt_version"),
            "last_status": num("last_attempt_status"),
        }
    return None


def efivar_path(name):
    return os.path.join(EFIVARS, "%s-%s" % (name, GLOBAL_GUID))


def read_u64_var(name):
    try:
        with open(efivar_path(name), "rb") as f:
            raw = f.read()
    except FileNotFoundError:
        return None
    if len(raw) != 12:
        raise Error("%s: %d byte, attesi 4 di attributi e 8 di valore" % (name, len(raw)))
    return struct.unpack("<IQ", raw)


def set_immutable(path, on):
    """efivarfs crea molte variabili immutabili (chattr +i)."""
    fd = os.open(path, os.O_RDONLY)
    try:
        try:
            flags = struct.unpack("i", fcntl.ioctl(fd, FS_IOC_GETFLAGS, struct.pack("i", 0)))[0]
        except OSError:
            return False
        was = bool(flags & FS_IMMUTABLE_FL)
        new = (flags | FS_IMMUTABLE_FL) if on else (flags & ~FS_IMMUTABLE_FL)
        if new != flags:
            fcntl.ioctl(fd, FS_IOC_SETFLAGS, struct.pack("i", new))
        return was
    finally:
        os.close(fd)


def write_os_indications(value):
    path = efivar_path("OsIndications")
    cur = read_u64_var("OsIndications")
    attrs = cur[0] if cur else VAR_ATTRS
    was = set_immutable(path, False) if cur else False
    try:
        # efivarfs vuole attributi e dati in una sola write()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o644)
        try:
            data = struct.pack("<IQ", attrs, value)
            if os.write(fd, data) != len(data):
                raise Error("OsIndications: scrittura incompleta")
        finally:
            os.close(fd)
    finally:
        if was:
            set_immutable(path, True)


def os_indications():
    cur = read_u64_var("OsIndications")
    return cur[1] if cur else 0


def cod_supported():
    cur = read_u64_var("OsIndicationsSupported")
    return bool(cur and cur[1] & FILE_CAPSULE_DELIVERY)


def mounted_vfat():
    out = {}
    with open(MOUNTS) as f:
        for line in f:
            dev, mnt, fstype = line.split()[:3]
            if fstype == "vfat":
                out[mnt.replace("\\040", " ")] = dev
    return out


def find_esp(explicit):
    if explicit:
        if not os.path.isdir(os.path.join(explicit, "EFI")):
            raise Error("%s: nessuna cartella EFI, non sembra un'ESP" % explicit)
        return explicit
    vfat = mounted_vfat()
    esps = []
    try:
        out = subprocess.run(["lsblk", "-J", "-o", "PATH,PARTTYPE,MOUNTPOINT"],
                             capture_output=True, check=True).stdout
        for dev in json.loads(out).get("blockdevices", []):
            stack = [dev]
            while stack:
                d = stack.pop()
                stack.extend(d.get("children") or [])
                if (d.get("parttype") or "").lower() in ESP_TYPES and d.get("mountpoint") in vfat:
                    esps.append(d["mountpoint"])
    except (OSError, subprocess.CalledProcessError, ValueError):
        pass
    if not esps:
        esps = [m for m in ("/boot/efi", "/efi", "/boot") if m in vfat]
    esps = [m for m in dict.fromkeys(esps) if os.path.isdir(os.path.join(m, "EFI"))]
    if len(esps) != 1:
        raise Error("ESP %s: indicala con --esp DIR" % ("non trovata" if not esps else
                                                         "ambigua (%s)" % ", ".join(esps)))
    return esps[0]


def staged_files(esp):
    d = os.path.join(esp, CAPSULE_DIR)
    if not os.path.isdir(d):
        return []
    return sorted(os.path.join(d, f) for f in os.listdir(d)
                  if os.path.isfile(os.path.join(d, f)))


def power():
    """(alimentatore collegato o None, carica della batteria o None)"""
    ac = None
    charge = None
    base = POWER_SUPPLY
    for p in sorted(os.listdir(base)) if os.path.isdir(base) else []:
        kind = read_text(os.path.join(base, p, "type"))
        if kind == "Mains":
            ac = (read_text(os.path.join(base, p, "online")) == "1") or bool(ac)
        elif kind == "Battery" and charge is None:
            c = read_text(os.path.join(base, p, "capacity"))
            charge = int(c) if c and c.isdigit() else None
    return ac, charge


# --- le release --------------------------------------------------------------

def http_get(url, accept=None):
    req = urllib.request.Request(url, headers={"User-Agent": "w541-coreboot-update"})
    if accept:
        req.add_header("Accept", accept)
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def release_key(tag):
    m = re.match(r"^v(\d+)\.(\d+)\.(\d+)(-[0-9A-Za-z.]+)?-(mrc|nri)$", tag)
    if not m:
        return None
    # a parita' di numeri la release vera viene dopo le -rcN
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4) is None, m.group(4) or "")


def download_capsule(rel, dest):
    """La capsula di una release di GitHub (la voce JSON dell'API), controllata
    con il suo SHA256SUMS."""
    tag = rel["tag_name"]
    assets = {a["name"]: a["browser_download_url"] for a in rel.get("assets", [])}
    cap_name = "w541-coreboot-%s.cap" % tag
    if cap_name not in assets or "SHA256SUMS" not in assets:
        raise Error("%s: senza %s o SHA256SUMS (le capsule ci sono dalla v1.2.0)" % (tag, cap_name))
    print("release %s: scarico %s" % (tag, cap_name))
    sums = http_get(assets["SHA256SUMS"]).decode()
    want = None
    for line in sums.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == cap_name:
            want = parts[0].lower()
    if not want:
        raise Error("SHA256SUMS di %s senza %s" % (tag, cap_name))
    data = http_get(assets[cap_name])
    if hashlib.sha256(data).hexdigest() != want:
        raise Error("%s: SHA-256 diverso da SHA256SUMS, download corrotto" % cap_name)
    path = os.path.join(dest, cap_name)
    with open(path, "wb") as f:
        f.write(data)
    return path


def download_latest(variant, pre, dest):
    rels = json.loads(http_get("https://api.github.com/repos/%s/releases?per_page=50" % REPO,
                               "application/vnd.github+json"))
    best = None
    for r in rels:
        tag = r.get("tag_name", "")
        if r.get("draft") or not tag.endswith("-" + variant) or release_key(tag) is None:
            continue
        # la nri e' sempre una pre-release
        if r.get("prerelease") and not pre and variant != "nri":
            continue
        if best is None or release_key(tag) > release_key(best["tag_name"]):
            best = r
    if best is None:
        raise Error("nessuna release %s con capsula su github.com/%s" % (variant, REPO))
    return download_capsule(best, dest)


def download_tag(tag, dest):
    try:
        rel = json.loads(http_get("https://api.github.com/repos/%s/releases/tags/%s" % (REPO, tag),
                                  "application/vnd.github+json"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise Error("la release %s non c'e' su github.com/%s: --capsule FILE" % (tag, REPO))
        raise
    return download_capsule(rel, dest)


# --- i comandi ---------------------------------------------------------------

def need_root():
    if os.geteuid() != 0:
        raise Error("serve root (sudo): si scrivono variabili UEFI e l'ESP")


def need_firmware():
    if not os.path.isdir(EFIVARS):
        raise Error("niente %s: Linux non e' partito in UEFI" % EFIVARS)
    fw = esrt_entry()
    if fw is None or not cod_supported():
        raise Error("il firmware che gira non applica capsule (e' piu' vecchio della v1.2.0?): "
                    "la prima volta si scrive con flashrom, docs/flashing.md")
    return fw


def print_capsule(path, cap):
    if "cab" in cap:
        print("pacchetto: %s (fwupd, %s)" % (path, cap["cab"]["meta"]["id"]))
        print("           %s" % " ".join(sorted(cap["cab"]["files"])))
    print("capsula:   %s" % (path if "cab" not in cap else "firmware.bin"))
    legacy = legacy_variant_of(cap["type_id"])
    print("firmware:  ouroBIOS, variante %s%s (%s)" % (cap["variant"] or legacy or "?",
                                                         ", v1.2.x senza vboot" if legacy else "", cap["type_id"]))
    print("versione:  %s, minima %s" % (version_str(cap["version"]), version_str(cap["lsv"])))
    print("regioni:   %s" % (" ".join(cap["regions"]) or "tutto il flash"))
    signer = signer_of(cap)
    if signer:
        print("firmata:   %s" % signer[0])
        print("           SHA-256 %s" % signer[1])


def cmd_info(args):
    data, cap = load_capsule(args.capsule)
    print_capsule(args.capsule, cap)
    check_capsule(cap)
    if args.guid and cap["type_id"] != uuid.UUID(args.guid):
        raise Error("GUID %s, atteso %s" % (cap["type_id"], args.guid))
    if args.fw_version is not None and cap["version"] != args.fw_version:
        raise Error("versione %s, attesa %s" % (version_str(cap["version"]), version_str(args.fw_version)))
    if args.lsv is not None and cap["lsv"] != args.lsv:
        raise Error("versione minima %s, attesa %s" % (version_str(cap["lsv"]), version_str(args.lsv)))
    if args.rom:
        with open(args.rom, "rb") as f:
            if f.read() != cap["image"]:
                raise Error("l'immagine nella capsula non e' %s" % args.rom)
        print("immagine:  uguale a %s" % args.rom)
    if args.same_as:
        with open(args.same_as, "rb") as f:
            if f.read() != data:
                raise Error("la capsula non e' %s" % args.same_as)
        print("capsula:   uguale a %s" % args.same_as)
    cert = args.cert or default_cert()
    if cert and (args.cert or shutil.which("openssl")):
        verify_signature(cap, cert)
        print("firma:     valida per %s" % cert)
        if "cab" in cap:
            if "firmware.jcat" not in cap["cab"]["files"]:
                raise Error("pacchetto fwupd senza firmware.jcat: fwupd non lo installa")
            verify_jcat(cap["cab"]["files"]["firmware.jcat"], cap["cab"]["files"], cert)
            print("fwupd:     firmware.jcat valido per %s" % cert)
    elif args.cert is None:
        print("firma:     non verificata (serve openssl e keys/capsule-signing.pem)")
    return 0


def cmd_status(args):
    fw = esrt_entry()
    bios = read_text(BIOS_VERSION)
    if fw is None:
        print("firmware:  %s, senza aggiornamento con capsula (prima della v1.2.0?)" % (bios or "?"))
        return 0
    print("firmware:  ouroBIOS, variante %s (BIOS %s)" % (fw["variant"], bios or "?"))
    if fw["legacy"]:
        print("mappa:     senza vboot (v1.2.x): la v1.3.0 si installa una volta con flashrom (%s)" % MIGRATION)
    else:
        print("mappa:     vboot, due slot RW (una capsula scrive quello che non gira)")
    print("versione:  %s, minima per una capsula %s" % (version_str(fw["version"]), version_str(fw["lsv"])))
    if fw["last_version"]:
        print("ultimo:    %s, %s" % (version_str(fw["last_version"]), attempt_str(fw["last_status"])))
        # riuscito vuol dire scritto: se lo slot nuovo non si verifica o non
        # arriva al payload, vboot torna al vecchio e l'ESRT lo dice solo cosi'
        # (tools/qemu-update.py, passi 6 e 7)
        if not fw["legacy"] and fw["last_status"] == 0 and fw["last_version"] != fw["version"]:
            print("nota:      la capsula %s e' stata scritta, ma gira la %s: vboot e' tornato all'altro "
                  "slot (quello nuovo non si e' verificato o non e' partito), si e' tornati indietro dal "
                  "setup (Firmware Update), o il flash e' stato riscritto dopo. Gli slot: sudo flashrom "
                  "-p internal -r flash.bin, poi update.py vboot flash.bin"
                  % (version_str(fw["last_version"]), version_str(fw["version"])))
    else:
        print("ultimo:    nessun aggiornamento con capsula")
    print("su disco:  %s" % ("supportato" if cod_supported() else "NON supportato da questo firmware"))
    armed = bool(os_indications() & FILE_CAPSULE_DELIVERY)
    try:
        esp = find_esp(args.esp)
        files = staged_files(esp)
    except Error as e:
        print("ESP:       %s" % e)
        return 0
    except PermissionError:
        print("ESP:       non leggibile senza root (sudo)")
        return 0
    for f in files:
        try:
            _, cap = load_capsule(f)
            what = "%s %s" % (cap["variant"] or legacy_variant_of(cap["type_id"]), version_str(cap["version"]))
        except (Error, OSError) as e:
            what = str(e)
        print("in attesa: %s (%s)" % (f, what))
    if files and armed:
        print("           si applica al prossimo avvio")
    elif files:
        print("           NON si applica: manca il bit di OsIndications (stage di nuovo, o cancel)")
    elif armed:
        print("in attesa: nessun file, ma il bit di OsIndications e' acceso (cancel lo spegne)")
    else:
        print("in attesa: niente")
    return 0


def stage_capsule(path, data, cap, fw, args, allow_older):
    """I controlli del firmware, poi la capsula nell'ESP e il bit di
    OsIndications: si applica al prossimo avvio."""
    print_capsule(path, cap)
    check_capsule(cap, fw)
    cert = args.cert or default_cert()
    if cert and shutil.which("openssl"):
        verify_signature(cap, cert)
        print("firma:     valida")
    if cap["version"] < fw["lsv"]:
        raise Error("%s e' piu' vecchia della minima %s: il firmware la rifiuterebbe"
                    % (version_str(cap["version"]), version_str(fw["lsv"])))
    if cap["version"] <= fw["version"] and not allow_older:
        step = rc_step(cap, fw)
        if not step:
            raise Error("gira gia' la %s: per reinstallare o tornare indietro --allow-older"
                        % version_str(fw["version"]))
        print("passo:     %s -> %s, la stessa %s" % (step[0], step[1], version_str(fw["version"])))
    ac, charge = power()
    if ac is False and not args.on_battery:
        raise Error("collega l'alimentatore (o --on-battery: il firmware aspetta comunque "
                    "una carica del 25%% o l'alimentatore). Batteria: %s%%" % charge)
    esp = find_esp(args.esp)
    d = os.path.join(esp, CAPSULE_DIR)
    os.makedirs(d, exist_ok=True)
    for f in staged_files(esp):
        if os.path.basename(f) != STAGED_NAME:
            print("attenzione: c'e' anche %s, il firmware applica anche quello" % f)
    dst = os.path.join(d, STAGED_NAME)
    part = dst + ".part"
    with open(part, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(part, dst)
    os.sync()
    with open(dst, "rb") as f:
        if hashlib.sha256(f.read()).digest() != hashlib.sha256(data).digest():
            raise Error("%s: la copia nell'ESP non e' uguale" % dst)
    write_os_indications(os_indications() | FILE_CAPSULE_DELIVERY)
    if not os_indications() & FILE_CAPSULE_DELIVERY:
        raise Error("OsIndications non scritta")


def finish_stage(args):
    if args.reboot:
        subprocess.run(["systemctl", "reboot"], check=False)
    else:
        print("  sudo systemctl reboot")
    return 0


def cmd_stage(args):
    need_root()
    fw = need_firmware()
    with tempfile.TemporaryDirectory() as tmp:
        path = args.capsule
        if args.latest:
            path = download_latest(fw["variant"], args.pre, tmp)
        data, cap = load_capsule(path)
        stage_capsule(path, data, cap, fw, args, args.allow_older)
    print("\npronta: %s %s -> %s, si applica al prossimo riavvio." % (
        fw["variant"], version_str(fw["version"]), version_str(cap["version"])))
    print("Il firmware la verifica e la scrive in circa un minuto, con una barra sotto il")
    print("logo: non spegnere. Poi riparte da solo con il firmware nuovo.")
    return finish_stage(args)


def cmd_cancel(args):
    need_root()
    esp = find_esp(args.esp)
    for f in staged_files(esp):
        if os.path.basename(f) == STAGED_NAME or args.all:
            os.remove(f)
            print("tolta %s" % f)
        else:
            print("lasciata %s (non e' di update.py: --all per toglierla)" % f)
    if os_indications() & FILE_CAPSULE_DELIVERY:
        write_os_indications(os_indications() & ~FILE_CAPSULE_DELIVERY)
        print("bit di OsIndications spento")
    return 0


# --- vboot: lo stato in RW_NVRAM ----------------------------------------------
#
# Record V1 di vboot da 16 byte, accodati dall'inizio della regione (coreboot,
# vbnv_flash.c; il payload, FmpDeviceSmmLib); il corrente e' l'ultimo prima del
# primo cancellato. Campi da vboot_reference, 2nvstorage_fields.h.
NV_RECORD = 16
NV_RESULTS = ("-", "in prova", "confermato", "fallito")


def nv_crc8(data):
    """Il CRC-8 di vboot (x^8 + x^2 + x + 1)."""
    crc = 0
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            if crc & 0x8000:
                crc ^= 0x1070 << 3
            crc = (crc << 1) & 0xffff
    return crc >> 8


def fmap_areas(data):
    """Le aree della FMAP di un'immagine del flash, {nome: (offset, size)}; None senza FMAP."""
    pos = data.find(b"__FMAP__")
    while pos >= 0:
        if pos + 56 <= len(data):
            major, _, _, _, _, n = struct.unpack_from("<BBQI32sH", data, pos + 8)
            if major == 1 and pos + 56 + 42 * n <= len(data):
                areas = {}
                for i in range(n):
                    off, size, name, _ = struct.unpack_from("<II32sH", data, pos + 56 + 42 * i)
                    areas[name.split(b"\0")[0].decode("ascii", "replace")] = (off, size)
                return areas
        pos = data.find(b"__FMAP__", pos + 1)
    return None


def nv_state(region):
    """Il record corrente di RW_NVRAM: dict con i campi, o None se non ce n'e' uno valido."""
    used = 0
    while used + NV_RECORD <= len(region) and region[used:used + NV_RECORD] != b"\xff" * NV_RECORD:
        used += NV_RECORD
    st = {"used": used // NV_RECORD, "total": len(region) // NV_RECORD, "offset": used - NV_RECORD}
    rec = region[used - NV_RECORD:used] if used else b""
    if not rec or rec[0] & 0xc3 != 0x40 or nv_crc8(rec[:15]) != rec[15]:
        st["valid"] = False
        return st
    boot, boot2 = rec[1], rec[7]
    st.update(valid=True, try_next=(boot2 >> 3) & 1, try_count=boot & 0x0f,
              tried=(boot2 >> 2) & 1, result=boot2 & 3,
              prev_tried=(boot2 >> 6) & 1, prev_result=(boot2 >> 4) & 3,
              recovery=rec[2])
    return st


def nv_coreboot_current(region):
    """L'offset del record che coreboot (vbnv_flash.c, init_vbnv) prende come corrente:
    una ricerca binaria del confine tra record usati e vuoti, che si fida che dopo il
    primo vuoto sia tutto vuoto."""
    empty = b"\xff" * NV_RECORD
    used_below, empty_above = 0, len(region) // NV_RECORD
    while used_below + 1 < empty_above:
        guess = (used_below + empty_above) // 2
        if region[guess * NV_RECORD:(guess + 1) * NV_RECORD] == empty:
            empty_above = guess
        else:
            used_below = guess
    return used_below * NV_RECORD


# La release scritta in una regione: coreboot mette la sua versione, con
# CONFIG_LOCALVERSION, nel banner del bootblock e di ogni stage. Due release
# nella stessa regione: una scrittura rimasta a meta'.
RELEASE_RE = re.compile(rb"(?:ouroBIOS|w541)-v\d+\.\d+\.\d+(?:-rc\d+)?-(?:mrc|nri)")


# (X, Y, Z, rc) di una release come la scrive la build (ouroBIOS-vX.Y.Z-rcN-mrc),
# con la release finale dopo tutte le sue rc; release_key e' quella dei tag
RELEASE_KEY_RE = re.compile(r"(?:ouroBIOS|w541)-v(\d+)\.(\d+)\.(\d+)(?:-rc(\d+))?-(?:mrc|nri)")


def build_release_key(name):
    m = RELEASE_KEY_RE.search(name or "")
    if not m:
        return None
    x, y, z, rc = m.groups()
    return (int(x), int(y), int(z), int(rc) if rc else 1 << 30)


def rc_step(cap, fw):
    """Le rc e la release finale di una versione hanno la stessa versione
    X.Y.Z nella capsula: dalla vX.Y.Z-rcN a una rc dopo, o alla release, e'
    comunque un passo avanti. (release che gira, release della capsula) se lo
    e', None altrimenti."""
    if cap["version"] != fw["version"]:
        return None
    names = region_releases(cap["image"], (0, len(cap["image"])))
    running = RELEASE_KEY_RE.search(read_text(BIOS_VERSION) or "")
    if len(names) != 1 or running is None:
        return None
    old, new = build_release_key(running.group()), build_release_key(names[0])
    if new is None or old is None or new[:3] != old[:3] or new <= old:
        return None
    return running.group(), names[0]


def region_releases(data, area):
    off, size = area
    found = []
    for m in RELEASE_RE.finditer(data[off:off + size]):
        name = m.group().decode()
        if name not in found:
            found.append(name)
    return found


def need_whole_flash(path, data, areas):
    """Una copia da 8 MiB senza FMAP e' solo il primo chip: flashrom e' passato
    da /dev/mtd0 perche' il driver del kernel teneva il controller SPI."""
    if areas is None and len(data) == 8 << 20:
        raise Error("%s: 8 MiB, solo il primo chip: flashrom e' passato da /dev/mtd0 (driver "
                    "spi_intel caricato). sudo modprobe -r spi_intel_platform spi_intel, poi "
                    "rileggi: deve venire di 12 MiB" % path)


def flash_image(path):
    """L'immagine del flash di una release: il .rom, o quella in una capsula (.cap, .cab)."""
    with open(path, "rb") as f:
        data = f.read()
    if data[:4] == b"MSCF" or (len(data) >= 16 and uuid.UUID(bytes_le=data[:16]) == FMP_CAPSULE_GUID):
        return load_capsule(path)[1]["image"]
    return data


def region_diff(a, b, area, block=0x1000):
    """I blocchi da 4 KiB di un'area diversi tra due immagini."""
    off, size = area
    return [o for o in range(off, off + size, block) if a[o:o + block] != b[o:o + block]]


def cmd_vboot(args):
    data = open(args.dump, "rb").read()
    areas = fmap_areas(data)
    need_whole_flash(args.dump, data, areas)
    if areas is not None:
        if "RW_NVRAM" not in areas:
            raise Error("%s: la FMAP non ha RW_NVRAM: un firmware senza vboot (prima della v1.3.0)" % args.dump)
        off, size = areas["RW_NVRAM"]
        if off + size > len(data):
            raise Error("%s: RW_NVRAM (0x%x, %d KiB) fuori dal file" % (args.dump, off, size // 1024))
        region = data[off:off + size]
    elif 0 < len(data) <= 0x100000 and len(data) % NV_RECORD == 0:
        region = data       # solo RW_NVRAM (flashrom -i RW_NVRAM:FILE)
    else:
        raise Error("%s: ne' un'immagine del flash con la FMAP ne' una copia di RW_NVRAM" % args.dump)
    slot = "AB"
    image = None
    if args.image:
        if areas is None:
            raise Error("%s: per --image serve tutto il flash (flashrom -r)" % args.dump)
        image = flash_image(args.image)
        iareas = fmap_areas(image)
        names = ("WP_RO", "RW_SECTION_A", "RW_SECTION_B")
        if len(image) != len(data) or iareas is None or any(iareas.get(n) != areas.get(n) for n in names):
            raise Error("%s: un'immagine con un'altra mappa del flash" % args.image)
    if areas is not None:
        # quale release c'e' nella parte read-only e in ciascuno slot; con
        # --image, se ogni byte e' quello della release
        head = "contenuto:"
        for label, name, whole in (("RO", "WP_RO", "WP_RO"), ("slot A", "FW_MAIN_A", "RW_SECTION_A"),
                                   ("slot B", "FW_MAIN_B", "RW_SECTION_B")):
            if name not in areas or sum(areas[name]) > len(data):
                continue
            found = region_releases(data, areas[name])
            print("%-12s%-8s%s" % (head, label, " + ".join(found) + (
                "  (mescolate: scritto a meta'?)" if len(found) > 1 else "")
                if found else "non riconosciuto: vuoto o rovinato?"))
            if image is not None:
                diff = region_diff(data, image, areas[whole])
                total = areas[whole][1] // 0x1000
                if not diff:
                    print("%20s= %s, tutti i %d blocchi da 4 KiB" % ("", os.path.basename(args.image), total))
                else:
                    print("%20s!= %s: %d blocchi da 4 KiB su %d, da 0x%06x a 0x%06x" % (
                        "", os.path.basename(args.image), len(diff), total, diff[0], diff[-1] + 0xfff))
            head = ""
    st = nv_state(region)
    print("RW_NVRAM:   %d record su %d" % (st["used"], st["total"]))
    if args.all:
        # la storia: ogni record e' lo stato dopo una scrittura (vboot dal
        # bootblock, il payload con la capsula o la conferma dello slot)
        print("            #   prossimo  tent.  corrente          precedente         byte")
        for i in range(st["used"]):
            rec = region[i * NV_RECORD:(i + 1) * NV_RECORD]
            if rec[0] & 0xc3 != 0x40 or nv_crc8(rec[:15]) != rec[15]:
                print("            %-3d record non valido                                          %s" % (i, rec.hex()))
                continue
            b1, b2 = rec[1], rec[7]
            print("            %-3d slot %s    %-5d  slot %s %-10s  slot %s %-10s  %s" % (
                i, slot[(b2 >> 3) & 1], b1 & 0x0f, slot[(b2 >> 2) & 1], NV_RESULTS[b2 & 3],
                slot[(b2 >> 6) & 1], NV_RESULTS[(b2 >> 4) & 3], rec.hex()))
        # quello che legge coreboot, e quello che c'e' oltre il primo record vuoto
        cb = nv_coreboot_current(region)
        beyond = [o for o in range(st["used"] * NV_RECORD, len(region), NV_RECORD)
                  if region[o:o + NV_RECORD] != b"\xff" * NV_RECORD]
        if st["used"] == 0 and not beyond:
            print("coreboot:   nessun record da leggere")
        else:
            print("coreboot:   legge il record a 0x%04x (#%d)%s" % (
                cb, cb // NV_RECORD, "" if cb == st["offset"] else ", NON l'ultimo prima del primo vuoto"))
        if beyond:
            print("oltre:      %d record non vuoti dopo il primo vuoto, da 0x%04x: %s" % (
                len(beyond), beyond[0], " ".join("0x%04x" % o for o in beyond[:8])))
    if not st["valid"]:
        print("stato:      nessun record valido: al prossimo avvio vboot riparte dallo slot A")
        return 0
    n = st["try_count"]
    print("prossimo:   slot %s%s" % (slot[st["try_next"]],
                                     ", %d tentativ%s" % (n, "o" if n == 1 else "i") if n else ""))
    print("corrente:   slot %s, %s" % (slot[st["tried"]], NV_RESULTS[st["result"]]))
    print("precedente: slot %s, %s" % (slot[st["prev_tried"]], NV_RESULTS[st["prev_result"]]))
    if st["recovery"]:
        print("recovery:   richiesto, motivo 0x%02x (questo firmware non ha recovery)" % st["recovery"])
    if st["prev_result"] == 3:
        print("nota:       lo slot %s non si e' verificato o non e' partito, vboot e' passato al %s"
              % (slot[st["prev_tried"]], slot[st["tried"]]))
    return 0


# --- rollback: la release dell'altro slot ----------------------------------------
#
# Dopo un aggiornamento lo slot che non gira tiene la release di prima. La sua
# capsula non vi riscrive niente (i blocchi sono gia' quelli) e chiede a vboot
# di provarlo, con due tentativi: il payload fa il resto come per ogni capsula.

TAG_RE = re.compile(r"v\d+\.\d+\.\d+(?:-rc\d+)?-(?:mrc|nri)")


def rollback_plan(data, running):
    """Da una copia di tutto il flash, presa in questo avvio, e dalla release
    che gira (bios_version): lo slot che gira, l'altro e la release che c'e'
    dentro, se si puo' tornare a quella."""
    areas = fmap_areas(data)
    need = ("RW_NVRAM", "FW_MAIN_A", "FW_MAIN_B", "RW_SECTION_A", "RW_SECTION_B")
    if areas is None or any(n not in areas or sum(areas[n]) > len(data) for n in need):
        raise Error("serve tutto il flash di un firmware con vboot (v1.3.0 in su): "
                    "sudo flashrom -p internal -r FILE")
    off, size = areas["RW_NVRAM"]
    st = nv_state(data[off:off + size])
    if not st["valid"]:
        raise Error("RW_NVRAM senza un record valido: non si sa quale slot gira")
    slot = "AB"
    cur, other = st["tried"], 1 - st["tried"]
    found = {s: region_releases(data, areas["FW_MAIN_" + slot[s]]) for s in (0, 1)}
    # confrontate per tag (v1.3.1-mrc): il prefisso, w541- o ouroBIOS-, e' cambiato
    tag = lambda name: TAG_RE.search(name or "").group() if TAG_RE.search(name or "") else None
    if running is None or [tag(n) for n in found[cur]] != [tag(running)]:
        raise Error("nello slot %s, quello di questo avvio secondo RW_NVRAM, c'e' %s e gira %s: "
                    "una copia del flash di un altro avvio? rileggila"
                    % (slot[cur], " + ".join(found[cur]) or "altro", running or "?"))
    if len(found[other]) != 1:
        raise Error("slot %s: %s, non c'e' una release a cui tornare (vuoto, rovinato o scritto a "
                    "meta')" % (slot[other], " + ".join(found[other]) or "nessuna release"))
    back = found[other][0]
    old, new = build_release_key(back), build_release_key(running)
    if old is None or new is None or tag(back) is None:
        raise Error("slot %s: release %s non riconosciuta" % (slot[other], back))
    if tag(back).rsplit("-", 1)[1] != tag(running).rsplit("-", 1)[1]:
        raise Error("slot %s: %s, un'altra variante: si cambia variante con flashrom" % (slot[other], back))
    if old == new:
        raise Error("lo slot %s ha la stessa release che gira, %s: niente a cui tornare" % (slot[other], back))
    if old > new:
        raise Error("lo slot %s ha la %s, piu' recente di quella che gira: non e' un ritorno indietro "
                    "(vboot vi e' tornato da li'? update.py vboot). Per riprovarla: stage" % (slot[other], back))
    return {"current": slot[cur], "other": slot[other], "running": running, "back": back,
            "tag": tag(back), "areas": areas,
            "pending": st["try_count"] > 0 and st["try_next"] == other}


def cmd_rollback(args):
    need_root()
    fw = need_firmware()
    if fw["legacy"]:
        raise Error("v1.2.x: senza vboot non c'e' un altro slot")
    with open(args.dump, "rb") as f:
        flash = f.read()
    need_whole_flash(args.dump, flash, fmap_areas(flash))
    running = RELEASE_KEY_RE.search(read_text(BIOS_VERSION) or "")
    plan = rollback_plan(flash, running.group() if running else None)
    print("gira:      slot %s, %s" % (plan["current"], plan["running"]))
    print("indietro:  slot %s, %s" % (plan["other"], plan["back"]))
    if plan["pending"]:
        print("nota:      vboot ha gia' in programma di provare lo slot %s al prossimo avvio" % plan["other"])
    with tempfile.TemporaryDirectory() as tmp:
        path = args.capsule or download_tag(plan["tag"], tmp)
        data, cap = load_capsule(path)
        names = region_releases(cap["image"], plan["areas"]["FW_MAIN_" + plan["other"]])
        if names != [plan["back"]]:
            raise Error("%s: nello slot %s porta %s, non %s"
                        % (path, plan["other"], " + ".join(names) or "altro", plan["back"]))
        area = plan["areas"]["RW_SECTION_" + plan["other"]]
        diff = region_diff(flash, cap["image"], area)
        if diff:
            print("slot %s:    %d blocchi da 4 KiB su %d diversi dalla release: il firmware li riscrive"
                  % (plan["other"], len(diff), area[1] // 0x1000))
        else:
            print("slot %s:    uguale alla release: il firmware non riscrive niente, cambia solo slot"
                  % plan["other"])
        if args.dry_run:
            print("\n--dry-run: niente di preparato")
            return 0
        stage_capsule(path, data, cap, fw, args, True)
    print("\npronta: al prossimo riavvio %s -> %s (slot %s)." % (plan["running"], plan["back"], plan["other"]))
    print("Il firmware verifica la capsula e chiede a vboot lo slot %s, con due tentativi;" % plan["other"])
    print("svuota anche il training della RAM: il primo avvio dura qualche secondo in piu'.")
    print("Se lo slot %s non partisse, vboot tornerebbe allo slot %s." % (plan["other"], plan["current"]))
    return finish_stage(args)


# --- le variabili UEFI in SMMSTORE ---------------------------------------------
#
# Lo store di EDK2 nella regione SMMSTORE: un firmware volume
# (EFI_SYSTEM_NV_DATA_FV), poi VARIABLE_STORE_HEADER e le variabili una dopo
# l'altra, allineate a 4 byte (MdeModulePkg, VariableFormat.h). Si mostrano
# nomi, GUID, stato, attributi e dimensioni, mai i dati.
VAR_STATES = {0x3f: "valida", 0x3e: "valida (in cancellazione)", 0x3d: "cancellata",
              0x3c: "cancellata", 0x7f: "solo header", 0xff: "vuota"}
AUTH_VAR_GUID = bytes.fromhex("782cf3aa7b949a43a1802e144ec37792")


def guid_str(b):
    a, c, d = struct.unpack_from("<IHH", b, 0)
    return "%08x-%04x-%04x-%s-%s" % (a, c, d, b[8:10].hex(), b[10:16].hex())


def var_store(region):
    """Le variabili dello store: lista di dict, e i dati dello store."""
    if region[0x28:0x2c] != b"_FVH":
        raise Error("SMMSTORE: niente firmware volume")
    hlen = struct.unpack_from("<H", region, 0x30)[0]
    fv_len = struct.unpack_from("<Q", region, 0x20)[0]
    sig = region[hlen:hlen + 16]
    size, fmt, state = struct.unpack_from("<IBB", region, hlen + 16)
    auth = sig == AUTH_VAR_GUID
    hsize = 60 if auth else 32
    start = hlen + 28
    end = min(hlen + size, len(region))
    out = []
    off = start
    while off + hsize <= end:
        if struct.unpack_from("<H", region, off)[0] != 0x55aa:
            break
        st, attrs = region[off + 2], struct.unpack_from("<I", region, off + 4)[0]
        if auth:
            nsize, dsize = struct.unpack_from("<II", region, off + 36)
            guid = region[off + 44:off + 60]
        else:
            nsize, dsize = struct.unpack_from("<II", region, off + 8)
            guid = region[off + 16:off + 32]
        if nsize > end or dsize > end:
            out.append({"off": off, "state": st, "name": "?", "guid": guid, "attrs": attrs,
                        "size": dsize, "bad": True})
            break
        name = region[off + hsize:off + hsize + nsize].decode("utf-16-le", "replace").rstrip("\0")
        # data: dove cominciano i dati (li legge solo tools/qemu-smoke.py, sul
        # flash di QEMU; vars non li stampa mai)
        out.append({"off": off, "state": st, "name": name, "guid": guid, "attrs": attrs, "size": dsize,
                    "data": off + hsize + nsize})
        off = (off + hsize + nsize + dsize + 3) & ~3
    # next: dove va la prossima variabile (tools/qemu-update.py ve la scrive,
    # nel flash di QEMU)
    info = {"fv_len": fv_len, "store_size": size, "format": fmt, "health": state, "auth": auth,
            "used": off - start, "free": end - off, "next": off}
    return out, info


def cmd_vars(args):
    data = open(args.dump, "rb").read()
    areas = fmap_areas(data)
    need_whole_flash(args.dump, data, areas)
    if areas is not None and "SMMSTORE" in areas:
        o, s = areas["SMMSTORE"]
        region = data[o:o + s]
    else:
        region = data
    vars_, info = var_store(region)
    print("store:      %s, %d KiB, formato 0x%02x, stato 0x%02x%s" % (
        "autenticato" if info["auth"] else "normale", info["store_size"] // 1024, info["format"],
        info["health"], "" if info["health"] == 0xfe else " (NON sano)"))
    states = {}
    for v in vars_:
        states[VAR_STATES.get(v["state"], "0x%02x" % v["state"])] = states.get(
            VAR_STATES.get(v["state"], "0x%02x" % v["state"]), 0) + 1
    print("variabili:  %s" % ", ".join("%d %s" % (n, k) for k, n in sorted(states.items())))
    print("spazio:     %d KiB usati, %d KiB liberi" % (info["used"] // 1024, info["free"] // 1024))
    pat = re.compile(args.grep, re.I) if args.grep else None
    for v in vars_:
        if pat and not (pat.search(v["name"]) or pat.search(guid_str(v["guid"]))):
            continue
        if not args.all and v["state"] not in (0x3f, 0x3e, 0x7f) and not pat:
            continue
        a = v["attrs"]
        flags = "".join(c for b, c in ((0, "N"), (1, "B"), (2, "R"), (5, "T")) if a >> b & 1)
        print("  %06x %-26s %-4s %6d  %s  %s" % (
            v["off"], VAR_STATES.get(v["state"], "0x%02x" % v["state"]), flags, v["size"],
            guid_str(v["guid"]), v["name"]))
    return 0


# --- vboot: cosa ha deciso verstage in questo avvio ----------------------------
#
# La workbuf di vboot resta in CBMEM ("VBOOT WORK", id 0x78007343): all'inizio
# vb2_shared_data (vboot_reference, 2struct.h), con dentro vb2_context
# (2context.h). Si legge con: sudo cbmem -r 0x78007343 > vbwork.bin
VB2_SD_MAGIC = 0x44533256
VB2_CONTEXT_FLAGS = ((0, "NVDATA_CHANGED"), (2, "RECOVERY_MODE"), (6, "FW_SLOT_B"),
                     (14, "S3_RESUME"), (18, "NVDATA_V2"), (29, "SLOT_A_ONLY"))
VB2_SD_STATUS = ((0, "NV_REINIT"), (1, "NV_INIT"), (3, "CHOSE_SLOT"), (7, "RECOVERY_DECIDED"))


def cmd_vboot_work(args):
    data = open(args.dump, "rb").read()
    if len(data) < 432 or struct.unpack_from("<I", data, 0)[0] != VB2_SD_MAGIC:
        raise Error("%s: non e' la workbuf di vboot (cbmem -r 0x78007343)" % args.dump)
    major, minor = struct.unpack_from("<HH", data, 4)
    flags = struct.unpack_from("<Q", data, 8)[0]
    nv = data[16:32]
    (wsize, wused, sdflags, recovery, last_slot, last_result, fw_slot, _, _, status) = \
        struct.unpack_from("<10I", data, 392)
    slot = "AB"
    print("workbuf:    vb2_shared_data %d.%d, %d byte usati" % (major, minor, wused))
    print("contesto:   %s" % (" ".join(n for b, n in VB2_CONTEXT_FLAGS if flags >> b & 1) or "-"))
    print("stato:      %s" % (" ".join(n for b, n in VB2_SD_STATUS if status >> b & 1) or "-"))
    print("slot:       %s (precedente: slot %s, %s)" % (
        slot[fw_slot & 1], slot[last_slot & 1], NV_RESULTS[last_result & 3]))
    if recovery:
        print("recovery:   motivo 0x%02x" % recovery)
    ok = nv[0] & 0xc3 == 0x40 and nv_crc8(nv[:15]) == nv[15]
    b1, b2 = nv[1], nv[7]
    print("nvdata:     %s  prossimo slot %s, %d tentativi; corrente slot %s %s; precedente slot %s %s%s" % (
        nv.hex(), slot[(b2 >> 3) & 1], b1 & 0x0f, slot[(b2 >> 2) & 1], NV_RESULTS[b2 & 3],
        slot[(b2 >> 6) & 1], NV_RESULTS[(b2 >> 4) & 3], "" if ok else " (NON valida)"))
    return 0


def main():
    ap = argparse.ArgumentParser(description="Aggiornamento del firmware del W541 con una capsula UEFI")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("status", help="firmware, versione, ultimo aggiornamento, capsula in attesa")
    p.add_argument("--esp", help="dove e' montata l'ESP (default: la si cerca)")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("stage", help="prepara una capsula per il prossimo avvio")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("capsule", nargs="?", help="file .cap (o .cab) di una release")
    g.add_argument("--latest", action="store_true", help="scarica l'ultima release della variante che gira")
    p.add_argument("--pre", action="store_true", help="con --latest anche le pre-release")
    p.add_argument("--allow-older", action="store_true", help="anche la stessa versione o una piu' vecchia")
    p.add_argument("--on-battery", action="store_true", help="anche senza alimentatore")
    p.add_argument("--cert", help="certificato per verificare la firma (default: keys/capsule-signing.pem)")
    p.add_argument("--esp", help="dove e' montata l'ESP (default: la si cerca)")
    p.add_argument("--reboot", action="store_true", help="riavvia subito")
    p.set_defaults(func=cmd_stage)

    p = sub.add_parser("cancel", help="toglie la capsula in attesa")
    p.add_argument("--all", action="store_true", help="anche i file di altri strumenti (fwupd)")
    p.add_argument("--esp", help="dove e' montata l'ESP (default: la si cerca)")
    p.set_defaults(func=cmd_cancel)

    num = lambda s: int(s, 0)
    p = sub.add_parser("info", help="mostra e controlla una capsula o un .cab (anche tools/build.sh)")
    p.add_argument("capsule")
    p.add_argument("--same-as", help="la capsula deve essere questo file (per un .cab)")
    p.add_argument("--cert", help="verifica la firma con questo certificato")
    p.add_argument("--rom", help="l'immagine nella capsula deve essere questa")
    p.add_argument("--guid", help="GUID atteso")
    p.add_argument("--fw-version", type=num, help="versione attesa (0x01020000)")
    p.add_argument("--lsv", type=num, help="versione minima attesa")
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("vboot", help="lo stato di vboot (slot, tentativi) in una copia del flash")
    p.add_argument("dump", help="flashrom -r: tutto il flash, o -i RW_NVRAM:FILE")
    p.add_argument("--all", action="store_true", help="anche tutti i record, dal primo")
    p.add_argument("--image", help="confronta RO e slot con una release (.rom, .cap o .cab)")
    p.set_defaults(func=cmd_vboot)

    p = sub.add_parser("rollback", help="torna alla release nell'altro slot, quella di prima dell'ultimo aggiornamento")
    p.add_argument("dump", help="sudo flashrom -p internal -r FILE: tutto il flash, preso in questo avvio")
    p.add_argument("--capsule", help="la capsula di quella release (default: la si scarica da GitHub)")
    p.add_argument("--dry-run", action="store_true", help="mostra cosa farebbe, senza preparare niente")
    p.add_argument("--on-battery", action="store_true", help="anche senza alimentatore")
    p.add_argument("--cert", help="certificato per verificare la firma (default: keys/capsule-signing.pem)")
    p.add_argument("--esp", help="dove e' montata l'ESP (default: la si cerca)")
    p.add_argument("--reboot", action="store_true", help="riavvia subito")
    p.set_defaults(func=cmd_rollback)

    p = sub.add_parser("vars", help="le variabili UEFI di SMMSTORE in una copia del flash (nomi, mai i dati)")
    p.add_argument("dump")
    p.add_argument("--grep", help="solo le variabili con questo nome o GUID (regex)")
    p.add_argument("--all", action="store_true", help="anche quelle cancellate")
    p.set_defaults(func=cmd_vars)

    p = sub.add_parser("vboot-work", help="cosa ha deciso vboot in questo avvio (sudo cbmem -r 0x78007343 > FILE)")
    p.add_argument("dump")
    p.set_defaults(func=cmd_vboot_work)

    args = ap.parse_args()
    try:
        return args.func(args)
    except Error as e:
        print("errore: %s" % e, file=sys.stderr)
        return 1
    except OSError as e:
        print("errore: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
