#!/usr/bin/env python3
# idle-report.py - cosa tiene sveglio il portatile a riposo. Misura per N
# secondi gli stati C dei core e del package, il consumo (RAPL e batteria) e
# il traffico verso la RAM, poi riporta lo stato di risparmio energetico dei
# dispositivi che li condizionano: ASPM, LTR e runtime PM sul PCIe, LPM del
# SATA, autosuspend dell'USB, RC6, FBC e PSR della grafica Intel, interrupt
# al secondo.
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
import mmap
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

# Contatori liberi del controller di memoria in MCHBAR, a 32 bit, in passi
# di 64 byte: quelli che legge il driver uncore_imc di Linux, che pero' non
# riconosce l'host bridge degli Haswell mobili (8086:0c04). Letture e
# scritture in RAM, e le richieste divise per chi le fa: i core, la grafica
# (GT) e l'I/O. Sul W541 l'I/O comprende lo schermo, che a ogni quadro legge
# l'immagine dalla RAM, oltre al DMA dei dispositivi dietro il PCH: a schermo
# acceso ~1 GB/s, a schermo spento 5 MB/s.
IMC_COUNTERS = (("letture", 0x5050), ("scritture", 0x5054),
                ("core", 0x5044), ("grafica", 0x5040), ("I/O", 0x5048))
# Sandy Bridge, Ivy Bridge, Haswell e Broadwell client: stessi indirizzi
IMC_MODELS = (0x2A, 0x3A, 0x3C, 0x45, 0x46, 0x3D, 0x47)

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


class Imc:
    """contatori di traffico del controller di memoria, da /dev/mem"""

    def __init__(self):
        self.m, self.error = None, None
        try:
            with open("/sys/bus/pci/devices/0000:00:00.0/config", "rb") as f:
                cfg = f.read(0x50)
            if len(cfg) < 0x50 or struct.unpack_from("<H", cfg, 0)[0] != 0x8086:
                self.error = "host bridge non Intel"
                return
            bar = struct.unpack_from("<Q", cfg, 0x48)[0]
            if not bar & 1:
                self.error = "MCHBAR non abilitato"
                return
            fd = os.open("/dev/mem", os.O_RDONLY | os.O_SYNC)
            try:
                self.m = mmap.mmap(fd, 0x1000, mmap.MAP_SHARED, mmap.PROT_READ,
                                   offset=(bar & ~0x7FFF) + 0x5000)
            finally:
                os.close(fd)
        except (OSError, ValueError, OverflowError) as e:
            self.m, self.error = None, "%s (serve iomem=relaxed?)" % e

    def read(self):
        if self.m is None:
            return None
        # "@": una lettura intera della dword (memcpy), non byte per byte
        return {name: struct.unpack_from("@I", self.m, off - 0x5000)[0] for name, off in IMC_COUNTERS}


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


def proc_times():
    """tick di CPU (utente + sistema) per nome di processo"""
    out, me = {}, os.getpid()
    for stat in glob.glob("/proc/[0-9]*/stat"):
        try:
            data = open(stat).read()
        except OSError:
            continue
        pid = int(stat.split("/")[2])
        if pid == me:
            continue
        comm = data[data.find("(") + 1:data.rfind(")")]
        fields = data[data.rfind(")") + 2:].split()
        name = comm.split("/")[0] if comm.startswith("kworker") else comm
        try:
            out[(pid, name)] = int(fields[11]) + int(fields[12])
        except (IndexError, ValueError):
            pass
    return out


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


