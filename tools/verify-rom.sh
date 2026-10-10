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
#      blobs/gbe.bin), ME da 0x3000; 0x500000-0x7FFFFF tutto 0xFF (le regioni
#      scrivibili vuote: niente variabili UEFI, seriale o training della RAM
#      di un'altra macchina)
#   2. FMAP come configs/w541.fmd: SI_BIOS 0x500000-0xBFFFFF; RW_MRC_CACHE,
#      SMMSTORE, RO_VPD e RW_ELOG nel chip da 8 MiB (sotto 0x800000); FMAP e COREBOOT
#      nel chip da 4 MiB (da 0x800000), cosi' l'immagine di quel chip e' un
#      coreboot completo
#   3. CBFS: i file di ogni build; mrc.bin solo nella mrc, uguale a
#      blobs/mrc.bin e a 0xFFFA0000, dove lo chiama la romstage;
#      pci10de,11fc.rom uguale al VBIOS NVIDIA di blobs/; la chiave Optimus
#      di blobs/opvk.inc nel DSDT
#   4. microcode per la CPU del W541 (CPUID 306C3) e voci microcode nel FIT
#   5. vettore di reset: un jmp a 0xFFFFFFF0
#   6. il .config dentro la ROM: board, RAM init della variante, IFD, ME
#      (me_cleaner) e GbE, flash non bloccato (regioni sbloccate,
#      BOOTMEDIA_LOCK_NONE e niente SMM_BWP: il prossimo aggiornamento si fa
#      ancora con flashrom -p internal), aggiornamento con capsula (ESRT con
#      il GUID del defconfig della variante e la versione X.Y.Z di
#      CONFIG_LOCALVERSION, capsule su disco, il certificato di keys/)
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

# La versione dell'ESRT di un CONFIG_LOCALVERSION w541-vX.Y.Z[-...]-<variante>[+...]
# (come fw_version di tools/build.sh), 0x00000000 per le altre
fw_version_of() {
	local x y z
	if [[ "$1" =~ ^w541-v([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,5})(-.*)?-${VARIANT}(\+.*)?$ ]]; then
		x=$((10#${BASH_REMATCH[1]})); y=$((10#${BASH_REMATCH[2]})); z=$((10#${BASH_REMATCH[3]}))
		if [ "${x}" -le 255 ] && [ "${y}" -le 255 ] && [ "${z}" -le 65535 ]; then
			printf '0x%08x' $(( (x << 24) | (y << 16) | z ))
			return
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
if all_ff "${ROM}" "${INTEL_SIZE}" $(( LOW_SIZE - INTEL_SIZE )); then
	ok "0x500000-0x7FFFFF tutto 0xFF (regioni scrivibili vuote)"
else
	bad "0x500000-0x7FFFFF non vuoto (dati di una macchina nella ROM, o CBFS nel chip da 8 MiB)"
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
for r in RW_MRC_CACHE SMMSTORE RO_VPD RW_ELOG; do
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
# coreboot tutto nel chip da 4 MiB
for r in FMAP COREBOOT; do
	read -r rsize roff <<< "$(region "${r}")"
	if [ -z "${rsize:-}" ]; then bad "FMAP: manca ${r}"; continue; fi
	if [ "${roff}" -lt "${LOW_SIZE}" ] || [ $(( roff + rsize )) -gt "${ROM_SIZE}" ]; then
		bad "FMAP: ${r} $(hex "${roff}") non nel chip da 4 MiB (0x800000-0xBFFFFF)"
	else
		ok "FMAP: ${r} $(hex "${roff}") nel chip da 4 MiB ($(( rsize / 1024 )) KiB)"
	fi
done

# --- 3. CBFS ---------------------------------------------------------------
"${CBFSTOOL}" "${ROM}" print -r COREBOOT > "${TMP}/cbfs.txt" 2>&1 || bad "cbfstool print: $(head -1 "${TMP}/cbfs.txt")"
awk 'NR > 2 { print $1 }' "${TMP}/cbfs.txt" > "${TMP}/names"
has() { grep -qxF -- "$1" "${TMP}/names"; }
extract() { "${CBFSTOOL}" "${ROM}" extract -r COREBOOT -n "$1" -f "$2" > /dev/null 2>&1; }
missing=()
for f in bootblock fallback/romstage fallback/postcar fallback/ramstage fallback/dsdt.aml \
	fallback/payload cpu_microcode_blob.bin intel_fit vbt.bin config revision build_info \
	pci10de,11fc.rom; do
	has "${f}" || missing+=("${f}")
done
if [ ${#missing[@]} -eq 0 ]; then ok "CBFS: stage, payload, DSDT, VBT, microcode, FIT, config"; else bad "CBFS: mancano ${missing[*]}"; fi
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
if has pci10de,11fc.rom && extract pci10de,11fc.rom "${TMP}/vbios.rom" \
	&& cmp -s "${TMP}/vbios.rom" "${O}/blobs/vbios_10de_11fc_1.rom"; then
	ok "pci10de,11fc.rom uguale a blobs/vbios_10de_11fc_1.rom"
else
	bad "pci10de,11fc.rom assente o diverso da blobs/vbios_10de_11fc_1.rom"
fi
# la chiave Optimus di blobs/opvk.inc (byte 0x.. separati da virgola) nel DSDT
if has fallback/dsdt.aml && extract fallback/dsdt.aml "${TMP}/dsdt.aml" \
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
	want CONFIG_BOOTMEDIA_LOCK_NONE=y
	never CONFIG_BOOTMEDIA_SMM_BWP=y "flash scrivibile solo da SMM: flashrom -p internal non aggiornerebbe piu'"
	# aggiornamento con capsula (docs/update.md)
	want CONFIG_DRIVERS_EFI_FW_INFO=y
	want "$(grep '^CONFIG_DRIVERS_EFI_MAIN_FW_GUID=' "${O}/configs/w541-${VARIANT}.defconfig")"
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
	# da solo e' un coreboot completo: la FMAP e' in testa al chip
	if [ "$(head -c 8 "${CHIP4}" 2>/dev/null)" = "__FMAP__" ]; then
		ok "$(basename "${CHIP4}"): FMAP in testa, coreboot completo nel chip da 4 MiB"
	else
		bad "$(basename "${CHIP4}"): nessuna FMAP in testa al chip da 4 MiB"
	fi
fi

if [ "${fail}" = 0 ]; then
	echo "  tutti i controlli passano"
else
	echo "  qualche controllo NON passa (righe NO)" >&2
fi
exit "${fail}"
