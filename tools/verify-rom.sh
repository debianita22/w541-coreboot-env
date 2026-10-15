#!/bin/bash
# verify-rom.sh - controlla una ROM di tools/build.sh prima che diventi una
# release. Esce con 1 se anche un solo controllo non passa (righe NO).
#
#   tools/verify-rom.sh --variant mrc|nri [opzioni] ROM [CHIP8M CHIP4M]
#
#   ROM            l'immagine completa del flash, 12 MiB
#   CHIP8M CHIP4M  (facoltative) le immagini dei due chip: devono essere i
#                  primi 8 MiB e gli ultimi 4 MiB della ROM
#
# Opzioni:
#   --reference R     l'immagine con i primi 5 MiB attesi (default:
#                     legacy/coreboot-4.22/coreboot.rom, costruita dagli
#                     stessi blob: IFD, GbE e ME non devono cambiare)
#   --cbfstool P      cbfstool (default: work/coreboot/build-<variante>/cbfstool)
#   --ifittool P      ifittool per il FIT (default: quello della stessa build)
#   --localversion S  il CONFIG_LOCALVERSION atteso (default: solo il suffisso
#                     della variante)
#   --release         anche: compilata con il crossgcc (non ANY_TOOLCHAIN) e
#                     senza patch opzionali (+NOME nella versione)
#
# Controlli:
#   1. 12 MiB; i primi 5 MiB (IFD, GbE, ME) identici a quelli di --reference:
#      IFD a 0x0 (firma 0x0FF0A55A a 0x10), GbE a 0x1000 (uguale a
#      blobs/gbe.bin), ME da 0x3000
#   2. FMAP come configs/w541.fmd: SI_BIOS 0x500000-0xBFFFFF; le regioni
#      scritte a ogni avvio (RW_MRC_CACHE, SMMSTORE, RO_VPD, RW_ELOG,
#      RW_NVRAM, RECOVERY_MRC_CACHE) nel chip da 8 MiB e vuote (niente
#      variabili UEFI, seriale o training della RAM di un'altra macchina);
#      RW_NVRAM in blocchi da 64 KiB tutti suoi; RW_SECTION_B e WP_RO nel chip
#      da 4 MiB, WP_RO in cima e allineata a 4 KiB (Protected Range Register)
#   3. CBFS: nella regione RO (COREBOOT) bootblock, microcode, FIT e, nella
#      mrc, mrc.bin uguale a blobs/mrc.bin e a 0xFFFA0000, dove lo chiama la
#      romstage, e niente stage dopo verstage ne' payload; in ognuno dei due
#      slot RW (FW_MAIN_A, FW_MAIN_B) gli stage, il payload, il DSDT con la
#      chiave Optimus di blobs/opvk.inc, pci10de,11fc.rom uguale al VBIOS
#      NVIDIA di blobs/, e gli stessi file nei due slot
#   4b. vboot: la chiave root nel GBB; con --release quella di
#      keys/vboot/root_key.vbpubk, non le chiavi di prova
#   4. microcode per la CPU del W541 (CPUID 306C3) e voci microcode nel FIT
#   5. vettore di reset: un jmp a 0xFFFFFFF0
#   6. il .config dentro la ROM: board, RAM init della variante, IFD, ME
#      (me_cleaner) e GbE, regioni del descrittore sbloccate e niente SMM_BWP
#      fisso (BIOS Lock e Firmware protection dal menu), vboot con due slot,
#      la RO minima, secdata finta e il TPM avviato da ramstage, protezione
#      di WP_RO dal controller con l'opzione, aggiornamento con capsula (ESRT
#      con il GUID del defconfig della variante e la versione X.Y.Z di
#      CONFIG_LOCALVERSION, capsule su disco, il certificato di keys/)
#   7. gli slot vanno con la parte RO gia' sui portatili, che una capsula non
#      riscrive (configs/ro-interface.txt): coreboot dello stesso commit (o
#      di un pin "rw-ok", i cui slot leggono ancora quella parte RO) e,
#      se la build di --cbfstool e' quella della ROM, bootblock e romstage
#      con le stesse regioni della cache-as-RAM
set -uo pipefail
O="$(cd "$(dirname "$0")/.." && pwd)"

