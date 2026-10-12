#!/usr/bin/env python3
# pm-regs.py - i registri di risparmio energetico di processore, System Agent
# e PCH di un Haswell con Lynx Point, cosi' come li ha lasciati il firmware:
# per confrontare due firmware, o il firmware con quello che dovrebbe aver
# scritto, quando il package non scende sotto PC2 (tools/idle-report.py).
#
#   sudo python3 tools/pm-regs.py > pm-regs.txt
#
# Prima un riassunto dei registri che contano per gli stati C del package,
# poi le zone intere (solo le dword diverse da zero): MCHBAR 0x4000-0x7fff,
# DMIBAR, EPBAR, RCBA, e lo spazio di configurazione dei dispositivi PCI del
# PCH. Solo lettura: MMIO da /dev/mem (serve iomem=relaxed, come per
# flashrom), configurazione PCI da sysfs, MSR da /dev/cpu/*/msr. Non legge
# i dispositivi in D3cold, per non risvegliarli. Niente numeri di serie o MAC.
#
# Solo la libreria standard di Python.
import glob
import mmap
import os
import struct
import subprocess
import sys

PCI = "/sys/bus/pci/devices/0000:"


def rd(path, default="?"):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def cfg(bdf, size=256):
    path = PCI + bdf
    if rd(path + "/power_state") == "D3cold":
        return None
    try:
        with open(path + "/config", "rb") as f:
            return f.read(size)
    except OSError:
        return None


def u(data, off, size=4):
    return int.from_bytes(data[off:off + size], "little") if data and off + size <= len(data) else None


class Mmio:
    def __init__(self, base, size):
        fd = os.open("/dev/mem", os.O_RDONLY | os.O_SYNC)
        try:
            self.m = mmap.mmap(fd, size, mmap.MAP_SHARED, mmap.PROT_READ, offset=base)
        finally:
            os.close(fd)

    def rd(self, off, size=4):
        # "@": il formato nativo legge con memcpy, cioe' un accesso intero alla
        # dword; "<" e "=" la ricompongono byte per byte
        fmt = {1: "@B", 2: "@H", 4: "@I"}[size]
        return struct.unpack_from(fmt, self.m, off)[0]


def msr(cpu, reg):
    try:
        fd = os.open("/dev/cpu/%d/msr" % cpu, os.O_RDONLY)
        try:
            return struct.unpack("<Q", os.pread(fd, 8, reg))[0]
        finally:
            os.close(fd)
    except OSError:
        return None


def h(v, width=8):
    return "-" if v is None else "0x%0*x" % (width, v)


def dump(title, reader, start, end, out):
    out.append("== %s (dword diverse da zero)" % title)
    line = []
    for off in range(start, end, 4):
        try:
            v = reader(off)
        except (OSError, ValueError, struct.error):
            v = None
        if v:
            line.append("%04x:%08x" % (off, v))
        if len(line) == 6:
            out.append("  " + " ".join(line))
            line = []
    if line:
        out.append("  " + " ".join(line))


