#!/usr/bin/env python3
# update.py - aggiorna il firmware del W541 da Linux con una capsula UEFI
# (docs/update.md). Dalla v1.2.0 ogni release ha, per variante, una capsula
# firmata (w541-coreboot-vX.Y.Z-mrc.cap): la si copia in \EFI\UpdateCapsule
# dell'ESP, si accende il bit "capsule su disco" di OsIndications e al
# riavvio il firmware la verifica e la scrive (RW_MRC_CACHE e COREBOOT), anche
# con il BIOS Lock acceso. Variabili UEFI, VPD e registro degli eventi
# restano.
#
#   sudo python3 tools/update.py status
#   sudo python3 tools/update.py stage --latest        # ultima release della variante che gira
#   sudo python3 tools/update.py stage w541-coreboot-v1.2.1-mrc.cap
#   sudo python3 tools/update.py cancel
#   python3 tools/update.py info w541-coreboot-v1.2.1-mrc.cap
#
# Il firmware che gira deve essere una release dalla v1.2.0 in su: la prima
# volta si scrive con flashrom (docs/flashing.md). Il firmware applica solo
# capsule della sua variante (mrc o nri, un GUID ciascuna nell'ESRT), firmate
# dalla chiave di keys/capsule-signing.pem e non piu' vecchie della versione
# minima (LSV); qui si controlla prima, per non riavviare per niente.
#
# Solo la libreria standard di Python; openssl, se c'e', verifica anche la
# firma (con --cert, o keys/capsule-signing.pem accanto a questo script).
import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import urllib.request
import uuid

REPO = "debianita22/w541-coreboot-env"

# Il GUID dell'ESRT (DRIVERS_EFI_MAIN_FW_GUID nei defconfig) di ogni variante
VARIANTS = {
    "mrc": uuid.UUID("b774aa46-be0e-4ec1-bb1b-b7c0bf2722aa"),
    "nri": uuid.UUID("d945239f-4a5a-4089-8597-6bd72edd1a1f"),
}
# Le regioni che scrive una capsula (CAPSULE_REGIONS di tools/build.sh)
REGIONS = ["RW_MRC_CACHE", "COREBOOT"]
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

# LastAttemptStatus dell'ESRT (UEFI 2.10, 23.4)
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
}


class Error(Exception):
    pass


def version_str(v):
    """La versione dell'ESRT come quella delle release: (X << 24) | (Y << 16) | Z."""
    return "v%d.%d.%d" % (v >> 24, (v >> 16) & 0xFF, v & 0xFFFF)


def attempt_str(status):
    if status in ATTEMPT:
        return ATTEMPT[status]
    if 0x1000 <= status <= 0x4000:
        return "errore 0x%x (FmpDxe o FmpDeviceSmmLib)" % status
    return "errore 0x%x" % status


def variant_of(guid):
    for name, g in VARIANTS.items():
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


def load_capsule(path):
    with open(path, "rb") as f:
        data = f.read()
    try:
        return data, parse_capsule(data)
    except Error as e:
        raise Error("%s: %s" % (path, e))


