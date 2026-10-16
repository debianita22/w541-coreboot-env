#!/usr/bin/env python3
# qemu-smoke.py - la prova di fumo del payload: una ROM coreboot per QEMU
# q35 con l'EDK2 del W541 (tools/build.sh smoke) avviata in QEMU, per vedere
# prima di una release che il payload parte, disegna e risponde ai tasti.
#
#   python3 tools/qemu-smoke.py [--out DIR] [--timeout S] ROM
#
# Controlla, in ordine:
#   1. coreboot arriva al payload ("Jumping to boot code" sulla seriale);
#   2. lo schermo non e' piu' di un colore solo (il payload disegna);
#   3. con Esc, premuto durante l'avvio, il payload apre il setup, e lo
#      schermo si ferma su una pagina grafica: non la Shell UEFI, dove il
#      payload arriva senza dischi se Esc non apre il setup;
#   4. Giu' e Invio aprono un'altra pagina, Esc torna indietro: il setup
#      risponde ai tasti;
#   5. QEMU e' ancora vivo: nessun riavvio, nessun triple fault (-no-reboot);
#   6. le variabili UEFI in SMMSTORE: senza dischi il payload arriva alla
#      Shell, dove setvar scrive una variabile di prova; spenta la macchina,
#      la variabile deve essere nello store del flash, con i suoi dati. Al
#      secondo avvio la Shell la riscrive con un altro valore: il driver deve
#      trovare la copia di prima nel flash e cancellarla, e ne deve restare
#      una sola valida;
#   7. iPXE: la voce "iPXE Network Boot" (il suo numero Boot#### letto dallo
#      store) messa in BootNext dalla Shell, poi un avvio con una scheda
#      e1000 sulla rete utente di QEMU, che fa da DHCP e TFTP: iPXE deve
#      prendere l'indirizzo, scaricare lo script del DHCP (opzione 67) ed
#      eseguirlo; lo script ne chiama un altro, e la richiesta TFTP di quel
#      secondo file nel pcap del traffico (filter-dump) e' la prova;
#   8. CSMWrap: la voce "CSMWrap Legacy Boot" in BootNext, poi un avvio con
#      due CPU (CSMWrap ne tiene una per i suoi servizi BIOS) e un disco
#      SATA il cui MBR scrive una riga sulla porta seriale: il BIOS legacy
#      deve avviarlo (INT 13h di SeaBIOS, con il suo driver AHCI).
# Le schermate di ogni passo vanno in DIR (PNG), con la console seriale.
#
# Il W541 non si emula: qui girano il payload (UEFIPayloadPkg, LvglPkg, le
# patch di patches/edk2) e il codice comune di coreboot, non quello della
# board. Solo la libreria standard di Python, e qemu-system-x86_64.
import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid


class Fail(Exception):
    pass


class Qmp:
    def __init__(self, path, timeout=30):
        end = time.monotonic() + timeout
        while True:
            try:
                self.s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self.s.connect(path)
                break
            except OSError:
                self.s.close()
                if time.monotonic() > end:
                    raise Fail("QMP: niente socket %s" % path)
                time.sleep(0.2)
        self.f = self.s.makefile("rwb")
        self.read()                     # il saluto
        self.cmd("qmp_capabilities")

    def read(self):
        line = self.f.readline()
        if not line:
            raise Fail("QMP: QEMU ha chiuso la connessione")
        return json.loads(line)

    def cmd(self, name, **args):
        msg = {"execute": name}
        if args:
            msg["arguments"] = args
        self.f.write(json.dumps(msg).encode() + b"\n")
        self.f.flush()
        while True:
            r = self.read()
            if "return" in r:
                return r["return"]
            if "error" in r:
                raise Fail("QMP %s: %s" % (name, r["error"].get("desc")))
            # gli eventi (RESET, SHUTDOWN...) si saltano qui: li vede alive()