die() { printf '\033[31m[x] %s\033[0m\n' "$*" >&2; exit 2; }

VARIANT=""
REFERENCE="${O}/legacy/coreboot-4.22/coreboot.rom"
CBFSTOOL=""
IFITTOOL=""
LOCALVERSION=""
RELEASE=no
FILES=()
while [ $# -gt 0 ]; do
	case "$1" in
		--variant)      VARIANT="${2:-}"; shift 2 ;;
		--reference)    REFERENCE="${2:-}"; shift 2 ;;
		--cbfstool)     CBFSTOOL="${2:-}"; shift 2 ;;
		--ifittool)     IFITTOOL="${2:-}"; shift 2 ;;
		--localversion) LOCALVERSION="${2:-}"; shift 2 ;;
		--release)      RELEASE=yes; shift ;;
		-h|--help)      sed -n '2,/^set -uo/p' "$0" | sed '$d; s/^# \{0,1\}//'; exit 0 ;;
		-*)             die "opzione sconosciuta: $1" ;;
		*)              FILES+=("$1"); shift ;;
	esac
done
case "${VARIANT}" in mrc|nri) ;; *) die "--variant mrc|nri" ;; esac
[ ${#FILES[@]} -eq 1 ] || [ ${#FILES[@]} -eq 3 ] || die "uso: verify-rom.sh --variant mrc|nri [opzioni] ROM [CHIP8M CHIP4M]"
ROM="${FILES[0]}"
CHIP8="${FILES[1]:-}"
CHIP4="${FILES[2]:-}"
[ -f "${ROM}" ] || die "${ROM}: non c'e'"
[ -f "${REFERENCE}" ] || die "--reference ${REFERENCE}: non c'e'"
[ -n "${CBFSTOOL}" ] || CBFSTOOL="${O}/work/coreboot/build-${VARIANT}/cbfstool"
[ -x "${CBFSTOOL}" ] || die "cbfstool non trovato (${CBFSTOOL}): --cbfstool"
if [ -z "${IFITTOOL}" ] && [ -x "$(dirname "${CBFSTOOL}")/util/cbfstool/ifittool" ]; then
	IFITTOOL="$(dirname "${CBFSTOOL}")/util/cbfstool/ifittool"
fi

fail=0
ok()  { printf '  ok  %s\n' "$*"; }
bad() { printf '  NO  %s\n' "$*"; fail=1; }
TMP="$(mktemp -d)"
trap 'rm -rf "${TMP}"' EXIT

ROM_SIZE=$((12 * 1024 * 1024))
LOW_SIZE=$((8 * 1024 * 1024))
CHIP_SIZE=$((4 * 1024 * 1024))
# IFD + GbE + ME: la parte che non cambia mai
INTEL_SIZE=$((0x500000))
# mrc.bin-position della romstage Haswell (haswell_mrc/Makefile.mk), come
# offset nel file: 0xFFFA0000 - (4 GiB - 12 MiB)
MRC_ADDR=0xfffa0000
MRC_OFF=$(( MRC_ADDR - (0x100000000 - ROM_SIZE) ))

# $1 dall'offset $2 per $3 byte e' tutto 0xFF?
all_ff() {
	[ "$(tail -c +"$(( $2 + 1 ))" "$1" | head -c "$3" | tr -d '\377' | wc -c)" = 0 ]
}
# $1 a partire dall'offset $2 contiene il file $3?
at_offset() {
	cmp -s -n "$(stat -c%s "$3")" <(tail -c +"$(( $2 + 1 ))" "$1") "$3"
}
hex() { printf '0x%x' "$1"; }

# La versione dell'ESRT di un CONFIG_LOCALVERSION ouroBIOS-vX.Y.Z[-rcN]-<variante>[+...]
# (come fw_version di tools/build.sh: X.Y.Z.0, le rc la release prima con
# R = N), 0x00000000 per le altre
fw_version_of() {
	local x y z r v
	if [[ "$1" =~ ^ouroBIOS-v([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})(-rc([0-9]{1,3}))?-${VARIANT}(\+.*)?$ ]]; then
		x=$((10#${BASH_REMATCH[1]})); y=$((10#${BASH_REMATCH[2]})); z=$((10#${BASH_REMATCH[3]}))
		r=$((10#${BASH_REMATCH[5]:-0}))
		if [ "${x}" -le 255 ] && [ "${y}" -le 255 ] && [ "${z}" -le 255 ] && [ "${r}" -le 255 ]; then
			v=$(( (x << 24) | (y << 16) | (z << 8) ))
			if [ "${r}" -eq 0 ]; then
				printf '0x%08x' "${v}"
				return
			elif [ "${v}" -ge 256 ]; then
				printf '0x%08x' $(( v - 256 + r ))
				return
			fi
		fi
	fi
	printf '0x%08x' 0
}

echo "${ROM} (${VARIANT})"

# --- 1. dimensione, IFD, GbE e ME ------------------------------------------
size="$(stat -c%s "${ROM}")"
if [ "${size}" = "${ROM_SIZE}" ]; then ok "12 MiB"; else bad "dimensione ${size}, attesa ${ROM_SIZE}"; fi
if cmp -s -n "${INTEL_SIZE}" "${ROM}" "${REFERENCE}"; then
	ok "0x000000-0x4FFFFF (IFD, GbE, ME) identici a ${REFERENCE#"${O}/"}"
else
	bad "0x000000-0x4FFFFF diversi da ${REFERENCE#"${O}/"}: IFD, GbE o ME cambiati ($(cmp -l -n "${INTEL_SIZE}" "${ROM}" "${REFERENCE}" 2>/dev/null | wc -l) byte)"
fi
sig="$(od -An -tx1 -j 16 -N4 "${ROM}" | tr -d ' \n')"
if [ "${sig}" = 5aa5f00f ]; then ok "descrittore Intel (0x0FF0A55A a 0x10)"; else bad "nessun descrittore Intel a 0x10 (${sig})"; fi
if [ -f "${O}/blobs/gbe.bin" ]; then
	if at_offset "${ROM}" $((0x1000)) "${O}/blobs/gbe.bin"; then ok "GbE a 0x1000 uguale a blobs/gbe.bin"; else bad "GbE a 0x1000 diversa da blobs/gbe.bin"; fi
fi

# --- 2. FMAP ---------------------------------------------------------------
if "${CBFSTOOL}" "${ROM}" layout -w > "${TMP}/layout.txt" 2>&1; then
	sed -n "s/^'\([A-Z0-9_]*\)' (.*size \([0-9]*\), offset \([0-9]*\))$/\1 \2 \3/p" \
		"${TMP}/layout.txt" > "${TMP}/regions"
else
	bad "cbfstool layout: $(head -1 "${TMP}/layout.txt")"
	: > "${TMP}/regions"
fi
region() { awk -v r="$1" '$1 == r { print $2, $3; exit }' "${TMP}/regions"; }
read -r bsize boff <<< "$(region SI_BIOS)"
if [ "${boff:-}" = "${INTEL_SIZE}" ] && [ "${bsize:-}" = $(( ROM_SIZE - INTEL_SIZE )) ]; then
	ok "FMAP: SI_BIOS 0x500000-0xBFFFFF (la regione BIOS del descrittore)"
else
	bad "FMAP: SI_BIOS ${boff:-?}+${bsize:-?}, atteso 0x500000+0x700000"
fi
# le regioni scritte a ogni avvio nel chip da 8 MiB, vuote
for r in RW_MRC_CACHE SMMSTORE RO_VPD RW_ELOG RW_NVRAM RECOVERY_MRC_CACHE; do
	read -r rsize roff <<< "$(region "${r}")"
	if [ -z "${rsize:-}" ]; then bad "FMAP: manca ${r}"; continue; fi
	if [ "${roff}" -lt "${INTEL_SIZE}" ] || [ $(( roff + rsize )) -gt "${LOW_SIZE}" ]; then
		bad "FMAP: ${r} $(hex "${roff}") non nel chip da 8 MiB (0x500000-0x7FFFFF)"
		continue
	fi
	if all_ff "${ROM}" "${roff}" "${rsize}"; then
		ok "FMAP: ${r} $(hex "${roff}") nel chip da 8 MiB, vuota"
	else
		bad "FMAP: ${r} non vuota (dati di una macchina nella ROM)"
	fi
done
# RW_NVRAM: il payload lo cancella a blocchi da 64 KiB quando e' pieno
read -r rsize roff <<< "$(region RW_NVRAM)"
if [ -n "${rsize:-}" ] && [ $(( roff % 0x10000 )) = 0 ] && [ $(( rsize % 0x10000 )) = 0 ]; then
	ok "FMAP: RW_NVRAM in blocchi da 64 KiB tutti suoi"
else
	bad "FMAP: RW_NVRAM ${roff:-?}+${rsize:-?} non in blocchi da 64 KiB"
fi
# slot B, la regione RO e la FMAP nel chip da 4 MiB, la RO in cima; slot A
for r in RW_SECTION_B WP_RO FMAP COREBOOT GBB; do
	read -r rsize roff <<< "$(region "${r}")"
	if [ -z "${rsize:-}" ]; then bad "FMAP: manca ${r}"; continue; fi
	if [ "${roff}" -lt "${LOW_SIZE}" ] || [ $(( roff + rsize )) -gt "${ROM_SIZE}" ]; then
		bad "FMAP: ${r} $(hex "${roff}") non nel chip da 4 MiB (0x800000-0xBFFFFF)"
	else
		ok "FMAP: ${r} $(hex "${roff}") nel chip da 4 MiB ($(( rsize / 1024 )) KiB)"
	fi
done
read -r rsize roff <<< "$(region WP_RO)"
if [ -n "${rsize:-}" ] && [ $(( roff + rsize )) = "${ROM_SIZE}" ] && [ $(( roff % 0x1000 )) = 0 ]; then
	ok "FMAP: WP_RO in cima al flash, allineata a 4 KiB"
else
	bad "FMAP: WP_RO ${roff:-?}+${rsize:-?} non in cima al flash o non allineata a 4 KiB"
fi
read -r asize aoff <<< "$(region RW_SECTION_A)"
read -r bsize boff2 <<< "$(region RW_SECTION_B)"
if [ -n "${asize:-}" ] && [ "${asize}" = "${bsize:-}" ]; then
	ok "FMAP: RW_SECTION_A $(hex "${aoff}") e RW_SECTION_B $(hex "${boff2}"), $(( asize / 1024 )) KiB ciascuno"
else
	bad "FMAP: RW_SECTION_A ${asize:-?} e RW_SECTION_B ${bsize:-?} di dimensioni diverse o mancanti"
fi

# --- 3. CBFS ---------------------------------------------------------------
for r in COREBOOT FW_MAIN_A FW_MAIN_B; do
	"${CBFSTOOL}" "${ROM}" print -r "${r}" > "${TMP}/cbfs-${r}.txt" 2>&1 || bad "cbfstool print -r ${r}: $(head -1 "${TMP}/cbfs-${r}.txt")"
	awk 'NR > 2 && $1 != "(empty)" { print $1 }' "${TMP}/cbfs-${r}.txt" > "${TMP}/names-${r}"
done
# has FILE [REGIONE], extract FILE OUT [REGIONE]: la regione RO di default
has() { grep -qxF -- "$1" "${TMP}/names-${2:-COREBOOT}"; }
extract() { "${CBFSTOOL}" "${ROM}" extract -r "${3:-COREBOOT}" -n "$1" -f "$2" > /dev/null 2>&1; }
missing=()
for f in bootblock cpu_microcode_blob.bin intel_fit config revision build_info; do
	has "${f}" || missing+=("${f}")
done
if [ ${#missing[@]} -eq 0 ]; then ok "CBFS RO: bootblock, microcode, FIT, config"; else bad "CBFS RO: mancano ${missing[*]}"; fi
extra=()
for f in fallback/romstage fallback/postcar fallback/ramstage fallback/payload fallback/dsdt.aml; do
	if has "${f}"; then extra+=("${f}"); fi
done
if [ ${#extra[@]} -eq 0 ]; then ok "CBFS RO: niente stage dopo verstage ne' payload (RO minima)"; else bad "CBFS RO: ci sono ${extra[*]}"; fi
for r in FW_MAIN_A FW_MAIN_B; do
	missing=()
	for f in fallback/romstage fallback/postcar fallback/ramstage fallback/dsdt.aml \
		fallback/payload cpu_microcode_blob.bin vbt.bin config pci10de,11fc.rom; do
		has "${f}" "${r}" || missing+=("${f}")
	done
	if [ ${#missing[@]} -eq 0 ]; then ok "CBFS ${r}: stage, payload, DSDT, VBT, microcode"; else bad "CBFS ${r}: mancano ${missing[*]}"; fi
done
if cmp -s "${TMP}/names-FW_MAIN_A" "${TMP}/names-FW_MAIN_B"; then
	ok "CBFS: gli stessi file nei due slot"
else
	bad "CBFS: i due slot hanno file diversi"
fi
if [ "${VARIANT}" = mrc ]; then
	if ! has mrc.bin; then
		bad "CBFS: manca mrc.bin (variante mrc)"
	else
		extract mrc.bin "${TMP}/mrc.bin"
		if cmp -s "${TMP}/mrc.bin" "${O}/blobs/mrc.bin"; then ok "mrc.bin uguale a blobs/mrc.bin"; else bad "mrc.bin diverso da blobs/mrc.bin"; fi
		if at_offset "${ROM}" "${MRC_OFF}" "${O}/blobs/mrc.bin"; then
			ok "mrc.bin a ${MRC_ADDR}"
		else
			bad "mrc.bin non a ${MRC_ADDR} (offset $(hex "${MRC_OFF}") nel file)"
		fi
	fi
else
	if has mrc.bin; then bad "CBFS: mrc.bin nella variante nri"; else ok "niente mrc.bin (RAM init nativa)"; fi
fi
if has pci10de,11fc.rom FW_MAIN_A && extract pci10de,11fc.rom "${TMP}/vbios.rom" FW_MAIN_A \
	&& cmp -s "${TMP}/vbios.rom" "${O}/blobs/vbios_10de_11fc_1.rom"; then
	ok "pci10de,11fc.rom uguale a blobs/vbios_10de_11fc_1.rom"
else
	bad "pci10de,11fc.rom assente o diverso da blobs/vbios_10de_11fc_1.rom"
fi
# la chiave Optimus di blobs/opvk.inc (byte 0x.. separati da virgola) nel DSDT
if has fallback/dsdt.aml FW_MAIN_A && extract fallback/dsdt.aml "${TMP}/dsdt.aml" FW_MAIN_A \
	&& [ -f "${O}/blobs/opvk.inc" ]; then
	n="$(python3 -I - "${O}/blobs/opvk.inc" "${TMP}/dsdt.aml" <<'PY'
import re, sys
key = bytes(int(t, 16) for t in re.findall(r"0x[0-9a-fA-F]{2}", open(sys.argv[1]).read()))
dsdt = open(sys.argv[2], "rb").read()
print(len(key) if key and key in dsdt else 0)
PY
)"
	if [ "${n:-0}" -gt 0 ]; then
		ok "DSDT: chiave Optimus di blobs/opvk.inc (${n} byte)"
	else
		bad "DSDT: senza la chiave Optimus di blobs/opvk.inc"
	fi
else
	bad "DSDT o blobs/opvk.inc non trovati per il controllo della chiave Optimus"
fi

# --- 4. microcode e FIT -------------------------------------------------------
if extract cpu_microcode_blob.bin "${TMP}/ucode.bin"; then
	# header Intel: versione 1, firma CPU a +12, dimensione totale a +32
	sigs="$(python3 -I -c '
import struct, sys
d = open(sys.argv[1], "rb").read()
o, out = 0, []
while o + 48 <= len(d):
    hv, rev, _, sig = struct.unpack_from("<4I", d, o)
    total = struct.unpack_from("<I", d, o + 32)[0] or 2048
    if hv != 1:
        break
    out.append("%x:%x" % (sig, rev))
    o += total
print(" ".join(out))' "${TMP}/ucode.bin")"
	case " ${sigs} " in
		*" 306c3:"*) ok "microcode per CPUID 306C3 (${sigs})" ;;
		*) bad "nessun microcode per CPUID 306C3 (${sigs:-blob vuoto})" ;;
	esac
else
	bad "cpu_microcode_blob.bin non estraibile"
fi
if [ -n "${IFITTOOL}" ]; then
	n="$("${IFITTOOL}" -f "${ROM}" -D -r COREBOOT 2>/dev/null | grep -c 'Microcode' || true)"
	if [ "${n}" -ge 1 ]; then ok "FIT: ${n} voci microcode"; else bad "FIT senza voci microcode"; fi
else
	echo "  --  FIT non controllato (ifittool non trovato)"
fi

# --- 4b. vboot: la chiave root nel GBB --------------------------------------------
read -r gsize goff <<< "$(region GBB)"
if [ -n "${gsize:-}" ]; then
	key="$(python3 -I - "${ROM}" "${goff}" "${gsize}" "${O}/keys/vboot/root_key.vbpubk" <<'PY'
import struct, sys
rom = open(sys.argv[1], "rb").read()
off, size = int(sys.argv[2]), int(sys.argv[3])
gbb = rom[off:off + size]
mine = open(sys.argv[4], "rb").read()
if gbb[:4] != b"$GBB":
    print("nogbb")
    sys.exit()
# header 1.2: firma, versioni, dimensione, flag, HWID, chiave root
ko, ks = struct.unpack_from("<II", gbb, 24)
root = gbb[ko:ko + ks]
print("mine" if root[:len(mine)] == mine else "other")
PY
)"
	case "${key}" in
		mine) ok "GBB: chiave root di keys/vboot/root_key.vbpubk" ;;
		other)
			if [ "${RELEASE}" = yes ]; then
				bad "GBB: la chiave root non e' keys/vboot/root_key.vbpubk (chiavi di prova?)"
			else
				echo "  --  GBB: chiave root diversa da keys/vboot (build di prova con le chiavi di vboot)"
			fi ;;
		*) bad "GBB: non c'e' un GBB valido" ;;
	esac
else
	bad "FMAP: manca GBB"
fi

# --- 5. vettore di reset --------------------------------------------------------
rv="$(od -An -tx1 -j $(( size - 16 )) -N1 "${ROM}" | tr -d ' \n')"
case "${rv}" in
	e9|eb) ok "vettore di reset: jmp (${rv}) a 0xFFFFFFF0" ;;
	*) bad "vettore di reset: ${rv:-?} a 0xFFFFFFF0, atteso un jmp" ;;
esac

# --- 6. il .config nella ROM ----------------------------------------------------
if extract config "${TMP}/config"; then
	want() { if grep -qxF -- "$1" "${TMP}/config"; then ok "config: $1"; else bad "config: manca $1"; fi; }
	never() { if grep -qxF -- "$1" "${TMP}/config"; then bad "config: $1 (${2})"; else ok "config: no ${1%%=*}"; fi; }
	want CONFIG_BOARD_LENOVO_THINKPAD_W541=y
	want 'CONFIG_FMDFILE="w541/w541.fmd"'
	if [ "${VARIANT}" = mrc ]; then
		want CONFIG_HAVE_MRC=y
		never CONFIG_USE_NATIVE_RAMINIT=y "RAM init nativa nella variante mrc"
	else
		want CONFIG_USE_NATIVE_RAMINIT=y
		never CONFIG_HAVE_MRC=y "mrc.bin nella variante nri"
	fi
	want CONFIG_VGA_BIOS_DGPU=y
	want CONFIG_MAINBOARD_USE_LIBGFXINIT=y
	want CONFIG_PAYLOAD_EDK2=y
	want CONFIG_HAVE_IFD_BIN=y
	want CONFIG_HAVE_ME_BIN=y
	want CONFIG_USE_ME_CLEANER=y
	want CONFIG_HAVE_GBE_BIN=y
	want CONFIG_UNLOCK_FLASH_REGIONS=y
	never CONFIG_BOOTMEDIA_SMM_BWP=y "flash scrivibile solo da SMM senza scelta: flashrom -p internal non aggiornerebbe piu'"
	want CONFIG_BOOTMEDIA_SMM_BWP_RUNTIME_OPTION=y
	# vboot (docs/vboot.md)
	want CONFIG_VBOOT=y
	want CONFIG_VBOOT_SLOTS_RW_AB=y
	want CONFIG_VBOOT_STARTS_IN_BOOTBLOCK=y
	want CONFIG_LENOVO_HASWELL_VBOOT_MINIMAL_RO=y
	want CONFIG_VBOOT_VBNV_FLASH=y
	want CONFIG_VBOOT_MOCK_SECDATA=y
	want CONFIG_VBOOT_MOCK_SECDATA_NO_TPM=y
	want CONFIG_TPM_INIT_RAMSTAGE=y
	want CONFIG_BOOTMEDIA_LOCK_CONTROLLER=y
	want CONFIG_BOOTMEDIA_LOCK_WPRO_VBOOT_RO=y
	want CONFIG_BOOTMEDIA_LOCK_RUNTIME_OPTION=y
	if [ "${RELEASE}" = yes ]; then
		want 'CONFIG_VBOOT_ROOT_KEY="w541/vboot/root_key.vbpubk"'
		want 'CONFIG_VBOOT_KEYBLOCK="w541/vboot/firmware.keyblock"'
	fi
	# aggiornamento con capsula (docs/update.md)
	want CONFIG_DRIVERS_EFI_FW_INFO=y
	guid="$(grep '^CONFIG_DRIVERS_EFI_MAIN_FW_GUID=' "${O}/configs/w541-${VARIANT}.defconfig" || true)"
	if [ -n "${guid}" ]; then want "${guid}"; else bad "config: w541-${VARIANT}.defconfig senza CONFIG_DRIVERS_EFI_MAIN_FW_GUID"; fi
	want CONFIG_DRIVERS_EFI_UPDATE_CAPSULES=y
	want CONFIG_DRIVERS_EFI_CAPSULE_ON_DISK_SUPPORT=y
	want 'CONFIG_DRIVERS_EFI_CAPSULE_TRUSTED_PUBLIC_CERT="../../../../../w541/capsule-signing.pem"'
	never CONFIG_DRIVERS_EFI_GENERATE_CAPSULE=y "la capsula la fa tools/build.sh, con la chiave fuori dal .config"
	lv="$(sed -n 's/^CONFIG_LOCALVERSION="\(.*\)"$/\1/p' "${TMP}/config")"
	want "CONFIG_DRIVERS_EFI_MAIN_FW_VERSION=$(fw_version_of "${lv}")"
	if [ -n "${LOCALVERSION}" ]; then
		if [ "${lv}" = "${LOCALVERSION}" ]; then ok "versione: ${lv}"; else bad "versione: '${lv}', attesa '${LOCALVERSION}'"; fi
	else
		case "${lv}" in
			*-"${VARIANT}"|*-"${VARIANT}"+*) ok "versione: ${lv}" ;;
			*) bad "versione: '${lv}' non finisce con -${VARIANT}" ;;
		esac
	fi
	if [ "${RELEASE}" = yes ]; then
		never CONFIG_ANY_TOOLCHAIN=y "compilata con il toolchain del sistema, non con il crossgcc"
		case "${lv}" in *+*) bad "versione con patch opzionali (${lv}): non per una release" ;; *) ok "nessuna patch opzionale" ;; esac
	fi