def sample(msr, cpus, seconds, imc=None):
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

    idle0, irq0, procs0 = cpuidle_snapshot(cpus), irq_counts(), proc_times()
    a = snap()
    # i contatori della memoria girano a 32 bit: letti ogni 5 s al massimo,
    # non fanno il giro (256 GiB) tra una lettura e l'altra
    mem_last = imc.read() if imc else None
    mem = dict.fromkeys(mem_last, 0) if mem_last else None
    bat, end = [], a["t"] + seconds
    while time.monotonic() < end:
        time.sleep(min(5.0, max(0.0, end - time.monotonic())))
        p = battery_power()
        if p is not None:
            bat.append(p)
        if mem is not None:
            now = imc.read()
            for k in mem:
                mem[k] += (now[k] - mem_last[k]) % (1 << 32)
            mem_last = now
    b = snap()
    idle1, irq1, procs1 = cpuidle_snapshot(cpus), irq_counts(), proc_times()

    def d(key, wrap=64):
        x, y = a.get(key), b.get(key)
        if x is None or y is None:
            return None
        return (y - x) % (1 << wrap)

    res = {"seconds": b["t"] - a["t"], "cores": [], "pkg": {}, "rapl": {}, "bat": bat}
    if mem is not None:
        res["mem"] = {k: v * 64 / res["seconds"] / 1e6 for k, v in mem.items()}
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
    hz = os.sysconf("SC_CLK_TCK")
    busy = {}
    for key, t1 in procs1.items():
        # un processo nato durante la misura vi ha speso tutto il suo tempo
        dt = t1 - procs0.get(key, 0)
        if dt > 0:
            busy[key[1]] = busy.get(key[1], 0) + dt
    res["procs"] = sorted(((t / hz / res["seconds"] * 100, n) for n, t in busy.items() if t > 0), reverse=True)[:8]
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
        # intel_idle la spegne dove C1E e' uno stato a se' (Haswell compreso)
        print("latenze di risveglio: %s; promozione C1E %s" % (", ".join(irtl) or "-",
                                                             "-" if pc is None else ("accesa" if pc & 2 else "spenta")))
    if res["rapl"]:
        print("potenza (RAPL): " + ", ".join("%s %.2f W" % (k, v) for k, v in res["rapl"].items()))
    if res["bat"]:
        print("batteria: %.1f W in media (%d letture)" % (sum(res["bat"]) / len(res["bat"]), len(res["bat"])))
    if "mem" in res:
        m = res["mem"]
        # Senza FBC lo schermo legge tutta l'immagine a ogni quadro (1920x1080
        # a 60 Hz: ~500 MB/s), e la conta l'I/O; a schermo spento, a riposo,
        # l'I/O e' il solo DMA dei dispositivi
        print("traffico RAM (MB/s): letture %.1f, scritture %.1f; richieste dai core %.1f, "
              "dalla grafica %.1f, dall'I/O %.1f" % (m["letture"], m["scritture"], m["core"],
                                                     m["grafica"], m["I/O"]))
    elif res.get("mem_error"):
        print("traffico RAM: non letto, %s" % res["mem_error"])
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
    if res.get("procs"):
        print("processi (% di un core): " + ", ".join("%s %.1f" % (n[:20], c) for c, n in res["procs"] if c >= 0.1))


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
    # Prima lo stato di tutti: leggere lo spazio di configurazione di un
    # dispositivo risveglia il ponte sopra di lui, e un dispositivo in D3cold
    devs = []
    for dev in sorted(glob.glob("/sys/bus/pci/devices/*")):
        devs.append({
            "dev": dev, "name": os.path.basename(dev),
            "parent": os.path.basename(os.path.dirname(os.path.realpath(dev))),
            "cls": rd(dev + "/class", "0x000000")[2:6],
            "ids": "%s:%s" % (rd(dev + "/vendor", "0x????")[2:], rd(dev + "/device", "0x????")[2:]),
            "drv": os.path.basename(os.path.realpath(dev + "/driver")) if os.path.exists(dev + "/driver") else "-",
            "dstate": rd(dev + "/power_state", "?"),
            "rt": rd(dev + "/power/runtime_status", "?"),
            "ctl": rd(dev + "/power/control", "?"),
        })
    state = {d["name"]: d for d in devs}
    for d in devs:
        dev, bdf, dstate, rt, ctl, drv = d["dev"], d["name"][5:], d["dstate"], d["rt"], d["ctl"], d["drv"]
        cls, ids = d["cls"], d["ids"]
        up = state.get(d["parent"])
        asleep = dstate == "D3cold" or (up is not None and (up["dstate"] != "D0" or up["rt"] != "active"))
        cfg = b""
        if not asleep:
            try:
                cfg = open(dev + "/config", "rb").read(256)
            except OSError:
                pass
        p = pcie_info(cfg)
        link = "(spento o sotto un ponte sospeso: non letto)" if asleep else ""
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


# Stato del collegamento: PxSSTS.IPM quando dice qualcosa (anche con DET
# diverso da 3: a riposo alcuni controller non riportano DET 3); -4 la PHY
# spenta (DET 4: Linux la spegne sulle porte senza disco con
# med_power_with_dipm e min_power); 100 + DET il resto
SATA_IPM = {1: "attivo", 2: "partial", 6: "slumber", 8: "devsleep", -4: "PHY spenta", 100: "giu'"}


def sata_state_name(k):
    if k in SATA_IPM:
        return SATA_IPM[k]
    return "giu' (DET %d)" % (k - 100) if k > 100 else "IPM %d" % k


def ata_name(host):
    parts = os.path.realpath(host).split("/")
    ata = [p for p in parts if re.fullmatch(r"ata\d+", p)]
    return ata[0] if ata else None


