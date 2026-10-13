#!/bin/bash
# ci-check.sh - i controlli veloci, gli stessi della CI (check.yml e il job
# setup di build.yml). Da lanciare anche a mano prima di un push: pochi
# secondi, nessuna rete.
#
#   ./tools/ci-check.sh
#
#   1. shellcheck (livello warning) sugli script, riconosciuti dalla prima riga
#   2. Python: la sintassi dei file .py, la VPD che scrive tools/vpd.py
#      riletta uguale, una capsula letta da tools/update.py come la scrive
#      GenerateCapsule, e il suo pacchetto fwupd (tools/fwupd-cab.py)
#   3. i workflow (actionlint, se c'e')
#   4. blobs/, assets/ e legacy/: i file corrispondono ai loro SHA256SUMS
#   5. patches/series, patches/edk2/series e patches/lvglpkg/series: ogni
#      patch elencata c'e', ogni .patch della cartella e' elencata, ognuna e'
#      una mail di git format-patch
#   6. la chiave Optimus NVIDIA solo in blobs/opvk.inc (lista di byte con la
#      firma della chiave): non nelle patch ne' in altri file
#   7. i due defconfig: diversi solo nella RAM init e nel GUID dell'ESRT
#      (cosi' mrc e nri differiscono solo li'), con IFD, ME e GbE (immagine
#      completa), EDK2 pinnato a un commit, i file in w541/ che
#      tools/build.sh copia davvero; la mappa configs/w541.fmd con le regioni
#      scrivibili nel chip da 8 MiB; i GUID e le regioni delle capsule uguali
#      in tools/update.py e tools/build.sh
#   9. le chiavi: in keys/ solo certificati e chiavi pubbliche di vboot, il
#      keyblock con la chiave dati di keys/vboot, nessuna chiave privata nei
#      file del repository
#   8. il pin di coreboot in tools/build.sh: commit intero e describe coerente,
#      lo stesso della parte RO gia' sui portatili (configs/ro-interface.txt:
#      gli slot di una capsula partono dopo quella)
# L'applicazione delle patch e i .config li prova check.yml (job "patch").
set -u
O="$(cd "$(dirname "$0")/.." && pwd)"
cd "${O}" || exit 1
fail=0
step() { printf '\n== %s\n' "$*"; }
bad()  { printf '  NO  %s\n' "$*"; fail=1; }
ok()   { printf '  ok  %s\n' "$*"; }

# i file del repository: quelli di git se c'e', altrimenti tutto tranne .git
files() {
	if git -C "${O}" rev-parse --git-dir >/dev/null 2>&1; then
		git -C "${O}" ls-files
	else
		find . -path ./.git -prune -o -path ./work -prune -o -path ./dist -prune -o -type f -print | sed 's|^\./||'
	fi
}

step "shellcheck"
if command -v shellcheck >/dev/null 2>&1; then
	sh_files=()
	while IFS= read -r f; do
		[ -f "${f}" ] || continue
		head -1 "${f}" 2>/dev/null | grep -qE '^#!(/usr/bin/env[[:space:]]+|/bin/)(ba)?sh([[:space:]]|$)' \
			&& sh_files+=("${f}")
	done < <(files)
	if shellcheck -S warning "${sh_files[@]}"; then
		ok "${#sh_files[@]} script"
	else
		bad "shellcheck: vedi sopra"
	fi
else
	bad "shellcheck non installato (Debian/Ubuntu: apt install shellcheck)"
fi

step "Python"
if command -v python3 >/dev/null 2>&1; then
	py_files=()
	while IFS= read -r f; do
		case "${f}" in *.py) [ -f "${f}" ] && py_files+=("${f}") ;; esac
	done < <(files)
	if python3 - "${py_files[@]}" <<'EOF'
import ast
import sys
for f in sys.argv[1:]:
    with open(f, encoding="utf-8") as fh:
        ast.parse(fh.read(), f)
EOF
	then
		ok "${#py_files[@]} file .py"
	else
		bad "Python: errore di sintassi, vedi sopra"
	fi
	# La regione RO_VPD che scrive tools/vpd.py (lunghezze di uno e piu'
	# byte, UUID binario) si rilegge uguale, e una regione vuota resta vuota
	if python3 - <<'EOF'
import sys
import uuid
sys.dont_write_bytecode = True
sys.path.insert(0, "tools")
import vpd
entries = [("serial_number", b"R90ABCDE"), ("system_uuid", uuid.uuid4().bytes_le),
           ("k" * 200, b"v" * 300)]