else
	bad "config non estraibile dal CBFS"
fi
if extract revision "${TMP}/revision"; then
	ev="$(sed -n 's/^#define COREBOOT_EXTRA_VERSION "\(.*\)"$/\1/p' "${TMP}/revision")"
	if [ "${ev}" = "-${lv:-}" ]; then ok "revision: COREBOOT_EXTRA_VERSION ${ev}"; else bad "revision: COREBOOT_EXTRA_VERSION '${ev}', atteso '-${lv:-}'"; fi
fi

# --- 7. interfaccia RO/RW ---------------------------------------------------------
# Una capsula scrive solo gli slot: girano dopo la parte RO gia' sui portatili,
# che lascia il suo stato nella cache-as-RAM. Stessa coreboot e stesse regioni
# di configs/ro-interface.txt.
IFACE="${O}/configs/ro-interface.txt"
if [ ! -f "${IFACE}" ]; then
	bad "manca configs/ro-interface.txt"
else
	ro_cb="$(awk '$1 == "coreboot" { print $2 }' "${IFACE}")"
	for r in FW_MAIN_A FW_MAIN_B; do
		if extract revision "${TMP}/revision-${r}" "${r}"; then
			cv="$(sed -n 's/^#define COREBOOT_VERSION "\(.*\)"$/\1/p' "${TMP}/revision-${r}")"
			if [ "${cv}" = "${ro_cb}" ]; then
				ok "${r}: coreboot ${cv}, come la parte RO sui portatili"
			elif awk -v d="${cv}" '$1 == "rw-ok" && $2 == d { f = 1 } END { exit !f }' "${IFACE}"; then
				ok "${r}: coreboot ${cv}, rw-ok in configs/ro-interface.txt (la parte RO e' ${ro_cb})"
			else
				bad "${r}: coreboot ${cv}, la parte RO sui portatili e' ${ro_cb:-?} (configs/ro-interface.txt)"
			fi
		else
			bad "${r}: revision non estraibile"
		fi
	done
	awk -v v="${VARIANT}" '$1 == v { print $2, $3 }' "${IFACE}" | sort > "${TMP}/car-want"
	BUILDDIR="$(dirname "${CBFSTOOL}")"
	if [ ! -s "${TMP}/car-want" ]; then
		bad "configs/ro-interface.txt: nessuna regione per ${VARIANT}"
	elif ! cmp -s "${TMP}/revision" "${BUILDDIR}/build.h" 2>/dev/null; then
		# i simboli vengono dalla build: devono essere di questa ROM
		echo "  --  cache-as-RAM non controllata: ${BUILDDIR} non e' la build di questa ROM"
	else
		for st in bootblock romstage; do
			elf="${BUILDDIR}/cbfs/fallback/${st}.debug"
			if [ ! -f "${elf}" ]; then
				bad "${st}: manca ${elf}"
				continue
			fi
			nm "${elf}" | awk 'NR == FNR { w[$1] = 1; next } ($3 in w) { print $3, $1 }' "${TMP}/car-want" - \
				| sort > "${TMP}/car-${st}"
			if cmp -s "${TMP}/car-want" "${TMP}/car-${st}"; then
				ok "${st}: le regioni della cache-as-RAM di configs/ro-interface.txt"
			else
				bad "${st}: cache-as-RAM diversa da configs/ro-interface.txt ($(diff "${TMP}/car-want" "${TMP}/car-${st}" | sed -n 's/^> //p' | head -3 | paste -sd ' '))"
			fi
		done
	fi