def ppm(path):
    """(larghezza, altezza, byte RGB) di uno screendump PPM (P6)."""
    with open(path, "rb") as f:
        data = f.read()
    fields, pos = [], 0
    while len(fields) < 4:
        while data[pos:pos + 1].isspace():
            pos += 1
        if data[pos:pos + 1] == b"#":
            pos = data.index(b"\n", pos)
            continue
        end = pos
        while not data[end:end + 1].isspace():
            end += 1
        fields.append(data[pos:end])
        pos = end
    if fields[0] != b"P6" or fields[3] != b"255":
        raise Fail("screendump: non e' un PPM P6")
    w, h = int(fields[1]), int(fields[2])
    return w, h, data[pos + 1:pos + 1 + 3 * w * h]


class Screen:
    """Una schermata, campionata: abbastanza per contare i colori e
    confrontarne due senza leggere ogni pixel di un 1920x1080."""

    def __init__(self, w, h, rgb, step=7):
        self.size = (w, h)
        self.px = [rgb[i:i + 3] for i in range(0, len(rgb) - 2, 3 * step)]

    def colors(self):
        return len(set(self.px))

    def diff(self, other):
        if other is None or other.size != self.size:
            return 1.0
        return sum(a != b for a, b in zip(self.px, other.px)) / max(1, len(self.px))