assert vpd.parse_region(vpd.encode_region(entries, 0x4000)) == (entries, 0)
assert vpd.parse_region(b"\xff" * 0x4000) == ([], None)
EOF
	then
		ok "tools/vpd.py: VPD scritta e riletta"
	else
		bad "tools/vpd.py: la VPD scritta non si rilegge uguale"
	fi
	# Una capsula con la struttura di GenerateCapsule (firma finta) letta da
	# tools/update.py: variante, versioni, regioni del manifest RMAP, immagine
	if python3 - <<'EOF'
import struct
import sys
import uuid
sys.dont_write_bytecode = True
sys.path.insert(0, "tools")
import update
image = bytes(range(256)) * (update.ROM_SIZE // 256)
rmap = b"".join(r.encode().ljust(16, b"\0") for r in update.REGIONS)
rmap += struct.pack("<IHH", update.RMAP_SIGNATURE, 1, len(update.REGIONS))
payload = struct.pack("<4sIII", b"MSS1", 16, 0x01020003, 0x01020000) + image + rmap
cert = b"\x30" * 100
auth = struct.pack("<QIHH", 7, 24 + len(cert), 0x200, 0x0EF1) + update.PKCS7_CERT_GUID.bytes_le + cert
item = auth + payload
ihdr = struct.pack("<I16sB3sIIQQ", 3, update.VARIANTS["nri"].bytes_le, 1, b"", len(item), 0, 0, 1)
fmp = struct.pack("<IHHQ", 1, 0, 1, 16) + ihdr + item
data = update.FMP_CAPSULE_GUID.bytes_le + struct.pack("<III", 32, 0x10000, 32 + len(fmp)) + bytes(4) + fmp
cap = update.parse_capsule(data)
assert (cap["variant"], cap["version"], cap["lsv"]) == ("nri", 0x01020003, 0x01020000)
assert cap["regions"] == update.REGIONS and cap["image"] == image and cap["signature"] == cert
assert cap["signed"] == payload + struct.pack("<Q", 7)
update.check_capsule(cap, {"variant": "nri", "legacy": False})
assert update.version_str(0x01020003) == "v1.2.3"
# il pacchetto fwupd di tools/fwupd-cab.py, riletto da tools/update.py
import datetime, importlib.util
spec = importlib.util.spec_from_file_location("fwupd_cab", "tools/fwupd-cab.py")
fc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fc)
meta = fc.metainfo("nri", str(update.VARIANTS["nri"]), "1.2.3", "2026-10-11", "https://example.org/")
cab = fc.make_cab([("firmware.bin", data), ("firmware.metainfo.xml", meta.encode())],
                  datetime.datetime(2026, 10, 11))
files = update.read_cab(cab)
assert files == {"firmware.bin": data, "firmware.metainfo.xml": meta.encode()}
capdata, cap2 = update.load_cab("prova.cab", cab)
assert capdata == data and cap2["version"] == 0x01020003 and cap2["cab"]["meta"]["format"] == "triplet"
EOF
	then
		ok "tools/update.py: capsula e pacchetto fwupd letti"
	else
		bad "tools/update.py: la capsula di prova non si legge come atteso"
	fi
	# I tag di GitHub si ordinano (stage --latest), e rollback trova nello
	# slot che non gira la release a cui tornare, solo se e' piu' vecchia
	if python3 - <<'EOF'
import struct
import sys
sys.dont_write_bytecode = True
sys.path.insert(0, "tools")
import update
assert update.release_key("v1.3.1-mrc") < update.release_key("v1.3.2-rc1-mrc") < update.release_key("v1.3.2-mrc")
assert update.build_release_key("ouroBIOS-v1.3.2-rc1-nri") < update.build_release_key("ouroBIOS-v1.3.2-nri")
areas = [("RW_NVRAM", 0x5A0000, 0x10000), ("RW_SECTION_A", 0x5C0000, 0x2D0000), ("FW_MAIN_A", 0x5D0000, 0x2C0000),
         ("RW_SECTION_B", 0x890000, 0x2D0000), ("FW_MAIN_B", 0x8A0000, 0x2C0000)]
