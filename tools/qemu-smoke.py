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
#   5. QEMU e' ancora vivo: nessun riavvio, nessun triple fault (-no-reboot).
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
    def __init__(self, rom, out, timeout):
        self.out, self.timeout = out, timeout
        self.tmp = tempfile.mkdtemp(prefix="qemu-smoke-")
        self.flash = os.path.join(self.tmp, "flash.rom")
        shutil.copyfile(rom, self.flash)        # la ROM si scrive (SMMSTORE): una copia
        self.serial = os.path.join(out, "serial.log")
        self.sock = os.path.join(self.tmp, "qmp.sock")
        self.n = 0
        cmd = ["qemu-system-x86_64", "-M", "q35,acpi=off", "-cpu", "Haswell-noTSX,stepping=3",
               "-smp", "1", "-m", "2048", "-drive", "if=pflash,format=raw,file=" + self.flash,
               "-serial", "file:" + self.serial, "-qmp", "unix:%s,server=on,wait=off" % self.sock,
               "-display", "none", "-vga", "std", "-no-reboot"]
        self.log = open(os.path.join(out, "qemu.log"), "w")
        self.proc = subprocess.Popen(cmd, stdout=self.log, stderr=subprocess.STDOUT)
        self.qmp = Qmp(self.sock)

    def close(self):
        try:
            self.qmp.cmd("quit")
        except (Fail, OSError):
            pass
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.log.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

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


def run(rom, out, timeout):
    os.makedirs(out, exist_ok=True)
    sm = Smoke(rom, out, timeout)
    steps = []
    t0 = time.monotonic()
    at = lambda: "%3.0f s" % (time.monotonic() - t0)
    try:
        sm.wait("coreboot non e' arrivato al payload (\"Jumping to boot code\" nella seriale)",
                lambda: (sm.alive(), "Jumping to boot code" in sm.serial_text())[1], timeout)
        steps.append("%s  coreboot -> payload" % at())

        # Esc durante l'avvio apre il setup (BOOT_MANAGER_ESCAPE); la pagina
        # principale lo ignora, cosi' si puo' premere finche' non c'e'
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
        steps.append("%s  Esc: di nuovo la pagina principale (%.0f%% diversa da quella di prima)"
                     % (at(), 100 * back.diff(home)))
        sm.alive()
        boots = sm.serial_text().count("bootblock starting")
        if boots > 1:
            raise Fail("coreboot e' partito %d volte" % boots)
        steps.append("%s  QEMU vivo, un solo avvio" % at())
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