fi

# --- immagini dei due chip -------------------------------------------------------
if [ -n "${CHIP8}" ]; then
	if [ "$(stat -c%s "${CHIP8}" 2>/dev/null)" = "${LOW_SIZE}" ] \
		&& cmp -s <(head -c "${LOW_SIZE}" "${ROM}") "${CHIP8}"; then
		ok "$(basename "${CHIP8}"): i primi 8 MiB della ROM"
	else
		bad "$(basename "${CHIP8}"): non sono i primi 8 MiB della ROM"
	fi
	if [ "$(stat -c%s "${CHIP4}" 2>/dev/null)" = "${CHIP_SIZE}" ] \
		&& cmp -s <(tail -c "${CHIP_SIZE}" "${ROM}") "${CHIP4}"; then
		ok "$(basename "${CHIP4}"): gli ultimi 4 MiB della ROM"
	else
		bad "$(basename "${CHIP4}"): non sono gli ultimi 4 MiB della ROM"
	fi
	# da solo avvia: la regione RO e lo slot B sono tutti nel chip da 4 MiB
	read -r _ foff <<< "$(region FMAP)"
	if [ -n "${foff:-}" ] && [ "$(tail -c +$(( foff - LOW_SIZE + 1 )) "${CHIP4}" 2>/dev/null | head -c 8)" = "__FMAP__" ]; then
		ok "$(basename "${CHIP4}"): FMAP, regione RO e slot B: avvia da solo (dallo slot B)"
	else
		bad "$(basename "${CHIP4}"): la FMAP non e' dove la mette configs/w541.fmd"
	fi
fi

if [ "${fail}" = 0 ]; then
	echo "  tutti i controlli passano"
else
	echo "  qualche controllo NON passa (righe NO)" >&2
fi
exit "${fail}"