def flash(a, b):
    img = bytearray(b"\xff" * update.ROM_SIZE)
    fmap = b"__FMAP__" + struct.pack("<BBQI32sH", 1, 1, 0, update.ROM_SIZE, b"FLASH", len(areas))
    fmap += b"".join(struct.pack("<II32sH", o, n, name.encode(), 0) for name, o, n in areas)
    img[0xB60000:0xB60000 + len(fmap)] = fmap
    img[0x5D0100:0x5D0100 + len(a)] = a
    img[0x8A0100:0x8A0100 + len(b)] = b
    rec = bytearray(16)                 # vboot: slot B provato e confermato
    rec[0], rec[7] = 0x40, (1 << 3) | (1 << 2) | 2
    rec[15] = update.nv_crc8(bytes(rec[:15]))
    img[0x5A0000:0x5A0010] = rec
    return bytes(img)
plan = update.rollback_plan(flash(b"ouroBIOS-v1.3.1-mrc", b"ouroBIOS-v1.3.2-mrc"), "ouroBIOS-v1.3.2-mrc")
assert (plan["current"], plan["other"], plan["tag"]) == ("B", "A", "v1.3.1-mrc")
for a, b in ((b"ouroBIOS-v1.3.3-mrc", b"ouroBIOS-v1.3.2-mrc"), (b"ouroBIOS-v1.3.2-mrc", b"ouroBIOS-v1.3.2-mrc"),
             (b"", b"ouroBIOS-v1.3.2-mrc"), (b"ouroBIOS-v1.3.1-nri", b"ouroBIOS-v1.3.2-mrc")):
    try:
        update.rollback_plan(flash(a, b), "ouroBIOS-v1.3.2-mrc")
    except update.Error:
        continue
    raise AssertionError("rollback accettato: %r" % a)
EOF
	then
		ok "tools/update.py: release di GitHub in ordine, rollback verso la release dell'altro slot"
	else
		bad "tools/update.py: release_key o rollback_plan non fanno quello che devono"
	fi
	# tools/qemu-update.py senza QEMU: le variabili che scrive nello store del
	# flash (OsIndications) come le legge tools/update.py, FmpState, l'ESRT
	# della seriale e la traccia del pflash
	if python3 - <<'EOF'
import importlib.util
import os
import struct
import sys
import tempfile
import uuid
sys.dont_write_bytecode = True
sys.path.insert(0, "tools")
import update
spec = importlib.util.spec_from_file_location("qemu_update", "tools/qemu-update.py")
qu = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qu)
areas = [("RW_MRC_CACHE", 0x0, 0x10000), ("SMMSTORE", 0x10000, 0x80000), ("RW_NVRAM", 0x90000, 0x10000),
         ("RW_SECTION_A", 0xA0000, 0x360000), ("FW_MAIN_A", 0xB0000, 0x34FFC0),
         ("RW_SECTION_B", 0x400000, 0x360000), ("FW_MAIN_B", 0x410000, 0x34FFC0), ("WP_RO", 0x760000, 0xA0000)]
img = bytearray(b"\xff" * 0x800000)
fmap = b"__FMAP__" + struct.pack("<BBQI32sH", 1, 1, 0, len(img), b"FLASH", len(areas))
fmap += b"".join(struct.pack("<II32sH", o, n, name.encode(), 0) for name, o, n in areas)
img[0x760000:0x760000 + len(fmap)] = fmap
st = 0x10000                       # lo store come lo formatta il payload
img[st + 0x20:st + 0x28] = struct.pack("<Q", 0x80000)
img[st + 0x28:st + 0x2C] = b"_FVH"
img[st + 0x30:st + 0x32] = struct.pack("<H", 0x48)
img[st + 0x48:st + 0x64] = update.AUTH_VAR_GUID + struct.pack("<IBBHI", 0x80000 - 0x48, 0x5A, 0xFE, 0, 0)
tmp = tempfile.TemporaryDirectory()
path = os.path.join(tmp.name, "flash.rom")
open(path, "wb").write(img)
f = qu.Flash(path)
f.set_var("OsIndications", qu.EFI_GLOBAL, 7, struct.pack("<Q", 4))
f.set_var("OsIndications", qu.EFI_GLOBAL, 7, struct.pack("<Q", 0))
f.set_var("OsIndications", qu.EFI_GLOBAL, 7, struct.pack("<Q", 4 | 1))
assert f.os_indications() == 5
found, info = update.var_store(f.region("SMMSTORE"))
states = [v["state"] for v in found if v["name"] == "OsIndications"]
assert states == [0x3D, 0x3D, 0x3F], states
guid = uuid.UUID("b195f75a-8f95-4282-950c-ee9dd1099ec9").bytes_le
f.set_var("FmpState", guid, 7, struct.pack("<4B4I", 1, 1, 1, 1, 0x01010000, 0x01000000, 0, 0x01020000))
assert f.fmp_state(guid) == {"version": 0x01010000, "lsv": 0x01000000, "status": 0, "attempt": 0x01020000}
row = ("| B195F75A-8F95-4282-950C-EE9DD1099EC9 | System FW    | 0x 1010000 | 0x 1000000 | 0x   14000 "
       "| 0x 1020000 | 0x       0 |")
