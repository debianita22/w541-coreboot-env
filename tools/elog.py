#!/usr/bin/env python3
# elog.py - il registro degli eventi di coreboot (regione RW_ELOG) da un dump
# del flash, senza elogtool: avvii, wake, entrate in S3/S5 e, se un avvio si
# e' bloccato, l'ultimo codice POST che aveva scritto in CMOS.
#
#   sudo modprobe -r spi_intel_platform spi_intel
#   sudo flashrom -p internal -r dump.rom
#   python3 tools/elog.py dump.rom
#
# Il file puo' essere l'immagine intera (12 MiB), il chip da 8 MiB o solo la
# regione RW_ELOG (flashrom --fmap -i RW_ELOG -r). Gli orari sono quelli
# dell'RTC, senza fuso: in UTC se l'RTC e' in UTC (Linux), in ora locale se
# l'ha scritto Windows.
import struct
import sys

# Gli eventi che coreboot scrive su questa macchina
# (commonlib/bsd/include/commonlib/bsd/elog.h)
EVENTS = {
    0x16: "Log area cleared",
    0x17: "System boot",
    0x92: "Power fail",
    0x93: "SUS power fail",
    0x94: "PWROK fail",
    0x95: "SYS_PWROK fail",
    0x96: "Power on",
    0x97: "Power button",
    0x98: "Power button override",
    0x99: "Reset button",
    0x9a: "System reset",
    0x9b: "RTC reset",
    0x9c: "TCO reset",
    0x9d: "ACPI enter",
    0x9e: "ACPI wake",
    0x9f: "Wake source",
    0xa2: "Management Engine",
    0xa3: "Last post code",
    0xa4: "Management Engine extra",
    0xa6: "POST extra",
    0xaa: "Memory cache update",
    0xab: "Thermal trip",
}
WAKE_SOURCES = {
    0x00: "PCIe", 0x01: "PME", 0x02: "PME internal", 0x03: "RTC alarm",
    0x04: "GPE", 0x05: "SMBus", 0x06: "power button",
}
ME_PATHS = {0: "normal", 1: "S3 wake", 2: "error", 3: "recovery", 4: "disabled",
            5: "firmware update"}

# I codici POST degli stadi di coreboot su questa macchina (patch 0038-0042
# e coreboot): dove era arrivato un avvio che si e' fermato
POST_CODES = {
    0x21: "bootblock: banco CMOS appena scelto, prima di romstage",
    0x30: "romstage: inizio",
    0x31: "romstage: dopo early_pch_init()",
    0x32: "romstage: dopo haswell_early_initialization()",
    0x33: "romstage: avvio normale riconosciuto",
    0x35: "romstage: ripresa da S3 riconosciuta",
    0x3a: "romstage: prima della RAM init (mrc.bin o NRI)",
    0x3b: "romstage: mrc.bin finito, prima del recupero di CBMEM",
    0x3c: "romstage: RAM init finita (CBMEM recuperato se S3)",
    0x3d: "romstage: dopo romstage_handoff_init()",
    0x3f: "romstage: fine, caricamento di postcar",
    0x11: "postcar: caricamento di ramstage",
    0x12: "postcar: ramstage caricato, salto",
    0x39: "ramstage: console pronta",
    0x6f: "ramstage: hardwaremain",
    0x70: "ramstage: BS_PRE_DEVICE",
    0x71: "ramstage: BS_DEV_INIT_CHIPS",
    0x72: "ramstage: BS_DEV_ENUMERATE",
    0x73: "ramstage: BS_DEV_RESOURCES",
    0x74: "ramstage: BS_DEV_ENABLE",
    0x75: "ramstage: BS_DEV_INIT (anche CPU e SMM)",
    0x76: "ramstage: BS_POST_DEVICE",
    0x77: "ramstage: BS_OS_RESUME_CHECK",
    0x78: "ramstage: BS_OS_RESUME",
    0x79: "ramstage: BS_WRITE_TABLES",
    0x7a: "ramstage: BS_PAYLOAD_LOAD",
    0x7b: "ramstage: BS_PAYLOAD_BOOT",
    0x93: "ramstage: MTRR",
    0xe2: "mrc.bin ha restituito un errore",
    0xed: "TPM: setup fallito",
    0xef: "ripresa da S3 fallita, reset (niente dati di mrc.bin o CBMEM)",
    0xf8: "payload avviato",
    0xfd: "salto al vettore di wake dell'OS",
    0xfe: "OS avviato",
}
DEVICE_PATHS = {1: "ROOT", 2: "PCI", 3: "PNP", 4: "I2C", 5: "APIC", 6: "DOMAIN",
                7: "CPU_CLUSTER", 8: "CPU", 9: "CPU_BUS", 10: "IOAPIC", 11: "GENERIC"}