def main():
    if os.geteuid() != 0:
        sys.exit("serve root: sudo python3 %s > pm-regs.txt" % sys.argv[0])
    if not os.path.exists("/dev/cpu/0/msr"):
        try:
            subprocess.run(["modprobe", "msr"], stderr=subprocess.DEVNULL)
        except OSError:
            pass
    out = []
    out.append("pm-regs: firmware %s, kernel %s" % (rd("/sys/class/dmi/id/bios_version"), os.uname().release))

    host = cfg("00:00.0")
    lpc = cfg("00:1f.0")
    # Solo Lynx Point o Wildcat Point (8 e 9 series): altrove gli indirizzi
    # letti qui non sono quei registri
    if u(host, 0, 2) != 0x8086 or u(lpc, 0, 2) != 0x8086 or (u(lpc, 2, 2) & 0xff00) not in (0x8c00, 0x9c00):
        sys.exit("non e' un PCH Lynx Point o Wildcat Point: niente da leggere")
    mchbar = (u(host, 0x48, 8) or 0) & ~0x7fff
    dmibar = (u(host, 0x68, 8) or 0) & ~0xfff
    epbar = (u(host, 0x40, 8) or 0) & ~0xfff
    rcba = (u(lpc, 0xf0) or 0) & ~0x3fff
    out.append("MCHBAR %s DMIBAR %s EPBAR %s RCBA %s" % (h(mchbar), h(dmibar), h(epbar), h(rcba)))

    regions = {}
    for name, base, size in (("MCHBAR", mchbar, 0x8000), ("DMIBAR", dmibar, 0x1000),
                             ("EPBAR", epbar, 0x1000), ("RCBA", rcba, 0x4000)):
        try:
            regions[name] = Mmio(base, size) if base else None
        except OSError as e:
            out.append("%s non leggibile da /dev/mem: %s (iomem=relaxed?)" % (name, e))
            regions[name] = None
    mch, dmi, ep, rc = (regions[n] for n in ("MCHBAR", "DMIBAR", "EPBAR", "RCBA"))

    out.append("== Riassunto")
    out.append("MSR: PKG_CST_CONFIG_CONTROL %s POWER_CTL %s MISC_PWR_MGMT %s MISC_ENABLE %s" % (
        h(msr(0, 0xE2)), h(msr(0, 0x1FC)), h(msr(0, 0x1AA)), h(msr(0, 0x1A0), 16)))
    out.append("MSR: PKGC3_IRTL %s PKGC6_IRTL %s PKGC7_IRTL %s PKG_POWER_LIMIT %s PLATFORM_INFO %s" % (
        h(msr(0, 0x60A)), h(msr(0, 0x60B)), h(msr(0, 0x60C)), h(msr(0, 0x610), 16), h(msr(0, 0xCE), 16)))
    if mch:
        out.append("MCHBAR: PM_SREF_CONFIG(5060) %s PM_PDWN_CONFIG(4cb0) %s MC_INIT_STATE_G(5030) %s" % (
            h(mch.rd(0x5060)), h(mch.rd(0x4cb0)), h(mch.rd(0x5030))))
        out.append("MCHBAR: BIOS_RESET_CPL(5da8) %s SAPMCTL(5f00) %s INTRDIRCTL(5418) %s PCU_DDR_PTM_CTL(5880) %s" % (
            h(mch.rd(0x5da8, 1), 2), h(mch.rd(0x5f00)), h(mch.rd(0x5418)), h(mch.rd(0x5880))))
        out.append("MCHBAR: SSKPD(5d10) %s%s TC_SRFTP ch0/ch1 %s %s" % (
            h(mch.rd(0x5d14)), h(mch.rd(0x5d10))[2:], h(mch.rd(0x42a4)), h(mch.rd(0x46a4))))
    if dmi:
        out.append("DMIBAR (CPU): LCAP %s LCTL %s LSTS %s LCTL2 %s L0SLAT %s LLTC %s AFE_PM_TMR %s" % (
            h(dmi.rd(0x84)), h(dmi.rd(0x88, 2), 4), h(dmi.rd(0x8a, 2), 4), h(dmi.rd(0x98, 2), 4),
            h(dmi.rd(0x22c)), h(dmi.rd(0x238)), h(dmi.rd(0xc28))))
    if rc:
        out.append("RCBA (PCH DMI): LCAP(21a4) %s LCTL(21a8) %s LSTS(21aa) %s 2304 %s 2314 %s 2320 %s 232c %s 2348 %s" % (
            h(rc.rd(0x21a4)), h(rc.rd(0x21a8, 2), 4), h(rc.rd(0x21aa, 2), 4), h(rc.rd(0x2304)),
            h(rc.rd(0x2314)), h(rc.rd(0x2320)), h(rc.rd(0x232c)), h(rc.rd(0x2348))))
        out.append("RCBA: GCS %s BUC %s FD %s CG %s FD2 %s 1100 %s 2238 %s" % (
            h(rc.rd(0x3410)), h(rc.rd(0x3414)), h(rc.rd(0x3418)), h(rc.rd(0x341c)), h(rc.rd(0x3428)),
            h(rc.rd(0x1100)), h(rc.rd(0x2238))))
        out.append("RCBA PM: " + " ".join("%04x:%s" % (o, h(rc.rd(o))) for o in (
            0x3310, 0x3314, 0x3318, 0x3324, 0x3340, 0x3344, 0x3368, 0x3378, 0x337c, 0x3388, 0x33a0,
            0x33a4, 0x33ac, 0x33b0, 0x33c0, 0x33c4, 0x33c8, 0x33cc, 0x33d0, 0x33d4, 0x3a28, 0x3a2c,
            0x3a80, 0x3a84, 0x3a88, 0x2b14, 0x2b24, 0x2b28, 0x2b2c)))
    if lpc:
        out.append("LPC: GEN_PMCON_1 %s GEN_PMCON_2 %s GEN_PMCON_3 %s PMIR %s a9 %s" % (
            h(u(lpc, 0xa0, 2), 4), h(u(lpc, 0xa2, 1), 2), h(u(lpc, 0xa4, 2), 4), h(u(lpc, 0xac)),
            h(u(lpc, 0xa9, 1), 2)))

    for name, region, end in (("MCHBAR", mch, 0x8000), ("DMIBAR", dmi, 0x1000),
                              ("EPBAR", ep, 0x1000), ("RCBA", rc, 0x4000)):
        if region:
            dump(name, region.rd, 0x4000 if name == "MCHBAR" else 0, end, out)

    for dev in sorted(glob.glob(PCI + "00:*")):
        bdf = dev.split("0000:")[-1]
        cls = rd(dev + "/class", "0x000000")
        size = 4096 if cls.startswith("0x0604") else 256
        data = cfg(bdf, size)
        if data is None:
            out.append("== PCI %s: in D3cold, non letto" % bdf)
            continue
        dump("PCI %s %s" % (bdf, cls[2:6]), lambda off, d=data: u(d, off), 0, len(data), out)

    print("\n".join(out))


if __name__ == "__main__":
    main()