assert qu.ESRT_ROW.findall(row) == [("B195F75A-8F95-4282-950C-EE9DD1099EC9", "System FW", "1010000", "1000000",
                                      "14000", "1020000", "0")]
trace = os.path.join(tmp.name, "pflash.trace")
open(trace, "w").write("pflash_data_write system.flash0: data offset:0x10010 size:1 value:0x0000\n"
                       "pflash_write_block_erase system.flash0: block erase offset:0x400000 bytes:0x1000\n"
                       "pflash_data_write system.flash0: data offset:0x90000 size:1 value:0x0040\n"
                       "pflash_data_write_block system.flash0: data offset:0xa0010 size:1 value:0x00ff "
                       "counter:0x0000000000000010\n")
hits, total = qu.traced_writes(trace, f.areas, ("WP_RO", "RW_SECTION_A", "RW_SECTION_B"))
assert total == 4 and hits == {"WP_RO": 0, "RW_SECTION_A": 1, "RW_SECTION_B": 0x1000}, (hits, total)
tmp.cleanup()
# il nome delle ROM (build.sh) e la pagina Firmware Update (edk2/0020) nella seriale
import re
assert re.fullmatch(qu.TAG_RE, "ouroBIOS-v1.3.0-mrc+update-test-D")
line = ("FirmwareUpdate: slot A runs ouroBIOS-v1.3.0-mrc+update-test-D, slot B holds "
        "ouroBIOS-v1.1.0-mrc+update-test-B\r\n")
assert qu.UpdateTest.FW_PAGE.search(line).groups() == ("A", "ouroBIOS-v1.3.0-mrc+update-test-D", "B",
                                                        "ouroBIOS-v1.1.0-mrc+update-test-B")
EOF
	then
		ok "tools/qemu-update.py: variabili scritte nello store, FmpState, ESRT, traccia del pflash e pagina Firmware Update letti"
	else
		bad "tools/qemu-update.py: le funzioni della prova dell'aggiornamento non fanno quello che devono"
	fi
else
	bad "python3 non installato"
fi

step "workflow (actionlint)"
if command -v actionlint >/dev/null 2>&1; then
	if actionlint .github/workflows/*.yml; then ok "workflow"; else bad "actionlint: vedi sopra"; fi
else
	echo "  (actionlint non installato: salto. pip install actionlint-py)"
fi

step "SHA256SUMS"
for d in blobs assets legacy/coreboot-4.22; do
	if [ ! -f "${d}/SHA256SUMS" ]; then bad "${d}/SHA256SUMS non c'e'"; continue; fi
	if (cd "${d}" && sha256sum --quiet -c SHA256SUMS); then
		ok "${d}: $(wc -l < "${d}/SHA256SUMS") file"
	else
		bad "${d}: i file non corrispondono a SHA256SUMS"
	fi
	# ogni file della cartella (tranne i README) e' elencato
	for f in "${d}"/*; do
		case "${f##*/}" in SHA256SUMS|README*) continue ;; esac
		grep -q "  ${f##*/}\$" "${d}/SHA256SUMS" || bad "${f}: non e' in ${d}/SHA256SUMS"
	done
done