def die(msg):
    sys.exit("elog.py: " + msg)


def bcd(b):
    return (b >> 4) * 10 + (b & 0xf)


def find_elog(data):
    """L'offset della regione RW_ELOG nel file."""
    if data[:4] == b"ELOG":
        return 0
    # la FMAP: "__FMAP__", versione, base, dimensione, nome, aree
    i = data.find(b"__FMAP__")
    while i >= 0:
        try:
            nareas = struct.unpack_from("<H", data, i + 54)[0]
            for a in range(nareas):
                off, size, name = struct.unpack_from("<II32s", data, i + 56 + a * 42)
                if name.rstrip(b"\0") == b"RW_ELOG" and data[off:off + 4] == b"ELOG":
                    return off
        except struct.error:
            pass
        i = data.find(b"__FMAP__", i + 1)
    # senza FMAP (il chip da 8 MiB): l'intestazione in un blocco da 4 KiB
    for off in range(0, len(data) - 8, 0x1000):
        if data[off:off + 4] == b"ELOG" and data[off + 4] == 1 and data[off + 5] == 8:
            return off
    die("regione RW_ELOG non trovata")


def post_extra(value):
    kind = value >> 24
    if kind == 0x01:
        t = (value >> 16) & 0xff
        name = DEVICE_PATHS.get(t, "tipo %d" % t)
        if t == 2:
            return "dispositivo PCI %02x:%02x.%x" % ((value >> 8) & 0xff, (value >> 3) & 0x1f,
                                                    value & 7)
        return "dispositivo %s %04x" % (name, value & 0xffff)
    if value >> 8 == 0x4d5243:
        return "dentro mrc.bin, dopo %d righe del suo log" % (value & 0xff)
    return "0x%08x" % value


def describe(etype, d):
    if etype == 0x16 and len(d) >= 2:
        return "%d byte" % struct.unpack_from("<H", d)[0]
    if etype == 0x17 and len(d) >= 4:
        return "%d" % struct.unpack_from("<I", d)[0]
    if etype in (0x9d, 0x9e) and d:
        return "S%d" % d[0]
    if etype == 0x9f and len(d) >= 5:
        src = WAKE_SOURCES.get(d[0], "0x%02x" % d[0])
        inst = struct.unpack_from("<I", d, 1)[0]
        return src if not inst else "%s %d" % (src, inst)
    if etype == 0xa2 and d:
        return ME_PATHS.get(d[0], "0x%02x" % d[0])
    if etype == 0xa3 and len(d) >= 2:
        code = struct.unpack_from("<H", d)[0]
        return "0x%02x: %s" % (code, POST_CODES.get(code, "?"))
    if etype == 0xa6 and len(d) >= 4:
        return post_extra(struct.unpack_from("<I", d)[0])
    if etype == 0xaa and len(d) >= 2:
        return "%s, %s" % ("recovery" if d[0] else "normal", "ok" if d[1] == 0 else "failed")
    return d.hex()


def main():
    if len(sys.argv) != 2 or sys.argv[1] in ("-h", "--help"):
        sys.exit("uso: python3 tools/elog.py dump.rom (l'immagine del flash o solo RW_ELOG)")
    with open(sys.argv[1], "rb") as f:
        data = f.read()
    base = find_elog(data)
    hdr = data[base + 5]
    p = base + hdr
    end = min(len(data), base + 0x4000)
    n = 0
    while p + 9 <= end and data[p] != 0xff:
        etype, length = data[p], data[p + 1]
        if length < 9 or p + length > end:
            print("evento %d: lunghezza %d non valida, fine" % (n, length))
            break
        ev = data[p:p + length]
        y, mo, dd, h, mi, s = (bcd(b) for b in ev[2:8])
        bad = "" if sum(ev) & 0xff == 0 else "  [checksum errato]"
        name = EVENTS.get(etype, "tipo 0x%02x" % etype)
        print("%3d  20%02d-%02d-%02d %02d:%02d:%02d  %-24s %s%s"
              % (n, y, mo, dd, h, mi, s, name, describe(etype, ev[8:-1]), bad))
        p += length
        n += 1
    print("%d eventi, %d byte usati" % (n, p - base))


if __name__ == "__main__":
    main()