def check_capsule(cap, variant=None):
    """Quello che il firmware controllerebbe a parte la firma."""
    if cap["variant"] is None:
        raise Error("capsula per un firmware sconosciuto (%s)" % cap["type_id"])
    if variant and cap["variant"] != variant:
        raise Error("capsula della variante %s, qui gira la %s: per cambiare variante si usa "
                    "flashrom (docs/flashing.md)" % (cap["variant"], variant))
    if cap["regions"] != REGIONS:
        raise Error("la capsula scrive %s, attese %s" % (cap["regions"] or "tutto il flash", REGIONS))
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
        if name is None:
            continue
        num = lambda f: int(read_text(os.path.join(d, f)) or "0", 0)
        return {
            "variant": name,
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
            os.write(fd, struct.pack("<IQ", attrs, value))
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
    assets = {a["name"]: a["browser_download_url"] for a in best.get("assets", [])}
    cap_name = "w541-coreboot-%s.cap" % best["tag_name"]
    if cap_name not in assets or "SHA256SUMS" not in assets:
        raise Error("%s: senza %s o SHA256SUMS (le capsule ci sono dalla v1.2.0)"
                    % (best["tag_name"], cap_name))
    print("release %s: scarico %s" % (best["tag_name"], cap_name))
    sums = http_get(assets["SHA256SUMS"]).decode()
    want = None
    for line in sums.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == cap_name:
            want = parts[0].lower()
    if not want:
        raise Error("SHA256SUMS di %s senza %s" % (best["tag_name"], cap_name))
    data = http_get(assets[cap_name])
    if hashlib.sha256(data).hexdigest() != want:
        raise Error("%s: SHA-256 diverso da SHA256SUMS, download corrotto" % cap_name)
    path = os.path.join(dest, cap_name)
    with open(path, "wb") as f:
        f.write(data)
    return path


# --- i comandi ---------------------------------------------------------------

def need_root():
    if os.geteuid() != 0:
        raise Error("serve root (sudo): si leggono e scrivono variabili UEFI e l'ESP")


def need_firmware():
    if not os.path.isdir(EFIVARS):
        raise Error("niente %s: Linux non e' partito in UEFI" % EFIVARS)
    fw = esrt_entry()
    if fw is None or not cod_supported():
        raise Error("il firmware che gira non applica capsule (e' piu' vecchio della v1.2.0?): "
                    "la prima volta si scrive con flashrom, docs/flashing.md")
    return fw


def print_capsule(path, cap):
    print("capsula:   %s" % path)
    print("firmware:  W541 coreboot, variante %s (%s)" % (cap["variant"] or "?", cap["type_id"]))
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
    cert = args.cert or default_cert()
    if cert and (args.cert or shutil.which("openssl")):
        verify_signature(cap, cert)
        print("firma:     valida per %s" % cert)
    elif args.cert is None:
        print("firma:     non verificata (serve openssl e keys/capsule-signing.pem)")
    return 0


def cmd_status(args):
    need_root()
    fw = esrt_entry()
    bios = read_text(BIOS_VERSION)
    if fw is None:
        print("firmware:  %s, senza aggiornamento con capsula (prima della v1.2.0?)" % (bios or "?"))
        return 0
    print("firmware:  W541 coreboot, variante %s (BIOS %s)" % (fw["variant"], bios or "?"))
    print("versione:  %s, minima per una capsula %s" % (version_str(fw["version"]), version_str(fw["lsv"])))
    if fw["last_version"]:
        print("ultimo:    %s, %s" % (version_str(fw["last_version"]), attempt_str(fw["last_status"])))
    else:
        print("ultimo:    nessun aggiornamento con capsula")
    print("su disco:  %s" % ("supportato" if cod_supported() else "NON supportato da questo firmware"))
    armed = bool(os_indications() & FILE_CAPSULE_DELIVERY)
    try:
        esp = find_esp(args.esp)
    except Error as e:
        print("ESP:       %s" % e)
        return 0
    files = staged_files(esp)
    for f in files:
        try:
            _, cap = load_capsule(f)
            what = "%s %s" % (cap["variant"], version_str(cap["version"]))
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


def cmd_stage(args):
    need_root()
    fw = need_firmware()
    with tempfile.TemporaryDirectory() as tmp:
        path = args.capsule
        if args.latest:
            path = download_latest(fw["variant"], args.pre, tmp)
        data, cap = load_capsule(path)
        print_capsule(path, cap)
        check_capsule(cap, fw["variant"])
        cert = args.cert or default_cert()
        if cert and shutil.which("openssl"):
            verify_signature(cap, cert)
            print("firma:     valida")
        if cap["version"] < fw["lsv"]:
            raise Error("%s e' piu' vecchia della minima %s: il firmware la rifiuterebbe"
                        % (version_str(cap["version"]), version_str(fw["lsv"])))
        if cap["version"] <= fw["version"] and not args.allow_older:
            raise Error("gira gia' la %s: per reinstallare o tornare indietro --allow-older"
                        % version_str(fw["version"]))
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
        with open(dst, "rb") as f:
            if hashlib.sha256(f.read()).digest() != hashlib.sha256(data).digest():
                raise Error("%s: la copia nell'ESP non e' uguale" % dst)
    write_os_indications(os_indications() | FILE_CAPSULE_DELIVERY)
    if not os_indications() & FILE_CAPSULE_DELIVERY:
        raise Error("OsIndications non scritta")
    print("\npronta: %s %s -> %s, si applica al prossimo riavvio." % (
        fw["variant"], version_str(fw["version"]), version_str(cap["version"])))
    print("Il firmware la verifica e la scrive in circa un minuto, con una barra sotto il")
    print("logo: non spegnere. Poi riparte da solo con il firmware nuovo.")
    if args.reboot:
        subprocess.run(["systemctl", "reboot"], check=False)
    else:
        print("  sudo systemctl reboot")
    return 0


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


def main():
    ap = argparse.ArgumentParser(description="Aggiornamento del firmware del W541 con una capsula UEFI")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("status", help="firmware, versione, ultimo aggiornamento, capsula in attesa")
    p.add_argument("--esp", help="dove e' montata l'ESP (default: la si cerca)")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("stage", help="prepara una capsula per il prossimo avvio")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("capsule", nargs="?", help="file .cap di una release")
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
    p = sub.add_parser("info", help="mostra e controlla una capsula (anche tools/build.sh)")
    p.add_argument("capsule")
    p.add_argument("--cert", help="verifica la firma con questo certificato")
    p.add_argument("--rom", help="l'immagine nella capsula deve essere questa")
    p.add_argument("--guid", help="GUID atteso")
    p.add_argument("--fw-version", type=num, help="versione attesa (0x01020000)")
    p.add_argument("--lsv", type=num, help="versione minima attesa")
    p.set_defaults(func=cmd_info)

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