# Una serie: la cartella $1 (patches, patches/edk2, ...) con il suo series
check_series() {
	local d="$1" p n=0 dups
	local -a listed
	if [ ! -f "${d}/series" ]; then bad "${d}/series non c'e'"; return; fi
	mapfile -t listed < <(sed -e 's/#.*//' -e 's/[[:space:]]*$//' "${d}/series" | sed '/^$/d')
	for p in "${listed[@]}"; do
		n=$((n + 1))
		if [ ! -f "${d}/${p}" ]; then bad "${p}: in ${d}/series ma non in ${d}/"; continue; fi
		head -1 "${d}/${p}" | grep -q '^From [0-9a-f]\{40\} ' || bad "${d}/${p}: non e' una mail di git format-patch"
		grep -q '^Subject: \[PATCH' "${d}/${p}" || bad "${d}/${p}: senza Subject: [PATCH"
		grep -q '^diff --git ' "${d}/${p}" || bad "${d}/${p}: senza diff"
	done
	for p in "${d}"/*.patch; do
		[ -e "${p}" ] || continue
		printf '%s\n' "${listed[@]}" | grep -qxF "${p#"${d}/"}" || bad "${p}: non e' in ${d}/series"
	done
	dups="$(printf '%s\n' "${listed[@]}" | sort | uniq -d)"
	[ -z "${dups}" ] || bad "${d}/series: ripetute ${dups}"
	ok "${d}: ${n} patch in series"
}

step "serie di patch"
check_series patches
check_series patches/edk2
check_series patches/lvglpkg
for p in patches/optional/*.patch; do
	head -1 "${p}" | grep -q '^From [0-9a-f]\{40\} ' || bad "${p}: non e' una mail di git format-patch"
done
ok "$(find patches/optional -name '*.patch' | wc -l) patch opzionali"
# la patch per q35 della prova dell'aggiornamento (build.sh update-test):
# solo nel worktree di quella prova, quindi in nessuna serie
t="$(sed -n 's/^UPDATE_TEST_PATCH=\(.*\)$/\1/p' tools/build.sh)"
if [ -n "${t}" ] && [ -f "${t}" ] && grep -q '^diff --git ' "${t}"; then
	ok "${t}: la patch della prova dell'aggiornamento"
else
	bad "tools/build.sh: UPDATE_TEST_PATCH '${t}' non c'e' o non e' una patch"
fi
for p in patches/test/*.patch; do
	[ "${p}" = "${t}" ] || bad "${p}: in patches/test ma non usata da tools/build.sh"
done
# i comandi SMMSTORE dei due slot di vboot: lo stesso numero in coreboot
# (src/include/smmstore.h), in SmmStoreLib di EDK2 e nell'applicazione della
# prova che fa da sistema operativo
for c in SMMSTORE_CMD_VBOOT_SLOT_GOOD SMMSTORE_CMD_VBOOT_TRY_OTHER SMMSTORE_CMD_VBOOT_LOCK; do
	v="$(cat patches/*.patch patches/edk2/*.patch tools/efi/*.c \
		| sed -n "s/^[+]\{0,1\}#define[[:space:]]\{1,\}${c}[[:space:]]\{1,\}\([0-9]\{1,\}\)[[:space:]]*\$/\1/p" \
		| tr -d '\r' | sort -u)"
	n="$(printf '%s\n' "${v}" | grep -c .)"
	if [ "${n}" = 1 ]; then ok "${c} = ${v} in coreboot, EDK2 e tools/efi"; else bad "${c}: numeri diversi o assente (${v//$'\n'/ })"; fi
done

step "chiave Optimus NVIDIA"
# blobs/opvk.inc ha la chiave che il driver NVIDIA di Windows chiede alla dGPU
# (NVOP 0x10), estratta dal DSDT del firmware Lenovo: byte separati da
# virgola, che il DSDT include (patch 0036). Deve esserci solo li': non nelle
# patch e in nessun altro file, sotto nessun nome.
if [ ! -f blobs/opvk.inc ]; then
	bad "blobs/opvk.inc non c'e'"
elif ! tr -d ' \t\r\n' < blobs/opvk.inc | grep -Eqx '(0x[0-9a-fA-F]{2},)*0x[0-9a-fA-F]{2},?'; then
	bad "blobs/opvk.inc: non e' una lista di byte 0x.. separati da virgola"
else
	ok "blobs/opvk.inc: $(tr -d ' \t\r\n' < blobs/opvk.inc | tr ',' '\n' | grep -c .) byte"
fi
k="$(files | grep -i 'opvk' | grep -vx 'blobs/opvk.inc' || true)"
if [ -n "${k}" ]; then bad "altri file della chiave nel repository: ${k}"; else ok "nessun altro opvk.inc"; fi
if grep -q '^+++ b/.*opvk\.inc' patches/optional/*.patch patches/*.patch patches/edk2/*.patch patches/lvglpkg/*.patch \
	patches/test/*.patch 2>/dev/null; then
	bad "una patch crea opvk.inc"
else
	ok "nessuna patch crea opvk.inc"
fi
# e il contenuto, come byte in esadecimale: "NVIDIA Certified" e' nel testo
# della chiave (la sequenza si costruisce qui, cosi' non compare in questo
# file)
sig="$(printf 'NVIDIA Certified' | od -An -tx1 | tr -s ' \n' ' ' | sed -e 's/^ //' -e 's/ $//' -e 's/ /,0x/g' -e 's/^/0x/')"
if [ -f blobs/opvk.inc ] && ! tr -d ' \t\r\n+' < blobs/opvk.inc | grep -qiF "${sig}"; then
	bad "blobs/opvk.inc: non contiene la firma della chiave"
fi
k=""
while IFS= read -r f; do
	[ -f "${f}" ] || continue
	[ "${f}" = blobs/opvk.inc ] && continue
	grep -Iq . "${f}" 2>/dev/null || continue
	if tr -d ' \t\r\n+' < "${f}" | grep -qiF "${sig}"; then
		k+=" ${f}"
	fi
done < <(files)
if [ -n "${k}" ]; then bad "byte della chiave fuori da blobs/opvk.inc:${k}"; else ok "byte della chiave solo in blobs/opvk.inc"; fi

step "defconfig"
ram='^CONFIG_(HAVE_MRC|MRC_FILE|HASWELL_HIDE_PEG_FROM_MRC|USE_NATIVE_RAMINIT|DRIVERS_EFI_MAIN_FW_GUID)='
for v in mrc nri; do
	[ -f "configs/w541-${v}.defconfig" ] || bad "configs/w541-${v}.defconfig non c'e'"
done
if [ -f configs/w541-mrc.defconfig ] && [ -f configs/w541-nri.defconfig ]; then
	d="$(diff <(grep '^CONFIG_' configs/w541-mrc.defconfig | grep -Ev "${ram}") \
		<(grep '^CONFIG_' configs/w541-nri.defconfig | grep -Ev "${ram}"))"
	if [ -z "${d}" ]; then ok "mrc e nri uguali tranne la RAM init e il GUID"; else bad "mrc e nri diversi fuori dalla RAM init e dal GUID:"; echo "${d}"; fi
	# il GUID dell'ESRT di ogni variante: diverso, e quello di tools/update.py
	g_mrc="$(sed -n 's/^CONFIG_DRIVERS_EFI_MAIN_FW_GUID="\(.*\)"$/\1/p' configs/w541-mrc.defconfig)"
	g_nri="$(sed -n 's/^CONFIG_DRIVERS_EFI_MAIN_FW_GUID="\(.*\)"$/\1/p' configs/w541-nri.defconfig)"
	guid_re='^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
	if [[ "${g_mrc}" =~ ${guid_re} ]] && [[ "${g_nri}" =~ ${guid_re} ]] && [ "${g_mrc}" != "${g_nri}" ]; then
		ok "GUID dell'ESRT: mrc ${g_mrc}, nri ${g_nri}"
	else
		bad "CONFIG_DRIVERS_EFI_MAIN_FW_GUID: due GUID diversi, minuscoli (mrc '${g_mrc}', nri '${g_nri}')"
	fi
	for v in mrc nri; do
		g="g_${v}"
		grep -qE "^ +\"${v}\": uuid\.UUID\(\"${!g}\"\),$" tools/update.py \
			|| bad "tools/update.py: VARIANTS[\"${v}\"] non e' ${!g}"
	done
	r_build="$(sed -n 's/^CAPSULE_REGIONS=(\(.*\))$/\1/p' tools/build.sh)"
	r_upd="$(sed -n 's/^REGIONS = \[\(.*\)\]$/\1/p' tools/update.py | tr -d '",')"
	if [ -n "${r_build}" ] && [ "${r_build}" = "${r_upd}" ]; then
		ok "regioni delle capsule: ${r_build}"
	else
		bad "regioni delle capsule: tools/build.sh '${r_build}', tools/update.py '${r_upd}'"
	fi
	# il certificato di cui si fida il payload: relativo all'albero di EDK2,
	# e' w541/<file> dell'albero di coreboot, copiato da tools/build.sh
	for v in mrc nri; do
		grep -qx 'CONFIG_DRIVERS_EFI_CAPSULE_TRUSTED_PUBLIC_CERT="../../../../../w541/capsule-signing.pem"' \
			"configs/w541-${v}.defconfig" || bad "w541-${v}: CONFIG_DRIVERS_EFI_CAPSULE_TRUSTED_PUBLIC_CERT non e' w541/capsule-signing.pem"
	done
	grep -qxF capsule-signing.pem <<< "$(sed -n '/^TREE_FILES=(/,/)/p' tools/build.sh | tr ' \t()' '\n\n\n\n' | sed -n 's|^.*/||p')" \
		|| bad "keys/capsule-signing.pem non e' in TREE_FILES di tools/build.sh"
	grep -qx 'CONFIG_HAVE_MRC=y' configs/w541-mrc.defconfig || bad "w541-mrc: senza CONFIG_HAVE_MRC=y"
	grep -qx 'CONFIG_USE_NATIVE_RAMINIT=y' configs/w541-nri.defconfig || bad "w541-nri: senza CONFIG_USE_NATIVE_RAMINIT=y"
	grep -q '^CONFIG_HAVE_MRC=y' configs/w541-nri.defconfig && bad "w541-nri: con CONFIG_HAVE_MRC=y"
	for v in mrc nri; do
		for sym in HAVE_IFD_BIN HAVE_ME_BIN HAVE_GBE_BIN; do
			grep -qx "CONFIG_${sym}=y" "configs/w541-${v}.defconfig" \
				|| bad "w541-${v}: senza CONFIG_${sym}=y (l'immagine deve essere completa)"
		done
	done
	grep -q '^CONFIG_LOCALVERSION=' configs/*.defconfig && bad "CONFIG_LOCALVERSION nel defconfig: la mette tools/build.sh"
	if grep -Eq '^CONFIG_EDK2_TAG_OR_REV="[0-9a-f]{40}"$' configs/w541-mrc.defconfig; then
		ok "EDK2 pinnato a un commit"
	else
		bad "CONFIG_EDK2_TAG_OR_REV: un commit intero, non un ramo"
	fi
	# i file che i defconfig cercano in w541/ sono quelli che build.sh copia
	# (TREE_FILES in w541/, VBOOT_KEYS in w541/vboot/) o vi scrive (la chiave
	# privata degli slot)
	tree_files="$(sed -n '/^TREE_FILES=(/,/)/p' tools/build.sh | tr ' \t()' '\n\n\n\n' | sed -n 's|^.*/||p'
		sed -n '/^VBOOT_KEYS=(/,/)/p' tools/build.sh | tr ' \t()' '\n\n\n\n' | sed -n 's|^keys/vboot/|vboot/|p'
		sed -n 's|^VBOOT_PRIVKEY_TREE=w541/||p' tools/build.sh)"
	for f in $(sed -n 's/^CONFIG_[A-Z0-9_]*="w541\/\([^"]*\)"$/\1/p' configs/*.defconfig | sort -u); do
		grep -qxF "${f}" <<< "${tree_files}" || bad "w541/${f} nei defconfig ma non in TREE_FILES di tools/build.sh"
	done
	ok "file in w541/: $(sed -n 's/^CONFIG_[A-Z0-9_]*="w541\/\([^"]*\)"$/\1/p' configs/*.defconfig | sort -u | tr '\n' ' ')"
	# la mappa del flash: le regioni scritte a ogni avvio nel chip da 8 MiB
	# (0x500000-0x7FFFFF, in SI_BIOS sotto 0x300000), lo slot B e la regione
	# RO nel chip da 4 MiB, la RO in fondo a SI_BIOS (il vettore di reset)
	grep -qx 'CONFIG_FMDFILE="w541/w541.fmd"' configs/w541-mrc.defconfig || bad "w541-mrc: senza CONFIG_FMDFILE=\"w541/w541.fmd\""
	if [ -f configs/w541.fmd ]; then
		fmd() { sed -n "s/^[[:space:]]*${1}@\(0x[0-9a-f]*\) \(0x[0-9a-f]*\).*/\1 \2/p" configs/w541.fmd; }
		for r in RW_MRC_CACHE SMMSTORE RO_VPD RW_ELOG RW_NVRAM RECOVERY_MRC_CACHE; do
			read -r off size <<< "$(fmd "${r}")"
			[ -n "${off}" ] && [ $(( off + size )) -le $(( 0x300000 )) ] || bad "w541.fmd: ${r} non nel chip da 8 MiB"
		done
		read -r off size <<< "$(fmd RW_NVRAM)"
		[ -n "${off}" ] && [ $(( off % 0x10000 )) = 0 ] && [ $(( size % 0x10000 )) = 0 ] \
			|| bad "w541.fmd: RW_NVRAM non in blocchi da 64 KiB tutti suoi"
		read -r off size <<< "$(fmd RW_SECTION_B)"
		[ -n "${off}" ] && [ $(( off )) -ge $(( 0x300000 )) ] || bad "w541.fmd: RW_SECTION_B non nel chip da 4 MiB"
		read -r off size <<< "$(fmd WP_RO)"
		[ -n "${off}" ] && [ $(( off + size )) = $(( 0x700000 )) ] && [ $(( off % 0x1000 )) = 0 ] \
			|| bad "w541.fmd: WP_RO non in fondo a SI_BIOS o non allineata a 4 KiB"
		ok "w541.fmd: regioni scrivibili nel chip da 8 MiB, slot B e regione RO nel chip da 4 MiB"
	else
		bad "configs/w541.fmd non c'e'"
	fi
fi

step "chiavi"
if [ -f keys/capsule-signing.pem ]; then
	if ! command -v openssl >/dev/null 2>&1; then
		echo "  (openssl non installato: certificato non controllato)"
	elif openssl x509 -in keys/capsule-signing.pem -noout 2>/dev/null; then
		ok "keys/capsule-signing.pem: $(openssl x509 -in keys/capsule-signing.pem -noout -fingerprint -sha256 | sed 's/^.*=//' | cut -c1-23)..."
	else
		bad "keys/capsule-signing.pem non e' un certificato"
	fi
else
	bad "keys/capsule-signing.pem non c'e'"
fi
# solo certificati e chiavi pubbliche in keys/, e nessuna chiave privata in
# nessun file: niente PEM privati, niente .vbprivk
for f in $(files | grep '^keys/'); do
	case "${f}" in
		keys/README.md|keys/*.pem|keys/vboot/*.vbpubk|keys/vboot/firmware.keyblock) ;;
		*) bad "${f}: in keys/ solo certificati .pem, chiavi pubbliche di vboot e il README" ;;
	esac
done
if files | grep -q '\.vbprivk$'; then bad "chiavi private di vboot nel repository: $(files | grep '\.vbprivk$')"; fi
# il keyblock porta la chiave dati di keys/vboot/firmware_data_key.vbpubk
if [ -f keys/vboot/firmware.keyblock ] && [ -f keys/vboot/firmware_data_key.vbpubk ] && python3 - <<'EOF'
import struct, sys
kb = open("keys/vboot/firmware.keyblock", "rb").read()
pub = open("keys/vboot/firmware_data_key.vbpubk", "rb").read()
# vb2_keyblock: "CHROMEOS", versioni, dimensione, firma, hash, flag, poi la
# chiave dati (vb2_packed_key) a 0x50 con i suoi dati a key_offset
off, _, size = struct.unpack_from("<III", kb, 0x50)
koff, _, ksize = struct.unpack_from("<III", pub, 0)
sys.exit(0 if kb[:8] == b"CHROMEOS" and kb[0x50 + off:0x50 + off + size] == pub[koff:koff + ksize] else 1)
EOF
then
	ok "keys/vboot: il keyblock porta firmware_data_key.vbpubk"
else
	bad "keys/vboot: firmware.keyblock non porta firmware_data_key.vbpubk (o mancano)"
fi
k="$(files | while IFS= read -r f; do
	[ -f "${f}" ] && grep -lE -- '-----BEGIN ([A-Z]+ )?PRIVATE KEY-----' "${f}" 2>/dev/null
done)"
if [ -n "${k}" ]; then bad "chiavi private nel repository: ${k}"; else ok "nessuna chiave privata nei file del repository"; fi

step "pin di coreboot"
commit="$(sed -n 's/^COREBOOT_COMMIT="\([^"]*\)".*/\1/p' tools/build.sh)"
desc="$(sed -n 's/^COREBOOT_DESCRIBE="\([^"]*\)".*/\1/p' tools/build.sh)"
if [[ "${commit}" =~ ^[0-9a-f]{40}$ ]] && [[ "${desc}" =~ -g([0-9a-f]+)$ ]] && [ "${commit:0:${#BASH_REMATCH[1]}}" = "${BASH_REMATCH[1]}" ]; then
	ok "coreboot ${desc}"
else
	bad "COREBOOT_COMMIT '${commit}' e COREBOOT_DESCRIBE '${desc}' non coerenti"
fi
ro_cb="$(awk '$1 == "coreboot" { print $2 }' configs/ro-interface.txt 2>/dev/null)"
if [ "${ro_cb}" = "${desc}" ]; then
	ok "la parte RO sui portatili e' della stessa coreboot (configs/ro-interface.txt)"
else
	bad "il pin e' ${desc}, la parte RO sui portatili ${ro_cb:-?}: gli slot devono ancora leggere quello che lascia (configs/ro-interface.txt, docs/vboot.md#limits)"
fi

echo
if [ "${fail}" = 0 ]; then
	echo "tutti i controlli passano"
else
	echo "qualche controllo NON passa (righe NO qui sopra)" >&2
fi
exit "${fail}"
