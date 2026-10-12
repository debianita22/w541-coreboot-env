#!/usr/bin/env python3
# idle-report.py - cosa tiene sveglio il portatile a riposo. Misura per N
# secondi gli stati C dei core e del package e il consumo (RAPL e batteria),
# poi riporta lo stato di risparmio energetico dei dispositivi che li
# condizionano: ASPM, LTR e runtime PM sul PCIe, LPM del SATA, autosuspend
# dell'USB, RC6, FBC e PSR della grafica Intel, interrupt al secondo.
#
#   sudo python3 tools/idle-report.py                # 30 s
#   sudo python3 tools/idle-report.py --seconds 60
#
# Va lanciato con il portatile a riposo (schermo acceso, niente in
# esecuzione): i numeri valgono per quell'intervallo. Solo lettura, tranne
# il modulo msr che carica se manca (come turbostat). Non stampa numeri di
# serie, MAC o UUID: il report si puo' allegare a una segnalazione.
#
# Solo la libreria standard di Python.
import argparse
import glob
import os
import platform
import re
import struct
import subprocess
import sys
import time

# MSR (Intel SDM vol. 4); i contatori di residenza contano al ritmo del TSC
MSR_TSC = 0x10
MSR_MPERF = 0xE7
MSR_APERF = 0xE8
MSR_PKG_CST_CONFIG_CONTROL = 0xE2
MSR_POWER_CTL = 0x1FC
MSR_PKGC_IRTL = (("PC3", 0x60A), ("PC6", 0x60B), ("PC7", 0x60C))
CORE_CSTATES = (("C3", 0x3FC), ("C6", 0x3FD), ("C7", 0x3FE))
PKG_CSTATES = (("PC2", 0x60D), ("PC3", 0x3F8), ("PC6", 0x3F9), ("PC7", 0x3FA),
               ("PC8", 0x630), ("PC9", 0x631), ("PC10", 0x632))
MSR_RAPL_POWER_UNIT = 0x606
RAPL = (("package", 0x611), ("core", 0x639), ("grafica", 0x641), ("RAM", 0x619))

# Limite del package in MSR_PKG_CST_CONFIG_CONTROL[3:0], come lo decodifica
# turbostat per Haswell e Broadwell (hsw_pkg_cstate_limits)
HSW_MODELS = (0x3C, 0x45, 0x46, 0x3D, 0x47)
HSW_PKG_LIMITS = ("PC0", "PC2", "PC3", "PC6", "PC7", "PC7s", "PC8", "PC9", "nessuno")
# Haswell e Broadwell non ULT: niente PC8-PC10
NO_PC8_MODELS = (0x3C, 0x46, 0x47)

IRTL_UNIT_NS = (1, 32, 1024, 32768, 1048576, 33554432)

PCIE_TYPES = {0: "ep", 1: "ep", 4: "rp", 5: "up", 6: "dp", 9: "rciep", 10: "rcec"}
ASPM_BITS = {0: "-", 1: "L0s", 2: "L1", 3: "L0sL1"}
LINK_SPEED = {1: "2.5GT/s", 2: "5GT/s", 3: "8GT/s", 4: "16GT/s", 5: "32GT/s"}

# Parametri del kernel che non dicono nulla sul consumo e identificano la macchina
CMDLINE_SKIP = re.compile(r"^(BOOT_IMAGE|root|rootflags|rootfstype|resume|resume_offset|"
                          r"cryptdevice|rd\.luks\.\w+|initrd|ro|rw|quiet|splash|loglevel)(=|$)")


def rd(path, default=None):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def cpu_list():
    out = []
    for part in (rd("/sys/devices/system/cpu/online") or "0").split(","):
        a, _, b = part.partition("-")
        out.extend(range(int(a), int(b or a) + 1))
    return out


class Msr:
    def __init__(self):
        self.fds = {}
        if not os.path.exists("/dev/cpu/0/msr"):
            try:
                subprocess.run(["modprobe", "msr"], stderr=subprocess.DEVNULL)
            except OSError:
                pass

    def read(self, cpu, reg):
        try:
            fd = self.fds.get(cpu)
            if fd is None:
                fd = self.fds[cpu] = os.open("/dev/cpu/%d/msr" % cpu, os.O_RDONLY)
            return struct.unpack("<Q", os.pread(fd, 8, reg))[0]
        except OSError:
            return None


