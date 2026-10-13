#!/usr/bin/env python3
# qemu-update.py - la prova dell'aggiornamento con le capsule, in QEMU q35:
# vboot con due slot RW e RW_NVRAM come sul W541, il payload EDK2 con le
# patch del progetto (FmpDeviceSmmLib, capsule su disco, conferma dello
# slot) e il codice comune di coreboot che scrive il flash (SMMSTORE).
# tools/build.sh update-test prepara tre ROM per q35 (A, B, C, tre versioni),
# un payload DEBUG che si fida di una chiave di prova creata per la prova, e
# le loro capsule, fatte come quelle delle release.
#
#   python3 tools/qemu-update.py [--out DIR] [--timeout S] A.cap B.cap C.cap
#
# Il flash parte dalla ROM della capsula A e resta lo stesso da un avvio
# all'altro. QEMU esce a ogni reset (-no-reboot) e riparte da spento, come
# dopo un ciclo di alimentazione. Una capsula va in \EFI\UpdateCapsule di
# una partizione di sistema EFI, con il bit "capsule su disco" di
# OsIndications, come la mette tools/update.py stage. In ordine:
#
#   1. ROM A: vboot avvia lo slot A
#   2. capsula B: il payload scrive lo slot B, che non gira, e RW_MRC_CACHE,
#      poi chiede a vboot di provare B per due avvii e riavvia. Restano la
#      parte RO, lo slot A e le variabili UEFI; la capsula sparisce dall'ESP,
#      il bit da OsIndications. FmpState (l'ultimo tentativo dell'ESRT):
#      la versione di B, riuscito
#   3. vboot avvia B e il payload lo conferma
#   4. capsula C: scritto lo slot A, che ora non gira; vboot avvia A (C)
#   5. capsula B, cioe' la release dell'altro slot (update.py rollback): con
#      la traccia del pflash di QEMU, nessun byte scritto negli slot; vboot
#      passa a B
#   6. capsula A, poi un byte dello slot A cambiato nel flash: vboot non lo
#      verifica, riavvia e torna a B, e segna A fallito
#   7. capsula A di nuovo, poi due avvii interrotti appena vboot sceglie A:
#      finiti i tentativi, vboot torna a B
# A ogni avvio fino al payload, l'ESRT che il payload stampa (build DEBUG):
# il firmware di sistema con la versione dello slot che gira, e l'ultimo
# tentativo.
#
# Le ROM non hanno grafica: in QEMU (TCG) la barra di avanzamento disegnata
# nel framebuffer rende cinque volte piu' lento l'avvio con una capsula, e la
# grafica del payload la prova tools/qemu-smoke.py. Seriale di coreboot e del payload
# e un riassunto in DIR; il flash finale solo se la prova fallisce. Solo la
# libreria standard di Python, qemu-system-x86_64 e mtools (l'ESP).
import argparse
import importlib.util
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import uuid

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import update  # noqa: E402 (tools/update.py)