def ahci_port(host):
    """(BDF del controller, numero della porta) di un scsi_host SATA"""
    parts = os.path.realpath(host).split("/")
    ata = ata_name(host)
    bdfs = [p for p in parts if re.fullmatch(r"[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]", p)]
    port_no = rd("/sys/class/ata_port/%s/port_no" % ata) if ata else None
    if not bdfs or not port_no or not port_no.isdigit():
        return None
    return bdfs[-1], int(port_no) - 1


def ata_lpm_caps(host):
    """cosa dice il disco di se' (IDENTIFY, parole 76 e 78): HIPM, cioe'
    accetta le richieste del controller (ALPE), DIPM, cioe' porta lui il
    collegamento a riposo, DevSleep. Solo questi bit, non il resto"""
    ata = ata_name(host)
    if not ata:
        return None
    try:
        words = [int(w, 16) for w in (rd("/sys/class/ata_device/dev%s.0/id" % ata[3:]) or "").split()]
    except ValueError:
        return None
    if len(words) < 80 or words[76] in (0, 0xFFFF):
        return None
    yes = lambda v: "si'" if v else "no"
    return "disco: HIPM %s, DIPM %s, DevSleep %s" % (yes(words[76] & (1 << 9)), yes(words[78] & (1 << 3)),
                                                     yes(words[78] & (1 << 8)))


def sata_link_states(ports, seconds=2.0, interval=0.01):
    """quanto tempo ogni collegamento passa in ciascuno stato (PxSSTS.IPM),
    campionato dopo la misura degli stati C per non disturbarla. Lettura
    MMIO dell'ABAR del controller AHCI: serve iomem=relaxed"""
    maps, regs, err = {}, {}, None
    for key in ports:
        bdf, port = key
        try:
            if bdf not in maps:
                start, end = (int(x, 16) for x in open("/sys/bus/pci/devices/%s/resource" % bdf)
                              .read().splitlines()[5].split()[:2])
                if not start:
                    raise OSError("ABAR non assegnato")
                base = start & ~0xFFF
                size = min(end - start + 1, 0x1100) + (start - base)
                fd = os.open("/dev/mem", os.O_RDONLY | os.O_SYNC)
                try:
                    maps[bdf] = mmap.mmap(fd, (size + 0xFFF) & ~0xFFF, mmap.MAP_SHARED,
                                          mmap.PROT_READ, offset=base)
                finally:
                    os.close(fd)
                maps[bdf + "+"] = start - base
            # solo le porte che il controller dichiara (PI): le altre sono
            # spazio riservato
            if not struct.unpack_from("@I", maps[bdf], maps[bdf + "+"] + 0x0C)[0] & (1 << port):
                continue
            off = maps[bdf + "+"] + 0x100 + port * 0x80 + 0x28
            struct.unpack_from("@I", maps[bdf], off)
            regs[key] = (maps[bdf], off)
        except (OSError, ValueError, IndexError, OverflowError, struct.error) as e:
            err = "%s (serve iomem=relaxed?)" % e
    counts = {k: {} for k in regs}
    if regs:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            for k, (m, off) in regs.items():
                v = struct.unpack_from("@I", m, off)[0]
                det, ipm = v & 0xF, (v >> 8) & 0xF
                state = ipm if ipm in (1, 2, 6, 8) else (-4 if det == 4 else 100 + det)
                counts[k][state] = counts[k].get(state, 0) + 1
            time.sleep(interval)
    return counts, err


