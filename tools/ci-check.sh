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
#      GenerateCapsule
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
#   9. le chiavi: in keys/ solo certificati, nessuna chiave privata nei file
#      del repository
#   8. il pin di coreboot in tools/build.sh: commit intero e describe coerente
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
update.check_capsule(cap, "nri")
assert update.version_str(0x01020003) == "v1.2.3"
EOF
	then
		ok "tools/update.py: capsula letta"
	else
		bad "tools/update.py: la capsula di prova non si legge come atteso"
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
if grep -q '^+++ b/.*opvk\.inc' patches/optional/*.patch patches/*.patch patches/edk2/*.patch patches/lvglpkg/*.patch 2>/dev/null; then
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
	tree_files="$(sed -n '/^TREE_FILES=(/,/)/p' tools/build.sh | tr ' \t()' '\n\n\n\n' | sed -n 's|^.*/||p')"
	for f in $(sed -n 's/^CONFIG_[A-Z0-9_]*="w541\/\([^"]*\)"$/\1/p' configs/*.defconfig | sort -u); do
		grep -qxF "${f}" <<< "${tree_files}" || bad "w541/${f} nei defconfig ma non in TREE_FILES di tools/build.sh"
	done
	ok "file in w541/: $(sed -n 's/^CONFIG_[A-Z0-9_]*="w541\/\([^"]*\)"$/\1/p' configs/*.defconfig | sort -u | tr '\n' ' ')"
	# la mappa del flash: le regioni scritte a ogni avvio nel chip da 8 MiB
	# (0x500000-0x7FFFFF), FMAP e CBFS nel chip da 4 MiB
	grep -qx 'CONFIG_FMDFILE="w541/w541.fmd"' configs/w541-mrc.defconfig || bad "w541-mrc: senza CONFIG_FMDFILE=\"w541/w541.fmd\""
	if [ -f configs/w541.fmd ]; then
		for r in RW_MRC_CACHE SMMSTORE RO_VPD RW_ELOG; do
			off="$(sed -n "s/^[[:space:]]*${r}@\(0x[0-9a-f]*\) .*/\1/p" configs/w541.fmd)"
			[ -n "${off}" ] && [ $(( off )) -lt $(( 0x300000 )) ] || bad "w541.fmd: ${r} non nel chip da 8 MiB (offset in SI_BIOS sotto 0x300000)"
		done
		for r in FMAP "COREBOOT(CBFS)"; do
			off="$(sed -n "s/^[[:space:]]*${r}@\(0x[0-9a-f]*\) .*/\1/p" configs/w541.fmd)"
			[ -n "${off}" ] && [ $(( off )) -ge $(( 0x300000 )) ] || bad "w541.fmd: ${r} non nel chip da 4 MiB (offset in SI_BIOS da 0x300000)"
		done
		ok "w541.fmd: regioni scrivibili nel chip da 8 MiB, FMAP e CBFS nel chip da 4 MiB"
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
# solo certificati in keys/, e nessuna chiave privata in nessun file
for f in $(files | grep '^keys/'); do
	case "${f}" in keys/README.md|keys/*.pem) ;; *) bad "${f}: in keys/ solo certificati .pem e il README" ;; esac
done
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

echo
if [ "${fail}" = 0 ]; then
	echo "tutti i controlli passano"
else
	echo "qualche controllo NON passa (righe NO qui sopra)" >&2
fi
exit "${fail}"