def load_smoke():
    """tools/qemu-smoke.py, per la macchina QEMU e il suo QMP."""
    spec = importlib.util.spec_from_file_location("qemu_smoke", os.path.join(HERE, "qemu-smoke.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


smoke = load_smoke()
Fail = smoke.Fail

EFI_GLOBAL = uuid.UUID("8be4df61-93ca-11d2-aa0d-00e098032b8c").bytes_le
# Una variabile di prova, mai usata dal firmware: scritta prima del primo
# aggiornamento, deve esserci ancora alla fine (le capsule non toccano SMMSTORE)
MARK_NAME = "OuroUpdateTest"
MARK_GUID = uuid.UUID("5e7c0a3d-41b2-4f6a-9c8e-2d1f0b7a6e94").bytes_le
MARK_DATA = b"ouroBIOS"
VAR_ADDED, VAR_DELETED = 0x3F, 0xFD
VAR_VALID = (0x3F, 0x3E)
NV_UNKNOWN, NV_TRYING, NV_SUCCESS, NV_FAILURE = 0, 1, 2, 3
SLOTS = "AB"

ESP_OFFSET = 1 << 20            # la partizione a 1 MiB, come la fanno gli installer
ESP_SECTORS = 65536             # 32 MiB: FAT16
MTOOLS_ENV = dict(os.environ, MTOOLS_SKIP_CHECK="1")


def run(cmd):
    r = subprocess.run(cmd, env=MTOOLS_ENV, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if r.returncode != 0:
        raise Fail("%s: %s" % (" ".join(cmd[:2]), r.stdout.strip()))
    return r.stdout


class Esp:
    """Un disco con una sola partizione, di sistema EFI (MBR, tipo 0xEF),
    FAT16 con \\EFI\\UpdateCapsule: dove tools/update.py mette le capsule."""

    def __init__(self, path):
        self.path = path
        part = path + ".part"
        run(["mformat", "-C", "-i", part, "-T", str(ESP_SECTORS), "-h", "64", "-s", "32", "-v", "ESP", "::"])
        run(["mmd", "-i", part, "::/EFI", "::/EFI/UpdateCapsule"])
        mbr = bytearray(512)
        mbr[446:462] = struct.pack("<B3sB3sII", 0, b"\xfe\xff\xff", 0xEF, b"\xfe\xff\xff",
                                   ESP_OFFSET // 512, ESP_SECTORS)
        mbr[510:512] = b"\x55\xaa"
        with open(path, "wb") as f, open(part, "rb") as p:
            f.write(mbr)
            f.write(bytes(ESP_OFFSET - len(mbr)))
            shutil.copyfileobj(p, f)
        os.remove(part)

    def image(self):
        return "%s@@%d" % (self.path, ESP_OFFSET)

    def put(self, src, name):
        run(["mcopy", "-o", "-i", self.image(), src, "::/EFI/UpdateCapsule/" + name])

    def files(self):
        out = run(["mdir", "-b", "-i", self.image(), "::/EFI/UpdateCapsule"])
        return [line.rsplit("/", 1)[-1] for line in out.split("\n") if line.strip()]

    def qemu_args(self):
        return ["-drive", "file=%s,format=raw,if=none,id=esp" % self.path,
                "-device", "ide-hd,drive=esp,bus=ide.0"]


class Flash:
    """Il flash emulato, letto e scritto con QEMU spento."""

    def __init__(self, path):
        self.path = path
        self.areas = update.fmap_areas(self.data())
        need = ("WP_RO", "RW_SECTION_A", "RW_SECTION_B", "FW_MAIN_A", "FW_MAIN_B", "RW_NVRAM",
                "RW_MRC_CACHE", "SMMSTORE")
        if not self.areas or any(n not in self.areas for n in need):
            raise Fail("la ROM non ha la mappa della prova (%s)" % ", ".join(need))

    def data(self):
        with open(self.path, "rb") as f:
            return f.read()

    def region(self, name, data=None):
        off, size = self.areas[name]
        return (data if data is not None else self.data())[off:off + size]

    def write(self, off, data):
        with open(self.path, "r+b") as f:
            f.seek(off)
            f.write(data)

    def fill(self, name, byte):
        off, size = self.areas[name]
        self.write(off, bytes([byte]) * size)

    def nv(self):
        return update.nv_state(self.region("RW_NVRAM"))

    def store(self):
        region = self.region("SMMSTORE")
        found, info = update.var_store(region)
        return region, found, info

    def var(self, name, guid):
        region, found, _ = self.store()
        valid = [v for v in found if v["name"] == name and v["guid"] == guid and v["state"] in VAR_VALID]
        if not valid:
            return None
        v = valid[-1]
        return region[v["data"]:v["data"] + v["size"]]

    def set_var(self, name, guid, attrs, value):
        """Scrive una variabile nello store come il driver delle variabili:
        la nuova in fondo, le copie di prima cancellate."""
        soff = self.areas["SMMSTORE"][0]
        _, found, info = self.store()
        if not info["auth"]:
            raise Fail("SMMSTORE: lo store non e' quello autenticato del payload")
        name16 = (name + "\0").encode("utf-16-le")
        rec = struct.pack("<HBBIQ16sIII16s", 0x55AA, VAR_ADDED, 0, attrs, 0, bytes(16), 0,
                          len(name16), len(value), guid) + name16 + value
        if info["free"] < len(rec) + 4:
            raise Fail("SMMSTORE: niente spazio per %s" % name)
        self.write(soff + info["next"], rec)
        for v in found:
            if v["name"] == name and v["guid"] == guid and v["state"] in VAR_VALID:
                self.write(soff + v["off"] + 2, bytes([v["state"] & VAR_DELETED]))

    def os_indications(self):
        raw = self.var("OsIndications", EFI_GLOBAL)
        return struct.unpack("<Q", raw)[0] if raw and len(raw) == 8 else 0

    def fmp_state(self, guid):
        """FmpState di FmpDxe (FMP_CONTROLLER_STATE): {version, lsv, status, attempt},
        None per i campi non validi."""
        raw = self.var("FmpState", guid)
        if raw is None or len(raw) < 20:
            return None
        flags = struct.unpack_from("<4B", raw)
        vals = struct.unpack_from("<4I", raw, 4)
        return {k: (v if f else None) for k, f, v in zip(("version", "lsv", "status", "attempt"), flags, vals)}


# Una riga dell'ESRT che EsrtFmpDxe stampa al ReadyToBoot (build DEBUG):
# GUID, tipo, versione, minima, flag, ultimo tentativo (versione, stato)
ESRT_ROW = re.compile(r"\| ([0-9A-F]{8}(?:-[0-9A-F]{4}){3}-[0-9A-F]{12}) \| (\S[^|]*?)\s*\|"
                      r" 0x\s*([0-9A-F]+) \| 0x\s*([0-9A-F]+) \| 0x\s*([0-9A-F]+) \| 0x\s*([0-9A-F]+)"
                      r" \| 0x\s*([0-9A-F]+) \|")
TRACE_RE = re.compile(r"^(pflash_data_write(?:_block)?|pflash_write_block_erase) \S+ .*?offset:0x([0-9a-f]+)"
                      r"(?: size:(\d+)| bytes:0x([0-9a-f]+))", re.M)


def traced_writes(path, areas, names):
    """Dalla traccia del pflash di QEMU: quanti byte scritti o cancellati
    in ciascuna delle aree names, e quante righe di scrittura in tutto."""
    hits = dict.fromkeys(names, 0)
    total = 0
    with open(path, errors="replace") as f:
        for m in TRACE_RE.finditer(f.read()):
            total += 1
            off = int(m.group(2), 16)
            size = int(m.group(3)) if m.group(3) else int(m.group(4), 16)
            for n in names:
                a, s = areas[n]
                if off < a + s and off + size > a:
                    hits[n] += size
    return hits, total


class UpdateTest:
    # Il payload ha pubblicato l'ESRT (ReadyToBoot), il disco non ha un
    # sistema da avviare e il payload aspetta un tasto: nessuna scrittura del
    # flash in corso, si puo' spegnere
    IDLE = "Press any key to continue"

    def __init__(self, caps, out, timeout):
        self.paths, self.out, self.timeout = caps, out, timeout
        self.tmp = self.m = None
        self.steps = []

    def setup(self):
        """Le capsule, il flash dalla ROM della capsula A, l'ESP."""
        self.caps = {}
        for key, path in zip("ABC", self.paths):
            _, cap = update.load_capsule(path)
            if cap["regions"] != update.REGIONS:
                raise Fail("%s: la capsula scrive %s, non %s come quelle delle release"
                           % (path, cap["regions"], update.REGIONS))
            self.caps[key] = {"path": path, "cap": cap, "image": cap["image"]}
        guids = {c["cap"]["type_id"] for c in self.caps.values()}
        if len(guids) != 1:
            raise Fail("le tre capsule non sono dello stesso firmware (GUID)")
        self.guid = guids.pop().bytes_le
        self.tmp = tempfile.mkdtemp(prefix="qemu-update-")
        rom = os.path.join(self.tmp, "start.rom")
        with open(rom, "wb") as f:
            f.write(self.caps["A"]["image"])
        self.m = smoke.Smoke(rom, self.out, self.timeout)
        self.flash = Flash(self.m.flash)
        for c in self.caps.values():
            if update.fmap_areas(c["image"]) != self.flash.areas:
                raise Fail("%s: un'altra mappa del flash" % c["path"])
            # il nome nel banner di coreboot (CONFIG_LOCALVERSION): dice quale
            # firmware gira, dalla romstage dello slot
            tags = set(re.findall(rb"ouroBIOS-update-test-[A-Z]", self.flash.region("FW_MAIN_A", c["image"])))
            if len(tags) != 1:
                raise Fail("%s: non e' una ROM di build.sh update-test" % c["path"])
            c["tag"] = tags.pop().decode()
        self.ro = self.flash.region("WP_RO")
        self.esp = Esp(os.path.join(self.tmp, "esp.img"))
        self.t0 = time.monotonic()

    def close(self, keep_flash=False):
        """QEMU spento, i file temporanei via; con keep_flash il flash in DIR."""
        if self.m is not None:
            self.m.halt()
            if keep_flash and os.path.exists(self.m.flash):
                shutil.copyfile(self.m.flash, os.path.join(self.out, "flash-fine.rom"))
            self.m.close()
        if self.tmp:
            shutil.rmtree(self.tmp, ignore_errors=True)

    def ok(self, text):
        self.steps.append("%3.0f s  %s" % (time.monotonic() - self.t0, text))
        print("  ok  %s" % self.steps[-1], flush=True)

    # --- gli avvii ------------------------------------------------------------

    def boot(self, tag, trace=None, cut_at=None):
        """Un avvio fino al payload che, pubblicato l'ESRT e fallito l'avvio
        dal disco, aspetta un tasto ("booted"), fino a un reset (QEMU esce:
        "reset") o, con cut_at, fino alla riga della seriale dopo cui si
        toglie la corrente ("cut"). Torna (come e' finito, seriale)."""
        extra = self.esp.qemu_args()
        if trace:
            # i byte scritti uno a uno e quelli del buffer di scrittura (0xE8),
            # le cancellazioni dei blocchi
            extra += ["-trace", "pflash_data_write*", "-trace", "pflash_write_block_erase", "-D", trace]
        self.m.boot(tag, extra)
        end = time.monotonic() + self.timeout
        while True:
            time.sleep(0.3)
            text = self.m.serial_text()
            if self.m.proc.poll() is not None:
                self.m.halt()
                return "reset", self.m.serial_text()
            if cut_at and cut_at in text:
                self.m.cut()
                return "cut", self.m.serial_text()
            if self.IDLE in text:
                self.m.halt()
                return "booted", self.m.serial_text()
            if time.monotonic() > end:
                self.m.halt()
                raise Fail("%s: dopo %d s ne' il payload ne' un reset (serial-%s.log)"
                           % (tag, self.timeout, tag))

    def expect(self, tag, how, text, want):
        if how != want:
            what = {"reset": "un reset", "booted": "il payload fino all'avvio dal disco",
                    "cut": "l'interruzione"}
            raise Fail("%s: atteso %s, invece %s (serial-%s.log)" % (tag, what[want], what[how], tag))

    def running(self, tag, text, slot, key):
        """Nella seriale: vboot ha scelto slot e la romstage e' quella della ROM key."""
        sel = re.findall(r"Slot ([AB]) is selected", text)
        if sel != [slot]:
            raise Fail("%s: vboot ha scelto %s, atteso lo slot %s" % (tag, sel or "nessuno slot", slot))
        tags = re.findall(r"(ouroBIOS-update-test-[A-Z])\S* .*romstage starting", text)
        if tags != [self.caps[key]["tag"]]:
            raise Fail("%s: romstage %s, attesa quella di %s" % (tag, tags or "assente", self.caps[key]["tag"]))

    def confirmed(self, tag, text, slot, key, trying, last):
        """Un avvio arrivato al payload, dallo slot giusto. Se vboot lo stava
        provando (trying), il payload lo deve aver confermato; se no vboot lo
        avvia senza prova (risultato "-") e non c'e' niente da confermare.
        Nell'ESRT il firmware di sistema con la versione della ROM key e
        l'ultimo tentativo last (versione della capsula, stato)."""
        self.running(tag, text, slot, key)
        m = re.search(r"Capsules: firmware slot marked good: (\S+)", text)
        if not m or m.group(1) != "Success":
            raise Fail("%s: il payload non ha mandato la conferma dello slot (%s)" % (tag, m.group(1) if m else "-"))
        st = self.flash.nv()
        want = NV_SUCCESS if trying else NV_UNKNOWN
        if not st["valid"] or SLOTS[st["tried"]] != slot or st["result"] != want:
            raise Fail("%s: RW_NVRAM %s, atteso lo slot %s %s" % (tag, nv_str(st), slot, update.NV_RESULTS[want]))
        rows = [r for r in ESRT_ROW.findall(text) if uuid.UUID(r[0]).bytes_le == self.guid]
        if len(rows) != 1:
            raise Fail("%s: nell'ESRT %d righe del firmware, attesa una" % (tag, len(rows)))
        kind, ver, lsv, _, aver, astat = rows[0][1], *(int(x, 16) for x in rows[0][2:])
        cap = self.caps[key]["cap"]
        got = (kind, ver, lsv, aver, astat)
        exp = ("System FW", cap["version"], cap["lsv"]) + tuple(last)
        if got != exp:
            raise Fail("%s: ESRT %s %s minima %s, ultimo %s stato 0x%x; attesi %s %s minima %s, ultimo %s stato 0x%x"
                       % ((tag, kind, update.version_str(ver), update.version_str(lsv), update.version_str(aver), astat)
                          + (exp[0], update.version_str(exp[1]), update.version_str(exp[2]),
                             update.version_str(exp[3]), exp[4])))

    def stage(self, key):
        """La capsula nell'ESP, il bit in OsIndications, RW_MRC_CACHE pieno:
        la capsula lo deve riscrivere."""
        self.esp.put(self.caps[key]["path"], "ouroBIOS-%s.cap" % key)
        self.flash.set_var("OsIndications", EFI_GLOBAL, update.VAR_ATTRS,
                           struct.pack("<Q", self.flash.os_indications() | update.FILE_CAPSULE_DELIVERY))
        self.flash.fill("RW_MRC_CACHE", 0x5A)

    def capsule_boot(self, tag, key, running, trace=None):
        """Un avvio con la capsula key mentre gira lo slot running: il payload
        scrive solo l'altro slot, chiede a vboot di provarlo e riavvia."""
        before = self.flash.data()
        target = "B" if running == "A" else "A"
        how, text = self.boot(tag, trace=trace)
        self.expect(tag, how, text, "reset")
        if not re.search(r"Slot %s is selected" % running, text):
            raise Fail("%s: la capsula non e' partita dallo slot %s" % (tag, running))
        for want in ("Capsules: ouroBIOS-%s.cap loaded" % key,
                     "vboot runs slot %s, updating slot %s" % (running, target),
                     "Capsules: capsule 1 of 1: Success"):
            if want not in text:
                raise Fail("%s: nella seriale manca \"%s\"" % (tag, want))
        after = self.flash.data()
        image = self.caps[key]["image"]
        if self.flash.region("WP_RO", after) != self.ro:
            raise Fail("%s: la parte RO e' cambiata" % tag)
        if self.flash.region("RW_SECTION_" + running, after) != self.flash.region("RW_SECTION_" + running, before):
            raise Fail("%s: scritto lo slot %s, quello che gira" % (tag, running))
        if self.flash.region("RW_SECTION_" + target, after) != self.flash.region("RW_SECTION_" + target, image):
            raise Fail("%s: lo slot %s non e' quello della capsula %s" % (tag, target, key))
        if self.flash.region("RW_MRC_CACHE", after) != self.flash.region("RW_MRC_CACHE", image):
            raise Fail("%s: RW_MRC_CACHE non riscritto dalla capsula" % tag)
        st = self.flash.nv()
        if not st["valid"] or SLOTS[st["try_next"]] != target or st["try_count"] != 2:
            raise Fail("%s: RW_NVRAM %s, atteso: prossimo lo slot %s, 2 tentativi" % (tag, nv_str(st), target))
        if self.esp.files():
            raise Fail("%s: la capsula e' ancora nell'ESP: %s" % (tag, self.esp.files()))
        if self.flash.os_indications() & update.FILE_CAPSULE_DELIVERY:
            raise Fail("%s: il bit delle capsule su disco e' ancora in OsIndications" % tag)
        fmp = self.flash.fmp_state(self.guid)
        want = self.caps[key]["cap"]["version"]
        if not fmp or fmp["status"] != 0 or fmp["attempt"] != want:
            raise Fail("%s: FmpState %s, atteso l'ultimo tentativo %s riuscito"
                       % (tag, fmp, update.version_str(want)))
        return text

    # --- la prova -------------------------------------------------------------

    def run(self):
        self.setup()
        f = self.flash
        v = {k: c["cap"]["version"] for k, c in self.caps.items()}
        # 1. la ROM A
        how, text = self.boot("1-A")
        self.expect("1-A", how, text, "booted")
        self.confirmed("1-A", text, "A", "A", trying=False, last=(0, 0))
        f.set_var(MARK_NAME, MARK_GUID, update.VAR_ATTRS, MARK_DATA)
        self.ok("ROM A: vboot avvia lo slot A; nell'ESRT il firmware di sistema %s, nessun aggiornamento"
                % update.version_str(v["A"]))

        # 2-3. capsula B: lo slot B, poi B confermato
        self.stage("B")
        self.capsule_boot("2-capsula-B", "B", "A")
        self.ok("capsula B: scritto lo slot B (non quello che gira) e RW_MRC_CACHE, RO e slot A "
                "uguali, capsula tolta dall'ESP, OsIndications spento, 2 tentativi per B")
        how, text = self.boot("3-B")
        self.expect("3-B", how, text, "booted")
        self.confirmed("3-B", text, "B", "B", trying=True, last=(v["B"], 0))
        self.ok("vboot prova lo slot B, il payload lo conferma; ESRT %s, ultimo tentativo riuscito"
                % update.version_str(v["B"]))

        # 4. capsula C: ora si scrive lo slot A
        self.stage("C")
        self.capsule_boot("4-capsula-C", "C", "B")
        how, text = self.boot("4-C")
        self.expect("4-C", how, text, "booted")
        self.confirmed("4-C", text, "A", "C", trying=True, last=(v["C"], 0))
        self.ok("capsula C: scritto lo slot A, che ora non girava; vboot avvia C dallo slot A")

        # 5. capsula B: la release dell'altro slot, nessun blocco da scrivere
        self.stage("B")
        trace = os.path.join(self.tmp, "pflash.trace")
        self.capsule_boot("5-ritorno-B", "B", "A", trace=trace)
        hits, total = traced_writes(trace, f.areas, ("WP_RO", "RW_SECTION_A", "RW_SECTION_B"))
        if total == 0:
            raise Fail("5-ritorno-B: la traccia del pflash di QEMU e' vuota (-trace non supportato?)")
        if any(hits.values()):
            raise Fail("5-ritorno-B: scritti %s" % ", ".join("%d byte in %s" % (n, a) for a, n in hits.items() if n))
        os.remove(trace)
        how, text = self.boot("5-B")
        self.expect("5-B", how, text, "booted")
        self.confirmed("5-B", text, "B", "B", trying=True, last=(v["B"], 0))
        self.ok("capsula B con B gia' nell'altro slot (rollback): %d scritture del pflash, nessuna "
                "negli slot o nella parte RO; vboot torna a B" % total)

        # 6. capsula A, poi lo slot A rovinato: vboot lo scarta e resta su B
        self.stage("A")
        self.capsule_boot("6-capsula-A", "A", "B")
        off = f.areas["FW_MAIN_A"][0] + 0x100000
        f.write(off, bytes([f.data()[off] ^ 0xFF]))
        how, text = self.boot("6-A-rovinato")
        self.expect("6-A-rovinato", how, text, "reset")
        if "vboot: reboot requested" not in text or "Slot A is selected" in text:
            raise Fail("6-A-rovinato: vboot non ha scartato lo slot A rovinato")
        how, text = self.boot("6-B")
        self.expect("6-B", how, text, "booted")
        # la capsula A e' stata scritta, l'ESRT lo dice; gira ancora B
        self.confirmed("6-B", text, "B", "B", trying=False, last=(v["A"], 0))
        st = f.nv()
        if SLOTS[st["prev_tried"]] != "A" or st["prev_result"] != NV_FAILURE:
            raise Fail("6-B: RW_NVRAM %s, atteso lo slot A fallito" % nv_str(st))
        self.ok("slot A scritto e poi rovinato nel flash: vboot non lo verifica, riavvia, torna a B "
                "e segna A fallito")

        # 7. capsula A di nuovo, poi due avvii interrotti: vboot torna a B
        self.stage("A")
        self.capsule_boot("7-capsula-A", "A", "B")
        for i, left in ((1, 1), (2, 0)):
            tag = "7-interrotto-%d" % i
            how, text = self.boot(tag, cut_at="Slot A is selected")
            self.expect(tag, how, text, "cut")
            st = f.nv()
            if SLOTS[st["tried"]] != "A" or st["result"] != NV_TRYING or st["try_count"] != left:
                raise Fail("%s: RW_NVRAM %s, atteso lo slot A in prova, %d tentativi" % (tag, nv_str(st), left))
        how, text = self.boot("7-B")
        self.expect("7-B", how, text, "booted")
        self.confirmed("7-B", text, "B", "B", trying=False, last=(v["A"], 0))
        if "try_count used up; falling back to slot B" not in text:
            raise Fail("7-B: vboot non dice di aver finito i tentativi dello slot A")
        st = f.nv()
        if SLOTS[st["prev_tried"]] != "A" or st["prev_result"] != NV_TRYING:
            raise Fail("7-B: RW_NVRAM %s, atteso lo slot A rimasto in prova" % nv_str(st))
        self.ok("capsula A, due avvii interrotti dopo verstage: finiti i tentativi, vboot torna a B")

        # alla fine
        if f.region("WP_RO") != self.ro:
            raise Fail("la parte RO e' cambiata")
        if f.var(MARK_NAME, MARK_GUID) != MARK_DATA:
            raise Fail("la variabile %s scritta all'inizio non c'e' piu'" % MARK_NAME)
        self.ok("dopo 5 capsule: la parte RO e' quella della ROM A, la variabile UEFI scritta prima "
                "degli aggiornamenti c'e' ancora")
        return self.steps


def nv_str(st):
    if not st.get("valid"):
        return "nessun record valido"
    return "prossimo %s (%d tentativi), corrente %s %s, precedente %s %s" % (
        SLOTS[st["try_next"]], st["try_count"], SLOTS[st["tried"]], update.NV_RESULTS[st["result"]],
        SLOTS[st["prev_tried"]], update.NV_RESULTS[st["prev_result"]])


def main():
    ap = argparse.ArgumentParser(description="Prova dell'aggiornamento con le capsule (vboot A/B) in QEMU q35")
    ap.add_argument("caps", nargs=3, metavar="CAP", help="le capsule A, B e C (tools/build.sh update-test)")
    ap.add_argument("--out", default="update-test", help="seriale e riassunto (default: update-test/)")
    ap.add_argument("--timeout", type=float, default=300, help="secondi per ogni avvio (default 300)")
    args = ap.parse_args()
    for tool in ("qemu-system-x86_64", "mformat", "mcopy", "mdir", "mmd"):
        if not shutil.which(tool):
            sys.exit("serve %s (Debian/Ubuntu: apt install qemu-system-x86 mtools)" % tool)
    os.makedirs(args.out, exist_ok=True)
    test = UpdateTest(args.caps, args.out, args.timeout)
    passed = False
    try:
        test.run()
        passed = True
    except (Fail, update.Error) as e:
        print("update-test: FALLITO: %s (seriale in %s)" % (e, args.out))
    finally:
        test.close(keep_flash=not passed)
        with open(os.path.join(args.out, "riassunto.txt"), "w") as f:
            f.write("\n".join(test.steps + ([] if passed else ["FALLITO"])) + "\n")
    if not passed:
        return 1
    print("update-test: passato (seriale in %s)" % args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