def cpu_model():
    fam = model = None
    for line in open("/proc/cpuinfo"):
        if line.startswith("cpu family"):
            fam = int(line.split(":")[1])
        elif line.startswith("model\t") or line.startswith("model :"):
            model = int(line.split(":")[1])
        if fam is not None and model is not None:
            break
    return fam, model


def topology(cpus):
    cores, pkgs = {}, {}
    for c in cpus:
        base = "/sys/devices/system/cpu/cpu%d/topology/" % c
        key = (rd(base + "physical_package_id", "0"), rd(base + "core_id", str(c)))
        cores.setdefault(key, []).append(c)
        pkgs.setdefault(key[0], c)
    return cores, pkgs


def irq_counts():
    counts = {}
    lines = open("/proc/interrupts").read().splitlines()
    ncpu = len(lines[0].split())
    for line in lines[1:]:
        name, _, rest = line.partition(":")
        fields = rest.split()
        nums = []
        for f in fields[:ncpu]:
            if not f.isdigit():
                break
            nums.append(int(f))
        label = " ".join(fields[len(nums):])
        name = name.strip()
        if name.isdigit():
            # chip, hwirq, nomi dei dispositivi: tengo i nomi
            label = " ".join(label.split()[2:]) or label
        counts[name] = (sum(nums), label)
    return counts


def battery_power():
    """W tolti dalla batteria (somma), None se nessuna batteria scarica"""
    total, any_discharging = 0.0, False
    for b in glob.glob("/sys/class/power_supply/BAT*"):
        if rd(b + "/present", "1") != "1" or rd(b + "/status") != "Discharging":
            continue
        p = rd(b + "/power_now")
        if p is not None:
            w = int(p) / 1e6
        else:
            i, v = rd(b + "/current_now"), rd(b + "/voltage_now")
            if i is None or v is None:
                continue
            w = int(i) * int(v) / 1e12
        total += abs(w)
        any_discharging = True
    return total if any_discharging else None


def cpuidle_snapshot(cpus):
    snap = {}
    for c in cpus:
        for st in glob.glob("/sys/devices/system/cpu/cpu%d/cpuidle/state*" % c):
            name = rd(st + "/name", "?")
            t, u = int(rd(st + "/time", "0")), int(rd(st + "/usage", "0"))
            a = snap.setdefault(name, [0, 0, set(), int(rd(st + "/latency", "0"))])
            a[0] += t
            a[1] += u
            if rd(st + "/disable", "0") != "0":
                a[2].add(c)
    return snap


def rc6_files():
    out = []
    for card in glob.glob("/sys/class/drm/card[0-9]"):
        drv = os.path.basename(os.path.realpath(card + "/device/driver"))
        f = card + "/power/rc6_residency_ms"
        if drv == "i915" and os.path.exists(f):
            out.append(f)
    return out