class Smoke:
    """Le macchine QEMU di una prova: una per fase, tutte sulla stessa copia
    della ROM, che il payload scrive (SMMSTORE)."""

    def __init__(self, rom, out, timeout):
        self.out, self.timeout = out, timeout
        self.tmp = tempfile.mkdtemp(prefix="qemu-smoke-")
        self.flash = os.path.join(self.tmp, "flash.rom")
        shutil.copyfile(rom, self.flash)
        self.sock = os.path.join(self.tmp, "qmp.sock")
        self.n = 0
        self.proc = self.qmp = self.log = None

    def boot(self, tag, extra=()):
        """Accende la macchina: extra sono altre opzioni di QEMU (dischi,
        traccia: tools/qemu-update.py). QEMU parte fermo (-S) e la CPU va
        solo dopo il saluto di QMP: un avvio che finisce subito, come un
        reset di vboot nel bootblock, non chiude QEMU prima del socket."""
        self.halt()
        self.serial = os.path.join(self.out, "serial-%s.log" % tag)
        cmd = ["qemu-system-x86_64", "-M", "q35,acpi=off", "-cpu", "Haswell-noTSX,stepping=3",
               "-smp", "1", "-m", "2048", "-drive", "if=pflash,format=raw,file=" + self.flash,
               "-serial", "file:" + self.serial, "-qmp", "unix:%s,server=on,wait=off" % self.sock,
               "-display", "none", "-vga", "std", "-no-reboot", "-S"] + list(extra)
        self.log = open(os.path.join(self.out, "qemu-%s.log" % tag), "w")
        self.proc = subprocess.Popen(cmd, stdout=self.log, stderr=subprocess.STDOUT)
        self.qmp = Qmp(self.sock)
        self.qmp.cmd("cont")

    def cut(self):
        """Toglie la corrente: QEMU ucciso senza chiudere niente. Il flash
        emulato e' quello che il firmware ha scritto fin li' (QEMU scrive
        ogni byte nel file appena il firmware lo programma)."""
        if self.proc is None:
            return
        self.proc.kill()
        self.proc.wait()
        self.log.close()
        self.proc = None

    def halt(self):
        if self.proc is None:
            return
        if self.proc.poll() is None:
            try:
                self.qmp.cmd("quit")
            except (Fail, OSError):
                pass
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        self.log.close()
        self.proc = None

    def close(self):
        self.halt()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def type(self, text):
        """Scrive text sulla tastiera PS/2, maiuscole con Shift."""
        names = {" ": "spc", "-": "minus", "=": "equal", "\n": "ret", ".": "dot", "/": "slash"}
        for ch in text:
            self.alive()
            if ch.isalpha() and ch.isupper():
                keys = ["shift", ch.lower()]
            elif ch.isalnum():
                keys = [ch]
            else:
                keys = [names[ch]]
            self.qmp.cmd("send-key", keys=[{"type": "qcode", "data": k} for k in keys])
            time.sleep(0.08)

    def alive(self):
        if self.proc.poll() is not None:
            raise Fail("QEMU e' uscito (codice %s): un riavvio o un triple fault, con -no-reboot"
                       % self.proc.returncode)

    def serial_text(self):
        try:
            with open(self.serial, "rb") as f:
                return f.read().decode("utf-8", "replace")
        except OSError:
            return ""

    def shot(self, name=None):
        self.alive()
        p = os.path.join(self.tmp, "s.ppm")
        self.qmp.cmd("screendump", filename=p)
        s = Screen(*ppm(p))
        if name:
            self.n += 1
            png = os.path.join(self.out, "%02d-%s.png" % (self.n, name))
            try:
                self.qmp.cmd("screendump", filename=png, format="png")
            except Fail:
                shutil.copyfile(p, png[:-4] + ".ppm")   # QEMU prima della 7.1: niente PNG
        return s

    def key(self, *names):
        for k in names:
            self.alive()
            self.qmp.cmd("send-key", keys=[{"type": "qcode", "data": k}])
            time.sleep(0.15)

    def wait(self, what, test, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            r = test()
            if r:
                return r
            time.sleep(0.5)
        raise Fail("dopo %d s: %s" % (seconds, what))

    def stable(self, what, seconds, keys=(), frames=3):
        """Aspetta una schermata ferma: frames schermate di fila, a 1 s, che
        differiscono per meno dell'1% (l'orologio del setup cambia ogni
        secondo), premendo keys prima di ognuna."""
        seen = []

        def test():
            if keys:
                self.key(*keys)
            time.sleep(1.0)
            seen.append(self.shot())
            del seen[:-frames]
            if len(seen) == frames and all(a.diff(b) < 0.01 for a, b in zip(seen, seen[1:])):
                return seen[-1]
            return None
        return self.wait(what, test, seconds)


# La variabile di prova: un GUID del fornitore tutto suo, mai usato dal
# firmware; due valori di lunghezza diversa, cosi' il secondo e' un record
# nuovo e quello vecchio va cancellato
VAR_NAME = "OuroSmoke"
VAR_GUID = "2c1a8e3f-6d5b-4f1e-9a37-0b6e5f4d2c11"
VAR_DATA = ("4F55524F", "424F4F5432")      # "OURO", "BOOT2"
VAR_VALID = (0x3F, 0x3E)                   # VAR_ADDED, e in cancellazione


def boot_option_number(flash, description):
    """Il numero della voce Boot#### dello store con quella descrizione
    (UCS-2 dopo gli 6 byte di attributi e lunghezza del device path)."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import update                           # noqa: E402 (tools/update.py)
    with open(flash, "rb") as f:
        data = f.read()
    areas = update.fmap_areas(data)
    off, size = areas["SMMSTORE"]
    region = data[off:off + size]
    found, _ = update.var_store(region)
    want = description.encode("utf-16-le") + b"\0\0"
    for v in found:
        if v["state"] == VAR_VALID[0] and len(v["name"]) == 8 and v["name"].startswith("Boot") \
                and v["name"][4:].isalnum() and v["guid"] == GLOBAL_GUID:
            if region[v["data"] + 6:v["data"] + 6 + len(want)] == want:
                return int(v["name"][4:], 16)
    return None


# Lo script che il DHCP di QEMU indica a iPXE (bootfile), e il secondo che
# questo chiama: la richiesta TFTP del secondo nel pcap prova che il primo
# e' stato scaricato ed eseguito
IPXE_SCRIPT = "smoke.ipxe"
IPXE_SCRIPT_2 = "smoke-ok.ipxe"
# Quello che l'MBR di prova scrive sulla seriale quando il BIOS legacy lo avvia
CSM_MARK = b"OUROBIOS-CSM-SMOKE"
GLOBAL_GUID = uuid.UUID("8be4df61-93ca-11d2-aa0d-00e098032b8c").bytes_le


def mbr_image(path):
    """Un disco di 1 MiB con un MBR che scrive CSM_MARK sulla porta seriale
    COM1 (0x3f8, senza inizializzarla: QEMU la accetta cosi') e si ferma."""
    code = bytearray()
    code += b"\xfa"                                  # cli
    code += b"\x31\xc0"                              # xor ax, ax
    code += b"\x8e\xd8"                              # mov ds, ax
    code += b"\xbe" + (0x7c00 + 0x20).to_bytes(2, "little")  # mov si, msg
    code += b"\xba\xf8\x03"                          # mov dx, 0x3f8
    # loop: lodsb; test al, al; jz halt; out dx, al; jmp loop
    code += b"\xac\x84\xc0\x74\x03\xee\xeb\xf8"
    code += b"\xf4\xeb\xfd"                          # halt: hlt; jmp halt
    assert len(code) <= 0x20
    code += b"\0" * (0x20 - len(code))
    code += CSM_MARK + b"\r\n\0"
    sector = bytes(code) + b"\0" * (510 - len(code)) + b"\x55\xaa"
    with open(path, "wb") as f:
        f.write(sector + b"\0" * (1024 * 1024 - 512))


def tftp_requests(pcap):
    """I file chiesti in TFTP (RRQ) nel pcap di filter-dump."""
    with open(pcap, "rb") as f:
        data = f.read()
    names = []
    off = 24                                        # il global header
    while off + 16 <= len(data):
        incl = int.from_bytes(data[off + 8:off + 12], "little")
        pkt = data[off + 16:off + 16 + incl]
        off += 16 + incl
        # Ethernet, IPv4, UDP, poi TFTP: opcode 1 (RRQ), nome, 0, modo
        if len(pkt) > 42 and pkt[12:14] == b"\x08\x00" and pkt[23] == 17:
            ihl = (pkt[14] & 0xf) * 4
            udp = pkt[14 + ihl:]
            if len(udp) > 10 and udp[8:10] == b"\x00\x01":
                names.append(udp[10:udp.index(b"\0", 10)].decode("ascii", "replace"))
    return names


def store_copies(flash):
    """Le copie della variabile di prova nello store di SMMSTORE della ROM:
    [(stato, attributi, dati)]."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import update                           # noqa: E402 (tools/update.py)
    with open(flash, "rb") as f:
        data = f.read()
    areas = update.fmap_areas(data)
    if not areas or "SMMSTORE" not in areas:
        raise Fail("la ROM non ha una regione SMMSTORE")
    off, size = areas["SMMSTORE"]
    region = data[off:off + size]
    found, _ = update.var_store(region)
    guid = uuid.UUID(VAR_GUID).bytes_le
    return [(v["state"], v["attrs"], region[v["data"]:v["data"] + v["size"]].hex().upper())
            for v in found if v["name"] == VAR_NAME and v["guid"] == guid]


def run(rom, out, timeout):
    os.makedirs(out, exist_ok=True)
    sm = Smoke(rom, out, timeout)
    steps = []
    t0 = time.monotonic()
    at = lambda: "%3.0f s" % (time.monotonic() - t0)

    def to_payload():
        sm.wait("coreboot non e' arrivato al payload (\"Jumping to boot code\" nella seriale)",
                lambda: (sm.alive(), "Jumping to boot code" in sm.serial_text())[1], timeout)

    try:
        # 1. il setup: Esc durante l'avvio lo apre (BOOT_MANAGER_ESCAPE); la
        # pagina principale lo ignora, cosi' si puo' premere finche' non c'e'
        sm.boot("setup")
        to_payload()
        steps.append("%s  coreboot -> payload" % at())

        def drawn():
            sm.key("esc")
            s = sm.shot()
            return s if s.colors() >= 2 else None
        first = sm.wait("il payload non ha disegnato niente (schermo di un colore solo)", drawn, timeout)
        sm.shot("payload")
        steps.append("%s  il payload disegna (%d colori)" % (at(), first.colors()))

        home = sm.stable("il setup non si e' fermato su una pagina", timeout, keys=("esc",))
        sm.shot("setup")
        if home.colors() <= 8:
            raise Fail("lo schermo si e' fermato con %d colori: la Shell UEFI o la console di testo, "
                       "non il setup (Esc non l'ha aperto)" % home.colors())
        steps.append("%s  setup, pagina principale (%d colori)" % (at(), home.colors()))

        sm.key("down", "ret")
        page = sm.stable("dopo Giu' e Invio lo schermo non si e' fermato", timeout)
        sm.shot("pagina")
        changed = page.diff(home)
        if changed < 0.03:
            raise Fail("Giu' e Invio non hanno aperto una pagina (%.1f%% dello schermo cambiato)"
                       % (100 * changed))
        steps.append("%s  Giu' + Invio: un'altra pagina (%.0f%% dello schermo)" % (at(), 100 * changed))

        sm.key("esc")
        back = sm.stable("dopo Esc lo schermo non si e' fermato", timeout)
        sm.shot("indietro")
        if back.diff(page) < 0.03:
            raise Fail("Esc non ha lasciato la pagina")
        steps.append("%s  Esc: di nuovo la pagina principale" % at())
        sm.alive()
        boots = sm.serial_text().count("bootblock starting")
        if boots > 1:
            raise Fail("coreboot e' partito %d volte" % boots)
        steps.append("%s  QEMU vivo, un solo avvio" % at())

        # 2. le variabili UEFI in SMMSTORE: senza dischi il payload arriva
        # alla Shell, dove setvar scrive la variabile di prova e reset -s
        # spegne; la si cerca poi nello store del flash. Al secondo avvio la
        # Shell la riscrive: il driver delle variabili deve trovare la copia
        # di prima nel flash e cancellarla, e ne deve restare una sola
        for i, value in enumerate(VAR_DATA):
            sm.boot("shell-%d" % (i + 1))
            to_payload()
            shell = sm.stable("il payload non e' arrivato alla Shell UEFI", timeout)
            if shell.colors() > 8:
                raise Fail("senza dischi il payload non si e' fermato nella Shell (%d colori)"
                           % shell.colors())
            sm.type("setvar %s -guid %s -bs -rt -nv =%s\n" % (VAR_NAME, VAR_GUID, value))
            # setvar torna al prompt quando la variabile e' nel flash; poi si
            # chiude QEMU, che scrive su disco il flash emulato. Niente reset
            # -s: il PM di ICH9 in QEMU spegne con SLP_TYP 0, coreboot da' a
            # S5 il 7 dei PCH Intel
            sm.stable("dopo setvar la Shell non e' tornata al prompt", timeout)
            sm.shot("shell-%d" % (i + 1))
            sm.halt()
            copies = store_copies(sm.flash)
            valid = [c for c in copies if c[0] in VAR_VALID]
            if len(valid) != 1 or valid[0][2] != value:
                raise Fail("nello store %d copie valide di %s (%s), attesa una con %s"
                           % (len(valid), VAR_NAME, ", ".join(c[2] for c in valid) or "nessuna", value))
            if valid[0][1] & 0x7 != 0x7:
                raise Fail("%s con attributi 0x%x, attesi NV+BS+RT (0x7)" % (VAR_NAME, valid[0][1]))
            if i == 0:
                steps.append("%s  Shell: setvar scrive %s in SMMSTORE (NV+BS+RT)" % (at(), VAR_NAME))
            else:
                if len(copies) < 2:
                    raise Fail("la copia di prima di %s non c'e' piu' nello store" % VAR_NAME)
                steps.append("%s  dopo un riavvio: riscritta, la copia di prima cancellata, una sola "
                             "valida" % at())

        # 3. iPXE e CSMWrap: le voci della FV che il boot manager registra.
        # Senza dischi il payload va alla Shell: da li' BootNext sceglie la
        # voce per l'avvio dopo (setvar sul nome, letto dallo store)
        def boot_next(tag, description):
            n = boot_option_number(sm.flash, description)
            if n is None:
                raise Fail("nello store nessuna voce Boot#### \"%s\": il boot manager non l'ha registrata"
                           % description)
            sm.boot(tag)
            to_payload()
            shell = sm.stable("il payload non e' arrivato alla Shell UEFI", timeout)
            if shell.colors() > 8:
                raise Fail("senza dischi il payload non si e' fermato nella Shell (%d colori)" % shell.colors())
            sm.type("setvar BootNext -guid %s -bs -rt -nv =%02X00\n" % (str(uuid.UUID(bytes_le=GLOBAL_GUID)), n))
            sm.stable("dopo setvar la Shell non e' tornata al prompt", timeout)
            sm.halt()
            return n

        n = boot_next("shell-ipxe", "iPXE Network Boot")
        tftp = os.path.join(sm.tmp, "tftp")
        os.makedirs(tftp, exist_ok=True)
        with open(os.path.join(tftp, IPXE_SCRIPT), "w") as f:
            f.write("#!ipxe\necho ouroBIOS smoke: ${net0/ip} from ${next-server}\nchain tftp://${next-server}/%s\n"
                    % IPXE_SCRIPT_2)
        with open(os.path.join(tftp, IPXE_SCRIPT_2), "w") as f:
            f.write("#!ipxe\necho ouroBIOS smoke: second script\nexit\n")
        pcap = os.path.join(out, "ipxe.pcap")
        sm.boot("ipxe", ["-netdev", "user,id=n0,tftp=%s,bootfile=%s" % (tftp, IPXE_SCRIPT),
                         "-device", "e1000,netdev=n0", "-object", "filter-dump,id=d0,netdev=n0,file=" + pcap])
        to_payload()
        got = sm.wait("iPXE non ha chiesto in TFTP %s, poi %s (pcap in %s)" % (IPXE_SCRIPT, IPXE_SCRIPT_2, pcap),
                      lambda: (sm.alive(), IPXE_SCRIPT_2 in tftp_requests(pcap))[1], timeout)
        sm.shot("ipxe")
        sm.halt()
        steps.append("%s  iPXE (Boot%04X): DHCP dalla rete di QEMU, %s scaricato in TFTP ed eseguito, "
                     "che ha chiesto %s" % (at(), n, IPXE_SCRIPT, IPXE_SCRIPT_2))

        n = boot_next("shell-csm", "CSMWrap Legacy Boot")
        disk = os.path.join(sm.tmp, "legacy.img")
        mbr_image(disk)
        # due CPU: CSMWrap ne tiene una, logica, per i suoi servizi BIOS (il "BIOS
        # proxy": senza un AP si ferma, "BIOS proxy initialization failed")
        sm.boot("csmwrap", ["-smp", "2", "-drive", "file=%s,format=raw,if=ide" % disk])
        to_payload()
        sm.wait("CSMWrap non ha avviato l'MBR del disco (niente %s sulla seriale)" % CSM_MARK.decode(),
                lambda: (sm.alive(), CSM_MARK.decode() in sm.serial_text())[1], timeout)
        sm.shot("csmwrap")
        steps.append("%s  CSMWrap (Boot%04X): il BIOS legacy ha avviato l'MBR del disco SATA, che ha scritto "
                     "sulla seriale" % (at(), n))
        return steps
    finally:
        sm.close()


def main():
    ap = argparse.ArgumentParser(description="Prova di fumo del payload EDK2 in QEMU q35")
    ap.add_argument("rom", help="ROM coreboot per QEMU q35 (tools/build.sh smoke)")
    ap.add_argument("--out", default="smoke", help="dove salvare schermate e seriale (default: smoke/)")
    ap.add_argument("--timeout", type=float, default=180, help="secondi per ogni passo (default 180)")
    args = ap.parse_args()
    if not shutil.which("qemu-system-x86_64"):
        sys.exit("serve qemu-system-x86_64 (Debian/Ubuntu: apt install qemu-system-x86)")
    try:
        steps = run(args.rom, args.out, args.timeout)
    except Fail as e:
        print("smoke: FALLITO: %s (schermate e seriale in %s)" % (e, args.out))
        return 1
    for s in steps:
        print("  ok  %s" % s)
    print("smoke: passato (schermate in %s)" % args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