def print_storage_usb_audio():
    print("== SATA (link power management)")
    hosts = []
    for host in sorted(glob.glob("/sys/class/scsi_host/host*"), key=lambda h: int(h.rsplit("host", 1)[1])):
        pol = rd(host + "/link_power_management_policy")
        models = [rd(m) for m in glob.glob(host + "/device/target*/*/model")]
        if pol is not None:
            hosts.append((host, pol, models, ahci_port(host)))
    links, link_err = sata_link_states([p for _, _, _, p in hosts if p]) if hosts else ({}, None)
    for host, pol, models, port in hosts:
        # Una porta senza disco conta solo se c'e' qualcosa sul collegamento
        # (un disco tolto da Linux, o la PHY spenta), non le porte vuote
        if not models and set(links.get(port) or {100: 1}) == {100}:
            continue
        notes = []
        # Linux (da 6.13) spegne l'LPM sulle porte "esterne": PxCMD.HPCP, o
        # PxCMD.ESP con CAP.SXS, come le dichiara il firmware
        cmd, caps = rd(host + "/ahci_port_cmd"), rd(host + "/ahci_host_caps")
        try:
            cmd, caps = int(cmd, 16), int(caps, 16)
            if cmd & (1 << 18) or (cmd & (1 << 21) and caps & (1 << 5)):
                notes.append("porta esterna per il firmware (HPCP/ESP): niente LPM")
            # PxCMD.ALPE: il controller porta da solo il collegamento in
            # partial o slumber (ASP). Spento, puo' farlo ancora il disco
            # (DIPM, con med_power_with_dipm e min_power): conta lo stato
            # misurato qui sotto
            notes.append("ALPE %s" % (("acceso, " + ("slumber" if cmd & (1 << 27) else "partial"))
                                       if cmd & (1 << 26) else "spento"))
        except (TypeError, ValueError):
            pass
        if rd(host + "/link_power_management_supported") == "0":
            notes.append("LPM non supportato")
        caps = ata_lpm_caps(host)
        if caps:
            notes.append(caps)
        c = links.get(port)
        if c:
            n = sum(c.values())
            notes.append("collegamento: " + ", ".join(
                "%s %.0f%%" % (sata_state_name(ipm), cnt * 100 / n)
                for ipm, cnt in sorted(c.items(), key=lambda x: -x[1])))
        elif link_err:
            notes.append("stato del collegamento non letto: " + link_err)
        print("%-6s %-24s %s%s" % (os.path.basename(host), pol,
                                   ", ".join(m for m in models if m) or "(nessun disco)",
                                   "  [" + "; ".join(notes) + "]" if notes else ""))
    print("== USB (autosuspend)")
    for dev in sorted(glob.glob("/sys/bus/usb/devices/*")):
        name = os.path.basename(dev)
        if ":" in name or name.startswith("usb") or not os.path.exists(dev + "/idVendor"):
            continue
        print("%-8s %s:%s  %-32s %-5s %s" % (name, rd(dev + "/idVendor"), rd(dev + "/idProduct"),
                                            (rd(dev + "/product") or "?")[:32],
                                            rd(dev + "/power/control", "?"), rd(dev + "/power/runtime_status", "?")))
    # Wi-Fi: senza power save la scheda resta sveglia e riporta latenze (LTR)
    # brevi, che tengono sveglio anche il resto
    for net in sorted(glob.glob("/sys/class/net/*/wireless")):
        ifname = os.path.basename(os.path.dirname(net))
        drv = os.path.basename(os.path.realpath(os.path.dirname(net) + "/device/driver"))
        ps = "?"
        try:
            out = subprocess.run(["iw", "dev", ifname, "get", "power_save"], capture_output=True, text=True).stdout
            ps = out.split(":")[-1].strip() or "?"
        except OSError:
            pass
        params = ", ".join("%s=%s" % (os.path.basename(f), rd(f)) for f in
                           ("/sys/module/iwlmvm/parameters/power_scheme", "/sys/module/iwlwifi/parameters/power_save")
                           if os.path.exists(f))
        print("== Wi-Fi %s (%s): power save %s%s" % (ifname, drv, ps, "; " + params if params else ""))
    ps = rd("/sys/module/snd_hda_intel/parameters/power_save")
    if ps is not None:
        # 0: i codec non si spengono mai, e il controller resta in D0
        print("== Audio HDA: power_save %s s%s, controller %s" % (
            ps, " (codec sempre accesi)" if ps == "0" else "",
            rd("/sys/module/snd_hda_intel/parameters/power_save_controller", "?")))
    # Con lo schermo acceso la RAM va in self-refresh, e il package sotto PC2,
    # solo se i915 abilita i watermark LP; le loro latenze vengono da MCHBAR
    # SSKPD, che scrive il firmware (mrc.bin o la RAM init nativa)
    for f, title, n in (("i915_sr_status", "self-refresh", 2), ("i915_pri_wm_latency", "latenze WM", 5),
                        ("i915_fbc_status", "FBC", 3), ("i915_edp_psr_status", "PSR", 3)):
        for path in glob.glob("/sys/kernel/debug/dri/*/" + f)[:1]:
            lines = [" ".join(l.split()) for l in (rd(path, "") or "").splitlines() if l.strip()][:n]
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
        imc = Imc() if fam == 6 and model in IMC_MODELS else None
        print("misura per %.0f s, non toccare il portatile..." % args.seconds, file=sys.stderr, flush=True)
        res = sample(msr, cpus, args.seconds, imc)
        if imc is not None and imc.error:
            res["mem_error"] = imc.error
        print_cstates(msr, cpus, res, fam, model)
    print_pci()
    print_storage_usb_audio()


if __name__ == "__main__":
    main()