def sample(msr, cpus, seconds):
    cores, pkgs = topology(cpus)
    pkg_cpu = sorted(pkgs.values())[0]
    unit = msr.read(pkg_cpu, MSR_RAPL_POWER_UNIT)
    esu = 1.0 / (1 << ((unit >> 8) & 0x1f)) if unit is not None else None

    def snap():
        s = {"t": time.monotonic()}
        for c in cpus:
            for r in (MSR_TSC, MSR_MPERF, MSR_APERF) + tuple(x[1] for x in CORE_CSTATES):
                s[(c, r)] = msr.read(c, r)
        for _, r in PKG_CSTATES:
            s[("pkg", r)] = msr.read(pkg_cpu, r)
        for _, r in RAPL:
            s[("rapl", r)] = msr.read(pkg_cpu, r)
        s["rc6"] = [int(rd(f, "0")) for f in rc6_files()]
        return s

    idle0, irq0 = cpuidle_snapshot(cpus), irq_counts()
    a = snap()
    bat, end = [], a["t"] + seconds
    while time.monotonic() < end:
        time.sleep(min(5.0, max(0.0, end - time.monotonic())))
        p = battery_power()
        if p is not None:
            bat.append(p)
    b = snap()
    idle1, irq1 = cpuidle_snapshot(cpus), irq_counts()

    def d(key, wrap=64):
        x, y = a.get(key), b.get(key)
        if x is None or y is None:
            return None
        return (y - x) % (1 << wrap)

    res = {"seconds": b["t"] - a["t"], "cores": [], "pkg": {}, "rapl": {}, "bat": bat}
    tsc = d((pkg_cpu, MSR_TSC))
    for key in sorted(cores):
        threads = cores[key]
        t = d((threads[0], MSR_TSC)) or tsc
        busy = [d((c, MSR_MPERF)) for c in threads]
        busy = [x / t * 100 for x in busy if x is not None] if t else []
        freq = []
        for c in threads:
            m, ap, tt = d((c, MSR_MPERF)), d((c, MSR_APERF)), d((c, MSR_TSC))
            if m and ap and tt:
                freq.append(ap / m * tt / res["seconds"] / 1e6)
        row = {"core": key[1], "busy": max(busy) if busy else None,
               "mhz": sum(freq) / len(freq) if freq else None}
        for name, r in CORE_CSTATES:
            v = d((threads[0], r))
            row[name] = v / t * 100 if (v is not None and t) else None
        res["cores"].append(row)
    fam, model = cpu_model()
    for name, r in PKG_CSTATES:
        if name in ("PC8", "PC9", "PC10") and fam == 6 and model in NO_PC8_MODELS:
            continue
        v = d(("pkg", r))
        if v is not None and tsc:
            res["pkg"][name] = v / tsc * 100
    if esu:
        for name, r in RAPL:
            v = d(("rapl", r), 32)
            if v is not None:
                res["rapl"][name] = v * esu / res["seconds"]
    if a["rc6"] and len(a["rc6"]) == len(b["rc6"]):
        res["rc6"] = (b["rc6"][0] - a["rc6"][0]) / (res["seconds"] * 1000) * 100

    ncpu = len(cpus)
    res["idle"] = []
    for name, (t1, u1, dis, lat) in idle1.items():
        t0, u0 = idle0.get(name, (0, 0))[:2]
        share = (t1 - t0) / 1e6 / res["seconds"] / ncpu * 100
        res["idle"].append((lat, name, share, (u1 - u0) / res["seconds"], len(dis)))
    res["idle"].sort()
    rates = []
    for k, (n1, label) in irq1.items():
        n0 = irq0.get(k, (0, ""))[0]
        rate = (n1 - n0) / res["seconds"]
        if rate >= 1:
            rates.append((rate, k, label))
    res["irq"] = sorted(rates, reverse=True)[:10]
    return res


def pct(v):
    return "  -  " if v is None else "%5.1f" % v


def print_cstates(msr, cpus, res, fam, model):
    print("== Stati C e consumo in %.0f s" % res["seconds"])
    pkg = "  ".join("%s %s%%" % (k, pct(v).strip()) for k, v in res["pkg"].items())
    print("package: " + (pkg or "contatori non leggibili"))
    cfg = msr.read(cpus[0], MSR_PKG_CST_CONFIG_CONTROL)
    if cfg is not None:
        lim = cfg & 0xF
        name = HSW_PKG_LIMITS[lim] if (fam == 6 and model in HSW_MODELS and lim < len(HSW_PKG_LIMITS)) else "%#x" % lim
        print("limite del package: %s (MSR 0xe2 = %#010x%s)" % (name, cfg & 0xFFFFFFFF,
                                                             ", bloccato" if cfg & (1 << 15) else ""))
    irtl = []
    for name, r in MSR_PKGC_IRTL:
        v = msr.read(cpus[0], r)
        if v is not None:
            unit = (v >> 10) & 7
            ns = (v & 0x3FF) * IRTL_UNIT_NS[unit] if unit < len(IRTL_UNIT_NS) else None
            irtl.append("%s %s%s" % (name, "%d us" % (ns // 1000) if ns is not None else "?",
                                     "" if v & (1 << 15) else " (non valido)"))
    pc = msr.read(cpus[0], MSR_POWER_CTL)
    if irtl or pc is not None:
        print("latenze di risveglio: %s; C1E %s" % (", ".join(irtl) or "-",
                                                  "-" if pc is None else ("acceso" if pc & 2 else "spento")))
    if res["rapl"]:
        print("potenza (RAPL): " + ", ".join("%s %.2f W" % (k, v) for k, v in res["rapl"].items()))
    if res["bat"]:
        print("batteria: %.1f W in media (%d letture)" % (sum(res["bat"]) / len(res["bat"]), len(res["bat"])))
    print("core   occupato    MHz     C3     C6     C7   (C1 e' il resto)")
    for r in res["cores"]:
        print("%4s     %s   %5s  %s  %s  %s" % (r["core"], pct(r["busy"]),
              "-" if r["mhz"] is None else "%4.0f" % r["mhz"], pct(r["C3"]), pct(r["C6"]), pct(r["C7"])))
    if "rc6" in res:
        print("grafica Intel: RC6 %.1f%%" % res["rc6"])
    drv = rd("/sys/devices/system/cpu/cpuidle/current_driver", "?")
    gov = rd("/sys/devices/system/cpu/cpuidle/current_governor", rd("/sys/devices/system/cpu/cpuidle/current_governor_ro", "?"))
    print("cpuidle (%s, %s): %s" % (drv, gov, "  ".join(
        "%s %.1f%% %.0f/s%s" % (n, s, u, " DISATTIVATO su %d CPU" % dis if dis else "")
        for _, n, s, u, dis in res["idle"] if n != "POLL" or s >= 0.1)))
    if res["irq"]:
        print("interrupt/s: " + ", ".join("%s %.0f" % ((lab or k)[:28], r) for r, k, lab in res["irq"]))


def pcie_info(cfg):
    if len(cfg) < 0x40 or not (struct.unpack_from("<H", cfg, 6)[0] & 0x10):
        return None
    ptr, seen = cfg[0x34] & 0xFC, 0
    while ptr and ptr + 0x2C <= len(cfg) and seen < 48:
        cap_id, nxt = cfg[ptr], cfg[ptr + 1] & 0xFC
        if cap_id == 0x10:
            flags = struct.unpack_from("<H", cfg, ptr + 2)[0]
            devcap2 = struct.unpack_from("<I", cfg, ptr + 0x24)[0] if ptr + 0x28 <= len(cfg) else 0
            devctl2 = struct.unpack_from("<H", cfg, ptr + 0x28)[0] if ptr + 0x2A <= len(cfg) else 0
            lnkcap = struct.unpack_from("<I", cfg, ptr + 0x0C)[0]
            lnkctl = struct.unpack_from("<H", cfg, ptr + 0x10)[0]
            lnksta = struct.unpack_from("<H", cfg, ptr + 0x12)[0]
            return {"type": (flags >> 4) & 0xF, "lnkcap": lnkcap, "lnkctl": lnkctl,
                    "lnksta": lnksta, "ltr_cap": bool(devcap2 & (1 << 11)),
                    "ltr_en": bool(devctl2 & (1 << 10))}
        ptr, seen = nxt, seen + 1
    return None


def print_pci():
    pol = rd("/sys/module/pcie_aspm/parameters/policy", "?")
    print("== PCI (criterio ASPM del kernel: %s)" % pol)
    print("BDF      classe  id          driver          stato   runtime     ctl   ASPM sup/att  ClkPM  LTR   link")
    for dev in sorted(glob.glob("/sys/bus/pci/devices/*")):
        bdf = os.path.basename(dev)[5:]
        cls = (rd(dev + "/class", "0x000000")[2:6])
        ids = "%s:%s" % (rd(dev + "/vendor", "0x????")[2:], rd(dev + "/device", "0x????")[2:])
        drv = os.path.basename(os.path.realpath(dev + "/driver")) if os.path.exists(dev + "/driver") else "-"
        dstate = rd(dev + "/power_state", "?")
        rt = rd(dev + "/power/runtime_status", "?")
        ctl = rd(dev + "/power/control", "?")
        try:
            cfg = open(dev + "/config", "rb").read(256)
        except OSError:
            cfg = b""
        p = pcie_info(cfg)
        link = ""
        if p and p["type"] not in (9, 10):
            cap = (p["lnkcap"] >> 10) & 3
            clk = "%s/%s" % ("+" if p["lnkcap"] & (1 << 18) else "-", "+" if p["lnkctl"] & (1 << 8) else "-")
            ltr = "%s/%s" % ("+" if p["ltr_cap"] else "-", "+" if p["ltr_en"] else "-")
            width = (p["lnksta"] >> 4) & 0x3F
            speed = LINK_SPEED.get(p["lnksta"] & 0xF, "?") if width else "giu'"
            link = "%-6s/%-6s %-6s %-5s %s%s" % (ASPM_BITS[cap], ASPM_BITS[p["lnkctl"] & 3], clk, ltr,
                                                 speed, " x%d" % width if width else "")
        elif p:
            ltr = "%s/%s" % ("+" if p["ltr_cap"] else "-", "+" if p["ltr_en"] else "-")
            link = "%-13s %-6s %-5s" % ("integrato", "", ltr)
        print("%-8s %-6s  %-10s  %-14s  %-6s  %-10s  %-4s  %s" % (bdf, cls, ids, drv[:14], dstate, rt, ctl, link))


def print_storage_usb_audio():
    print("== SATA (link power management)")
    for host in sorted(glob.glob("/sys/class/scsi_host/host*"), key=lambda h: int(h.rsplit("host", 1)[1])):
        pol = rd(host + "/link_power_management_policy")
        models = [rd(m) for m in glob.glob(host + "/device/target*/*/model")]
        if pol is not None and models:
            print("%-6s %-24s %s" % (os.path.basename(host), pol, ", ".join(m for m in models if m)))
    print("== USB (autosuspend)")
    for dev in sorted(glob.glob("/sys/bus/usb/devices/*")):
        name = os.path.basename(dev)
        if ":" in name or name.startswith("usb") or not os.path.exists(dev + "/idVendor"):
            continue
        print("%-8s %s:%s  %-32s %-5s %s" % (name, rd(dev + "/idVendor"), rd(dev + "/idProduct"),
                                            (rd(dev + "/product") or "?")[:32],
                                            rd(dev + "/power/control", "?"), rd(dev + "/power/runtime_status", "?")))
    ps = rd("/sys/module/snd_hda_intel/parameters/power_save")
    if ps is not None:
        print("== Audio HDA: power_save %s s, controller %s" % (
            ps, rd("/sys/module/snd_hda_intel/parameters/power_save_controller", "?")))
    for f, title in (("i915_fbc_status", "FBC"), ("i915_edp_psr_status", "PSR")):
        for path in glob.glob("/sys/kernel/debug/dri/*/" + f)[:1]:
            lines = [l.strip() for l in (rd(path, "") or "").splitlines() if l.strip()][:3]
            print("== Grafica Intel %s: %s" % (title, " | ".join(lines) or "?"))


def print_header():
    print("ouroBIOS idle-report: firmware %s, kernel %s" % (
        rd("/sys/class/dmi/id/bios_version", "?"), platform.release()))
    params = [t for t in (rd("/proc/cmdline", "") or "").split() if not CMDLINE_SKIP.match(t) and "UUID" not in t.upper()]
    print("parametri del kernel: %s" % (" ".join(params) or "-"))
    ac = [rd(p + "/online") for p in glob.glob("/sys/class/power_supply/A*") if rd(p + "/type") == "Mains"]
    bats = []
    for b in sorted(glob.glob("/sys/class/power_supply/BAT*")):
        if rd(b + "/present", "1") != "1":
            bats.append("%s assente" % os.path.basename(b))
        else:
            bats.append("%s %s %s%%" % (os.path.basename(b), rd(b + "/status", "?"), rd(b + "/capacity", "?")))
    source = "alimentatore" if "1" in ac else ("batteria" if ac else "?")
    print("alimentazione: %s; %s" % (source, ", ".join(bats) or "nessuna batteria"))


def main():
    ap = argparse.ArgumentParser(description="Stati C, consumo e risparmio energetico dei dispositivi a riposo")
    ap.add_argument("--seconds", type=float, default=30, help="durata della misura (default 30)")
    args = ap.parse_args()
    if os.geteuid() != 0:
        sys.exit("serve root: sudo python3 %s" % sys.argv[0])
    cpus = cpu_list()
    fam, model = cpu_model()
    print_header()
    msr = Msr()
    if msr.read(cpus[0], MSR_TSC) is None:
        print("== Stati C: MSR non leggibili (modulo msr?)")
    else:
        print("misura per %.0f s, non toccare il portatile..." % args.seconds, file=sys.stderr, flush=True)
        print_cstates(msr, cpus, sample(msr, cpus, args.seconds), fam, model)
    print_pci()
    print_storage_usb_audio()


if __name__ == "__main__":
    main()
