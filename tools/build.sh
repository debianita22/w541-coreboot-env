#!/bin/bash
# build.sh - le ROM coreboot del ThinkPad W541: "mrc" (RAM init con mrc.bin)
# e "nri" (RAM init nativa di coreboot). Lo stesso script a mano e nella CI
# (.github/workflows/build.yml e check.yml).
#
#   tools/build.sh [opzioni] [comando ...]
#
# Comandi (default: prepare toolchain roms):
#   prepare        coreboot al commit pinnato (solo quel commit), i submodule
#                  che servono dai mirror GitHub, le patch di patches/series,
#                  i blob controllati con SHA256SUMS nella cartella w541/
#                  dell'albero (e' li' che li cercano i defconfig) e la
#                  chiave Optimus dove la include il DSDT; EDK2 al
#                  commit dei defconfig con le patch di patches/edk2/series
#                  e di patches/lvglpkg/series (il submodule LvglPkg)
#   toolchain      il crossgcc di coreboot: i386 con Ada (libgfxinit), iasl e
#                  nasm. La prima volta 30-60 minuti, poi resta in
#                  util/crossgcc/xgcc dell'albero (e nella cache della CI)
#   config         solo il .config di ogni variante, con il controllo che
#                  nessuna riga del defconfig sia andata persa
#   roms           .config, build, tools/verify-rom.sh, file in dist/; con
#                  la chiave in CAPSULE_SIGNING_KEY anche la capsula firmata
#                  e il .cab di ogni variante, e i metadati firmati del
#                  remote fwupd (docs/update.md). Gli slot RW di vboot si
#                  firmano con la chiave in VBOOT_SIGNING_KEY (docs/vboot.md);
#                  senza, con le chiavi di prova di vboot: build di prova
#   smoke          dopo roms, la prova di fumo del payload: per ogni variante
#                  una ROM coreboot per QEMU q35 con lo stesso UEFIPAYLOAD.fd
#                  (EDK2 non si ricompila), avviata in QEMU da
#                  tools/qemu-smoke.py: il payload parte, il setup si apre e
#                  risponde ai tasti. Schermate e seriale in WORK/smoke-<variante>
#   update-test    la prova dell'aggiornamento con le capsule (vboot A/B) in
#                  QEMU q35, tools/qemu-update.py: un payload EDK2 DEBUG con
#                  le patch del progetto, che si fida di una chiave creata per
#                  la prova; quattro ROM per q35 con vboot a due slot (A, B,
#                  C e D, che ha anche lo schermo per il setup), costruite in
#                  un worktree dell'albero con patches/test/q35-update-test.patch;
#                  le loro capsule, fatte come quelle delle release. Le ROM del
#                  W541 non c'entrano. Seriale in WORK/update-test/out
#   keys           controlla le chiavi di firma dell'ambiente (CAPSULE_SIGNING_KEY,
#                  VBOOT_SIGNING_KEY) senza build: con --release devono esserci
#   toolchain-key  stampa la chiave della cache del crossgcc (build.yml)
#
# Opzioni:
#   --variant V     mrc o nri, ripetibile (default: tutte e due)
#   --version V     nella versione di coreboot e nei nomi dei file (default:
#                   dev-<commit corto di questo repository>)
#   --toolchain T   crossgcc (default, l'unico per le release) oppure host:
#                   gcc e GNAT del sistema, della stessa versione, piu' iasl e
#                   nasm. Solo per prove: la ROM dice ANY_TOOLCHAIN nel .config
#   --with NOME     applica anche patches/optional/NOME.patch; il nome finisce
#                   nella versione e nei file (+NOME). Mai in una release
#   --release       build da pubblicare: solo crossgcc, niente --with, e
#                   verify-rom.sh --release (build.yml); con una versione
#                   vX.Y.Z anche la capsula, che vuole la chiave
#   --work DIR      cartella di lavoro (default: work/ in questo repository)
#   --dist DIR      i file finali (default: dist/ in questo repository)
#   --jobs N        default: nproc
#
# Ogni ROM e' un'immagine completa del flash da 12 MiB (chip da 8 MiB + chip
# da 4 MiB), con la mappa di configs/w541.fmd: IFD, GbE e ME di blobs/ nei
# primi 5 MiB (identici a legacy/coreboot-4.22/coreboot.rom), poi le regioni
# scritte a ogni avvio (training della RAM, variabili UEFI, VPD, registro,
# dati di vboot), vuote, nel chip da 8 MiB; i due slot RW di vboot (A a
# cavallo dei chip, B nel chip da 4 MiB) e la regione RO in cima al chip da
# 4 MiB. In dist/ anche le immagini dei due chip, per un programmatore
# esterno. Dall'interno si scrive con flashrom (docs/flashing.md), o, dalla
# v1.3.0, con la capsula, che scrive uno slot (docs/update.md).
#
# La capsula (w541-coreboot-<versione>-<variante>.cap): la ROM con il
# manifest RMAP delle regioni che il firmware riscrive (CAPSULE_REGIONS: i
# due slot, di cui il payload scrive solo quello che non gira),
# firmata con GenerateCapsule di EDK2. La chiave privata arriva solo
# dall'ambiente, CAPSULE_SIGNING_KEY (PEM; nella CI il secret omonimo), e
# deve essere quella del certificato keys/capsule-signing.pem, di cui le ROM
# si fidano. Accanto, il pacchetto per fwupd (.cab, tools/fwupd-cab.py), con
# la capsula firmata anche per fwupd da jcat-tool, e i metadati del remote
# fwupd (ourobios.xml.gz e la sua firma .jcat, configs/fwupd/ourobios.conf):
# i .cab delle due varianti con gli indirizzi delle release. La versione
# nell'ESRT e nella capsula e' X.Y.Z.R, un byte ciascuno (il "quad" di
# fwupd): X.Y.Z.0 per --version vX.Y.Z, la release prima con R = N per
# vX.Y.Z-rcN (fw_version); con le altre versioni (dev, ci-...) e' 0 e la
# capsula non si fa.
set -euo pipefail
O="$(cd "$(dirname "$0")/.." && pwd)"

# coreboot: il commit pinnato e il suo git describe (l'albero di lavoro e' un
# clone di quel commit solo, senza tag: la versione nelle ROM la diamo noi)
COREBOOT_REPO="https://github.com/coreboot/coreboot.git"
COREBOOT_COMMIT="acdbfd47295d26898789e80ffcdc45d94b76682a"
COREBOOT_DESCRIBE="26.09-280-gacdbfd47295d"

# I submodule che servono a questa board, "nome percorso URL": gli URL di
# .gitmodules sono review.coreboot.org, qui i mirror su GitHub. Gli altri non
# si scaricano e make gira con UPDATED_SUBMODULES=1 (niente fetch da solo).
SUBMODULES=(
	"vboot 3rdparty/vboot https://github.com/coreboot/vboot.git"
	"libhwbase 3rdparty/libhwbase https://github.com/coreboot/libhwbase.git"
	"libgfxinit 3rdparty/libgfxinit https://github.com/coreboot/libgfxinit.git"
	"intel-microcode 3rdparty/intel-microcode https://github.com/coreboot/intel-microcode.git"
)

# EDK2: il repository di MrChromebox (quello di CONFIG_EDK2_REPO_MRCHROMEBOX)
# nella cartella dove lo cerca il Makefile del payload, e il commit dei
# defconfig. Le patch vanno nell'albero come modifiche, non come commit: il
# Makefile riporta al commit pinnato un albero pulito, ma uno con modifiche
# lo lascia com'e' ("Working directory not clean; will not overwrite").
EDK2_REPO="https://github.com/mrchromebox/edk2"
EDK2_SUBDIR="payloads/external/edk2/workspace/mrchromebox"

# I file di blobs/, assets/ e configs/ che vanno nell'albero (w541/<nome>): i
# defconfig li cercano li'. mrc.bin lo usa solo la variante mrc.
TREE_FILES=(blobs/ifd.bin blobs/gbe.bin blobs/me.bin blobs/mrc.bin
	blobs/vbios_10de_11fc_1.rom assets/bootsplash.bmp configs/w541.fmd
	keys/capsule-signing.pem)
# Le regioni che una capsula riscrive: il training della RAM, che il codice
# nuovo rifa' al primo avvio, e lo slot RW di vboot che non gira (la capsula
# li porta tutti e due, il payload sceglie: edk2 0015). Le altre restano:
# la regione RO, variabili UEFI (SMMSTORE), VPD, registro degli eventi,
# dati di vboot (li aggiorna il payload), IFD, GbE, ME.
CAPSULE_REGIONS=(RW_MRC_CACHE RW_SECTION_A RW_SECTION_B)
CAPSULE_CERT=keys/capsule-signing.pem
# i metadati del remote fwupd, nella release mrc (configs/fwupd/ourobios.conf)
FWUPD_METADATA=ourobios.xml.gz
# La prova dell'aggiornamento (update-test): il GUID dell'FMP delle ROM di
# prova, che non e' quello di una variante; le ROM A, B, C e D con le loro
# versioni e la minima; quella con lo schermo, per il setup del payload (la
# grafica rallenta gli avvii, le altre non ce l'hanno); la patch per q35, che
# va solo nel worktree della prova
UPDATE_TEST_GUID="b195f75a-8f95-4282-950c-ee9dd1099ec9"
UPDATE_TEST_ROMS=("A 0x01000000" "B 0x01010000" "C 0x01020000" "D 0x01030000")
UPDATE_TEST_LSV=0x01000000
UPDATE_TEST_SCREEN=D
UPDATE_TEST_PATCH=patches/test/q35-update-test.patch
# Le chiavi pubbliche di vboot (tools/vboot-keys.sh), in w541/vboot/
# dell'albero. La chiave privata della chiave dati (firmware_data_key.vbprivk)
# arriva solo da VBOOT_SIGNING_KEY (base64) e sta nell'albero solo durante le
# build: senza, i defconfig puntano alle chiavi di prova di vboot.
VBOOT_KEYS=(keys/vboot/root_key.vbpubk keys/vboot/recovery_key.vbpubk
	keys/vboot/kernel_subkey.vbpubk keys/vboot/firmware.keyblock
	keys/vboot/firmware_data_key.vbpubk)
VBOOT_PRIVKEY_TREE=w541/vboot/firmware_data_key.vbprivk
# La chiave Optimus (patch 0036, CONFIG_LENOVO_HASWELL_NVIDIA_OPVK): il DSDT
# la include da questo file, che la patch fa ignorare a git
OPVK_FILE=blobs/opvk.inc
OPVK_TREE=src/mainboard/lenovo/haswell/acpi/opvk.inc

# git am: il committer e le date fisse rendono uguale ogni volta il commit in
# cima all'albero (genbuild_h.sh ne prende data e hash)
export GIT_COMMITTER_NAME="ourobios"
export GIT_COMMITTER_EMAIL="ourobios@users.noreply.github.com"

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die()  { note error "$*"; printf '\033[31m[x] %s\033[0m\n' "$*" >&2; exit 1; }
# un'annotazione del job, se gira nella CI (si legge anche dall'API)
note() {
	[ -n "${GITHUB_ACTIONS:-}" ] || return 0
	local msg="$2"
	msg="${msg//'%'/'%25'}"; msg="${msg//$'\r'/}"; msg="${msg//$'\n'/'%0A'}"
	echo "::$1 title=build.sh::${msg}"
}
# Dopo un errore: le ultime righe dei log $2... in un'annotazione ($1 il
# titolo). Il log intero di un job non sempre si scarica, le annotazioni si'.
failure_report() {
	local title="$1" f msg=""
	shift
	for f in "$@"; do
		[ -s "${f}" ] || continue
		msg+="== ${f#"${WORK}/"}"$'\n'"$(tail -n 40 "${f}" | sed 's/\x1b\[[0-9;]*m//g' | cut -c1-300)"$'\n'
	done
	[ -n "${msg}" ] && note error "${title}"$'\n'"${msg}"
	return 0
}

VARIANTS=()
VERSION=""
TOOLCHAIN=crossgcc
WITH=()
RELEASE=no
WORK="${O}/work"
DIST="${O}/dist"
JOBS="$(nproc 2>/dev/null || echo 2)"
CMDS=()
while [ $# -gt 0 ]; do
	case "$1" in
		--variant)   VARIANTS+=("${2:?--variant mrc|nri}"); shift 2 ;;
		--version)   VERSION="${2:?--version V}"; shift 2 ;;
		--toolchain) TOOLCHAIN="${2:?--toolchain crossgcc|host}"; shift 2 ;;
		--with)      WITH+=("${2:?--with NOME}"); shift 2 ;;
		--release)   RELEASE=yes; shift ;;
		--work)      WORK="$(realpath -m "${2:?--work DIR}")"; shift 2 ;;
		--dist)      DIST="$(realpath -m "${2:?--dist DIR}")"; shift 2 ;;
		--jobs)      JOBS="${2:?--jobs N}"; shift 2 ;;
		-h|--help)   sed -n '2,/^set -euo/p' "$0" | sed '$d; s/^# \{0,1\}//'; exit 0 ;;
		prepare|toolchain|config|roms|smoke|update-test|keys|toolchain-key) CMDS+=("$1"); shift ;;
		*) die "argomento sconosciuto: $1 (tools/build.sh --help)" ;;
	esac
done
[ ${#VARIANTS[@]} -gt 0 ] || VARIANTS=(mrc nri)
[ ${#CMDS[@]} -gt 0 ] || CMDS=(prepare toolchain roms)
for v in "${VARIANTS[@]}"; do
	case "${v}" in mrc|nri) ;; *) die "variante '${v}': mrc o nri" ;; esac
done
case "${TOOLCHAIN}" in crossgcc|host) ;; *) die "toolchain '${TOOLCHAIN}': crossgcc o host" ;; esac
case "${JOBS}" in ''|*[!0-9]*|0) die "--jobs ${JOBS}: un numero" ;; esac
if [ -z "${VERSION}" ]; then
	VERSION="dev-$(git -C "${O}" rev-parse --short=7 HEAD 2>/dev/null || echo local)"
fi
case "${VERSION}" in
	''|*[!A-Za-z0-9._-]*) die "versione '${VERSION}': solo lettere, cifre e . _ -" ;;
	*-mrc|*-nri) die "versione '${VERSION}': senza -mrc/-nri, la variante la aggiunge lo script" ;;
esac
for w in "${WITH[@]}"; do
	case "${w}" in ''|*[!A-Za-z0-9._-]*) die "--with '${w}': il nome di un file di patches/optional/ senza .patch" ;; esac
	[ -f "${O}/patches/optional/${w}.patch" ] || die "--with ${w}: patches/optional/${w}.patch non c'e'"
done
if [ "${RELEASE}" = yes ]; then
	[ "${TOOLCHAIN}" = crossgcc ] || die "--release: solo con il crossgcc di coreboot"
	[ ${#WITH[@]} -eq 0 ] || die "--release: niente patch opzionali (--with)"
fi

TREE="${WORK}/coreboot"
XGCC="${TREE}/util/crossgcc/xgcc"
STAMP="${WORK}/prepared"
EDK2_DIR="${TREE}/${EDK2_SUBDIR}"

# Le patch di patches/series (o di patches/$1/series), una per riga (vuote e
# # saltate)
series() { sed -e 's/#.*//' -e 's/[[:space:]]*$//' "${O}/patches/${1:+$1/}series" | sed '/^$/d'; }

# Il commit di EDK2, lo stesso nei due defconfig (ci-check.sh lo controlla)
edk2_rev() { sed -n 's/^CONFIG_EDK2_TAG_OR_REV="\([0-9a-f]\{40\}\)"$/\1/p' "${O}/configs/w541-mrc.defconfig"; }

# L'impronta di cio' che prepare mette nell'albero: commit, patch, blob. Le
# altre fasi la confrontano con quella scritta da prepare.
state() {
	(
		cd "${O}" || exit 1
		echo "${COREBOOT_COMMIT}"
		printf '%s\n' "${SUBMODULES[@]}"
		local p
		while read -r p; do sha256sum "patches/${p}"; done < <(series)
		for p in "${WITH[@]}"; do sha256sum "patches/optional/${p}.patch"; done
		edk2_rev
		while read -r p; do sha256sum "patches/edk2/${p}"; done < <(series edk2)
		while read -r p; do sha256sum "patches/lvglpkg/${p}"; done < <(series lvglpkg)
		for p in "${TREE_FILES[@]}" "${VBOOT_KEYS[@]}"; do sha256sum "${p}"; done
		sha256sum "${OPVK_FILE}"
	) | sha256sum | cut -d' ' -f1
}
need_prepared() {
	[ -f "${STAMP}" ] && [ "$(cat "${STAMP}")" = "$(state)" ] \
		|| die "l'albero in ${TREE} non e' pronto o e' di altre patch: prima tools/build.sh prepare (con le stesse --with)"
}

# make nell'albero, con la cartella di build della variante $1
mk() {
	mk_in "${TREE}" "$@"
}
# make nell'albero $1 (o nel worktree di update-test), cartella di build $2
mk_in() {
	local t="$1" v="$2"; shift 2
	make -C "${t}" obj="build-${v}" DOTCONFIG="build-${v}/.config" \
		UPDATED_SUBMODULES=1 KERNELVERSION="${COREBOOT_DESCRIBE}" "$@"
}
# Ogni riga del defconfig della cartella di build $1 nel suo .config: un
# simbolo con le dipendenze non soddisfatte sparisce senza errori
defconfig_kept() {
	local line miss=0
	while IFS= read -r line; do
		grep -qxF -- "${line}" "$1/.config" || { echo "  NO  ${line}"; miss=1; }
	done < "$1/defconfig"
	return "${miss}"
}

# Il nome della variante $1 con le patch opzionali: mrc, mrc+test-peg-afe
flavour() {
	local f="$1" w
	for w in "${WITH[@]}"; do f+="+${w}"; done
	printf '%s' "${f}"
}
# CONFIG_LOCALVERSION della variante $1, e il nome dei suoi file in dist/
localversion() { printf 'ouroBIOS-%s-%s' "${VERSION}" "$(flavour "$1")"; }
romname()      { printf 'w541-coreboot-%s-%s' "${VERSION}" "$(flavour "$1")"; }

# La versione per l'ESRT e la capsula, X.Y.Z.R un byte ciascuno (il "quad"
# di fwupd): vX.Y.Z -> X.Y.Z.0; vX.Y.Z-rcN -> la release prima con R = N
# (v1.3.7-rc2 -> 1.3.6.2), cosi' le rc vengono dopo la release precedente e
# prima di quella a cui portano. 0x00000000 per le altre versioni (dev,
# ci-...): niente capsula. fw_quad: la stessa come X.Y.Z.R (fwupd)
fw_quad() {
	local v
	v=$(( $(fw_version) ))
	printf '%d.%d.%d.%d' $(( v >> 24 )) $(( (v >> 16) & 255 )) $(( (v >> 8) & 255 )) $(( v & 255 ))
}
fw_version() {
	local x y z r v
	if [[ "${VERSION}" =~ ^v([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})(-rc([0-9]{1,3}))?$ ]]; then
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

cmd_prepare() {
	say "coreboot ${COREBOOT_DESCRIBE} (${COREBOOT_COMMIT:0:12})"
	rm -f "${STAMP}"
	mkdir -p "${WORK}"
	if [ ! -d "${TREE}/.git" ]; then
		git init -q "${TREE}"
		git -C "${TREE}" remote add origin "${COREBOOT_REPO}"
	fi
	if ! git -C "${TREE}" cat-file -e "${COREBOOT_COMMIT}^{commit}" 2>/dev/null; then
		git -C "${TREE}" fetch -q --depth 1 origin "${COREBOOT_COMMIT}" \
			|| die "coreboot ${COREBOOT_COMMIT}: fetch da ${COREBOOT_REPO} fallito"
	fi
	# Di nuovo al commit pinnato: via le patch di un giro precedente e i file
	# nuovi che avevano creato. Restano le build delle varianti, il crossgcc e
	# il workspace di EDK2 (ignorati, o qui sotto in info/exclude).
	git -C "${TREE}" am --abort >/dev/null 2>&1 || true
	git -C "${TREE}" checkout -q --force --detach "${COREBOOT_COMMIT}"
	printf '%s\n' '/build-*/' '/w541/' > "${TREE}/.git/info/exclude"
	git -C "${TREE}" clean -q -fd

	say "submodule"
	local s name path url paths=()
	for s in "${SUBMODULES[@]}"; do
		read -r name path url <<< "${s}"
		git -C "${TREE}" config "submodule.${name}.url" "${url}"
		paths+=("${path}")
	done
	# --checkout anche per quelli con update = none (intel-microcode)
	git -C "${TREE}" submodule update -q --init --checkout --depth 1 -- "${paths[@]}" \
		|| die "submodule: fetch dai mirror GitHub fallito"
	git -C "${TREE}" submodule status -- "${paths[@]}"

	say "patch"
	local p n=0
	while read -r p; do
		[ -f "${O}/patches/${p}" ] || die "patches/series: ${p} non c'e'"
		if ! git -C "${TREE}" am -q --committer-date-is-author-date "${O}/patches/${p}"; then
			git -C "${TREE}" am --abort >/dev/null 2>&1 || true
			die "${p} non applica su coreboot ${COREBOOT_COMMIT:0:12}"
		fi
		n=$((n + 1))
	done < <(series)
	echo "  ${n} patch di patches/series"
	for p in "${WITH[@]}"; do
		if ! git -C "${TREE}" am -q --committer-date-is-author-date "${O}/patches/optional/${p}.patch"; then
			git -C "${TREE}" am --abort >/dev/null 2>&1 || true
			die "patches/optional/${p}.patch non applica sopra la serie"
		fi
		echo "  + patches/optional/${p}.patch"
	done
	git -C "${TREE}" log --oneline -1

	say "blob"
	(cd "${O}/blobs" && sha256sum --quiet -c SHA256SUMS) || die "blobs/: i file non corrispondono a SHA256SUMS"
	(cd "${O}/assets" && sha256sum --quiet -c SHA256SUMS) || die "assets/: i file non corrispondono a SHA256SUMS"
	rm -rf "${TREE}/w541"
	mkdir -p "${TREE}/w541"
	for p in "${TREE_FILES[@]}"; do
		cp "${O}/${p}" "${TREE}/w541/"
		echo "  w541/$(basename "${p}")"
	done
	mkdir -p "${TREE}/w541/vboot"
	for p in "${VBOOT_KEYS[@]}"; do
		cp "${O}/${p}" "${TREE}/w541/vboot/"
		echo "  w541/vboot/$(basename "${p}")"
	done
	cp "${O}/${OPVK_FILE}" "${TREE}/${OPVK_TREE}"
	echo "  ${OPVK_TREE}"

	prepare_edk2
	state > "${STAMP}"
}

# EDK2 al commit dei defconfig, i submodule ai loro commit, poi le patch
# applicate come modifiche: patches/edk2 sull'albero, patches/lvglpkg sul
# submodule LvglPkg. Il clone e' parziale (--filter=blob:none): la storia
# senza il contenuto dei file, che arriva solo per i commit pinnati.
prepare_edk2() {
	local rev p n
	rev="$(edk2_rev)"
	[ -n "${rev}" ] || die "configs/w541-mrc.defconfig: CONFIG_EDK2_TAG_OR_REV non e' un commit intero"
	say "EDK2 ${rev:0:12}"
	if [ ! -d "${EDK2_DIR}/.git" ]; then
		rm -rf "${EDK2_DIR}"
		mkdir -p "$(dirname "${EDK2_DIR}")"
		git clone -q --filter=blob:none --no-checkout "${EDK2_REPO}" "${EDK2_DIR}" \
			|| die "EDK2: clone da ${EDK2_REPO} fallito"
	fi
	if ! git -C "${EDK2_DIR}" cat-file -e "${rev}^{commit}" 2>/dev/null; then
		git -C "${EDK2_DIR}" fetch -q origin || die "EDK2: fetch da ${EDK2_REPO} fallito"
	fi
	# Di nuovo al commit pinnato: via le patch di un giro precedente, i file
	# nuovi che avevano creato e il Logo.bmp che riscrive la build. Restano i
	# file ignorati (BaseTools compilati).
	git -C "${EDK2_DIR}" checkout -q --force --detach "${rev}" || die "EDK2: ${rev} non c'e' in ${EDK2_REPO}"
	git -C "${EDK2_DIR}" clean -q -fd
	git -C "${EDK2_DIR}" submodule update -q --init --force --recursive --filter=blob:none \
		|| die "EDK2: submodule"
	git -C "${EDK2_DIR}" submodule foreach -q --recursive 'git clean -q -fd'
	[ -d "${EDK2_DIR}/LvglPkg/.git" ] || [ -f "${EDK2_DIR}/LvglPkg/.git" ] || die "EDK2: senza il submodule LvglPkg"

	# --whitespace=nowarn: le righe CRLF dei file di EDK2 per git apply sono
	# spazi in fondo alla riga
	n=0
	while read -r p; do
		[ -f "${O}/patches/edk2/${p}" ] || die "patches/edk2/series: ${p} non c'e'"
		git -C "${EDK2_DIR}" apply --whitespace=nowarn "${O}/patches/edk2/${p}" \
			|| die "patches/edk2/${p} non applica su EDK2 ${rev:0:12}"
		n=$((n + 1))
	done < <(series edk2)
	echo "  ${n} patch di patches/edk2/series"
	n=0
	while read -r p; do
		[ -f "${O}/patches/lvglpkg/${p}" ] || die "patches/lvglpkg/series: ${p} non c'e'"
		git -C "${EDK2_DIR}/LvglPkg" apply --whitespace=nowarn "${O}/patches/lvglpkg/${p}" \
			|| die "patches/lvglpkg/${p} non applica su LvglPkg $(git -C "${EDK2_DIR}/LvglPkg" rev-parse --short=12 HEAD)"
		n=$((n + 1))
	done < <(series lvglpkg)
	echo "  ${n} patch di patches/lvglpkg/series (LvglPkg $(git -C "${EDK2_DIR}/LvglPkg" rev-parse --short=12 HEAD))"
}

# I sorgenti del crossgcc in util/crossgcc/tarballs/ prima di buildgcc, che
# poi li trova li' e li riverifica. Per ognuno l'URL di buildgcc, poi
# ftp.gnu.org al posto di ftpmirror.gnu.org (rimanda a un mirror a caso, a
# volte irraggiungibile dai runner di GitHub), poi il mirror di coreboot; il
# checksum e' quello di util/crossgcc/sum/.
seed_tarballs() {
	local cg="${TREE}/util/crossgcc" vars pkg file base mirror sum url last got
	mkdir -p "${cg}/tarballs"
	# solo le assegnazioni semplici di buildgcc: versioni, archivi, URL
	vars="$(sed -n -e '/^[A-Z_]*_VERSION=/p' -e '/^[A-Z_]*_ARCHIVE=/p' \
		-e '/^[A-Z_]*_BASE_URL=/p' -e '/^COREBOOT_MIRROR_URL=/p' "${cg}/buildgcc")"
	while read -r pkg file base mirror; do
		[ -n "${file}" ] && [ -n "${base}" ] || die "crossgcc: ${pkg} non trovato in buildgcc"
		[ -f "${cg}/sum/${file}.cksum" ] || die "crossgcc: manca sum/${file}.cksum"
		sum="$(cut -d' ' -f1 "${cg}/sum/${file}.cksum")"
		if [ -f "${cg}/tarballs/${file}" ] && [ "$(sha1sum < "${cg}/tarballs/${file}" | cut -d' ' -f1)" = "${sum}" ]; then
			echo "  ${file} gia' scaricato"
			continue
		fi
		got=no
		last=""
		for url in "${base}/${file}" "${base/#https:\/\/ftpmirror.gnu.org\//https://ftp.gnu.org/gnu/}/${file}" "${mirror}/${file}"; do
			[ "${url}" != "${last}" ] || continue
			last="${url}"
			if curl -fsSL --retry 3 --retry-delay 10 --connect-timeout 30 --max-time 1800 \
				-o "${cg}/tarballs/${file}.part" "${url}" \
				&& [ "$(sha1sum < "${cg}/tarballs/${file}.part" | cut -d' ' -f1)" = "${sum}" ]; then
				mv "${cg}/tarballs/${file}.part" "${cg}/tarballs/${file}"
				echo "  ${file} da ${url}"
				got=yes
				break
			fi
			echo "  ${file}: ${url} non va (download o checksum)"
		done
		rm -f "${cg}/tarballs/${file}.part"
		[ "${got}" = yes ] || die "crossgcc: ${file} non si scarica da nessun mirror"
	done < <(
		eval "${vars}"
		for pkg in GMP MPFR MPC BINUTILS GCC NASM IASL; do
			a="${pkg}_ARCHIVE"
			u="${pkg}_BASE_URL"
			echo "${pkg} ${!a:-} ${!u:-} ${COREBOOT_MIRROR_URL:-}"
		done
	)
}

xgcc_ok() {
	local t
	for t in i386-elf-gcc i386-elf-gnatbind iasl nasm; do
		[ -x "${XGCC}/bin/${t}" ] || return 1
	done
}

cmd_toolchain() {
	local t
	if [ "${TOOLCHAIN}" = host ]; then
		say "toolchain del sistema (solo prove)"
		for t in gcc gnatbind iasl nasm; do
			command -v "${t}" >/dev/null || die "${t} non trovato: con --toolchain host servono gcc, GNAT, iasl e nasm"
		done
		gcc --version | head -1
		gnatbind --version 2>/dev/null | head -1 || true
		return 0
	fi
	if xgcc_ok; then
		say "crossgcc gia' pronto"
		"${XGCC}/bin/i386-elf-gcc" --version | head -1
		return 0
	fi
	[ -d "${TREE}/util/crossgcc" ] || die "prima tools/build.sh prepare"
	say "sorgenti del crossgcc"
	seed_tarballs
	say "crossgcc i386 + Ada, iasl, nasm (lungo: 30-60 minuti)"
	# BUILD_LANGUAGES esplicito: senza GNAT buildgcc si ferma, invece di fare
	# un compilatore solo C che poi non compila libgfxinit
	local log="${WORK}/crossgcc.log" logs d
	if ! make -C "${TREE}" crossgcc-i386 CPUS="${JOBS}" BUILD_LANGUAGES=c,ada UPDATED_SUBMODULES=1 2>&1 \
		| tee "${log}"; then
		# il log di make e quello del pacchetto che si e' fermato
		logs=("${log}")
		for d in "${TREE}"/util/crossgcc/build-*; do
			if [ -f "${d}/.failed" ]; then logs+=("${d}/build.log"); fi
		done
		failure_report "crossgcc" "${logs[@]}"
		die "crossgcc: build fallita (${log}, util/crossgcc/build-*/build.log nell'albero)"
	fi
	xgcc_ok || die "crossgcc incompleto in ${XGCC}/bin"
	"${XGCC}/bin/i386-elf-gcc" --version | head -1
}

# La chiave della cache: cosa costruisce buildgcc (script, versioni e checksum
# dei sorgenti, patch) e il sistema che lo costruisce
cmd_toolchain_key() {
	[ -f "${TREE}/util/crossgcc/buildgcc" ] || die "prima tools/build.sh prepare"
	local h os
	h="$(cd "${TREE}/util/crossgcc" \
		&& find buildgcc Makefile sum patches -type f -print0 | LC_ALL=C sort -z \
		| xargs -0 sha256sum | sha256sum | cut -c1-16)"
	# shellcheck disable=SC1091
	os="$(. /etc/os-release && echo "${ID}${VERSION_ID}")"
	echo "crossgcc-i386-ada-${os}-${h}"
}

# .config della variante $1 dal suo defconfig, e il controllo: ogni riga
# CONFIG_ del defconfig deve esserci uguale (un simbolo con le dipendenze non
# soddisfatte sparisce senza errori)
variant_config() {
	local v="$1" obj="build-$1" line sym have miss=0
	: "${VBOOT_KEYS_REAL:=no}"
	rm -rf "${TREE:?}/${obj}"
	mkdir -p "${TREE}/${obj}"
	{
		if [ "${VBOOT_KEYS_REAL}" = yes ]; then
			cat "${O}/configs/w541-${v}.defconfig"
		else
			grep -v -E '^CONFIG_VBOOT_(ROOT_KEY|RECOVERY_KEY|KERNEL_KEY|KEYBLOCK|FIRMWARE_PRIVKEY)=' \
				"${O}/configs/w541-${v}.defconfig"
		fi
		echo "CONFIG_LOCALVERSION=\"$(localversion "${v}")\""
		echo "CONFIG_DRIVERS_EFI_MAIN_FW_VERSION=$(fw_version)"
		if [ "${TOOLCHAIN}" = host ]; then echo "CONFIG_ANY_TOOLCHAIN=y"; fi
		if [ "${VBOOT_KEYS_REAL}" != yes ]; then
			# senza la chiave del progetto: le chiavi di prova di vboot
			local k
			for k in ROOT_KEY:root_key.vbpubk RECOVERY_KEY:recovery_key.vbpubk \
				KERNEL_KEY:kernel_subkey.vbpubk KEYBLOCK:firmware.keyblock \
				FIRMWARE_PRIVKEY:firmware_data_key.vbprivk; do
				echo "CONFIG_VBOOT_${k%%:*}=\"\$(VBOOT_SOURCE)/tests/devkeys/${k#*:}\""
			done
		fi
	} > "${TREE}/${obj}/defconfig"
	mk "${v}" defconfig KBUILD_DEFCONFIG="${obj}/defconfig" > "${TREE}/${obj}/defconfig.log" 2>&1 \
		|| { cat "${TREE}/${obj}/defconfig.log"; die "${v}: make defconfig fallito"; }
	while IFS= read -r line; do
		case "${line}" in CONFIG_*=*) ;; *) continue ;; esac
		grep -qxF -- "${line}" "${TREE}/${obj}/.config" && continue
		sym="${line%%=*}"
		have="$(grep -E "^(# )?${sym}[= ]" "${TREE}/${obj}/.config" || echo "non c'e'")"
		echo "  NO  ${line}  (nel .config: ${have})"
		miss=1
	done < "${TREE}/${obj}/defconfig"
	[ "${miss}" = 0 ] || die "${v}: il .config non ha tutto il defconfig (dipendenze non soddisfatte o simboli rinominati)"
	echo "  ok  ${v}: $(grep -c '^CONFIG_' "${TREE}/${obj}/defconfig") righe del defconfig nel .config"
}

cmd_config() {
	need_prepared
	local v
	for v in "${VARIANTS[@]}"; do
		say ".config ${v}"
		variant_config "${v}"
	done
}

# I file della variante $1 in dist/
collect() {
	local v="$1" b="${TREE}/build-$1" n
	n="$(romname "${v}")"
	cp "${b}/coreboot.rom" "${DIST}/${n}.rom"
	# i due chip per un programmatore esterno: 0x000000-0x7FFFFF (IFD, GbE,
	# ME e le regioni scrivibili, vuote) e 0x800000-0xBFFFFF (FMAP e CBFS:
	# tutto coreboot)
	head -c 8388608 "${b}/coreboot.rom" > "${DIST}/${n}-8mb-chip.rom"
	tail -c 4194304 "${b}/coreboot.rom" > "${DIST}/${n}-4mb-chip.rom"
	cp "${b}/.config" "${DIST}/${n}.config"
	{
		echo "${n}"
		echo "coreboot ${COREBOOT_DESCRIBE} (${COREBOOT_COMMIT}) + $(series | wc -l) patches (patches/series)"
		echo "EDK2 $(edk2_rev) + $(series edk2 | wc -l) patches (patches/edk2/series)," \
			"LvglPkg + $(series lvglpkg | wc -l) patches (patches/lvglpkg/series)"
		local w
		for w in "${WITH[@]}"; do echo "+ patches/optional/${w}.patch"; done
		echo "CONFIG_LOCALVERSION=\"$(localversion "${v}")\""
		echo
		"${b}/cbfstool" "${b}/coreboot.rom" layout -w
		echo
		"${b}/cbfstool" "${b}/coreboot.rom" print -r COREBOOT
		echo
		"${b}/cbfstool" "${b}/coreboot.rom" print -r FW_MAIN_A
	} > "${DIST}/${n}-layout.txt"
}

# Il certificato di cui si fida la ROM appena costruita: quello compilato
# in FmpDxe (il PCD che scrive capsule_keys di payloads/external/edk2,
# nell'AutoGen.c della build di EDK2 della variante $1) deve essere
# keys/capsule-signing.pem, e solo quello
check_trusted_cert() {
	local v="$1" guid build autogen
	guid="$(sed -n 's/^CONFIG_DRIVERS_EFI_MAIN_FW_GUID="\(.*\)"$/\1/p' "${TREE}/build-${v}/.config")"
	build=RELEASE
	if grep -qx 'CONFIG_EDK2_DEBUG=y' "${TREE}/build-${v}/.config"; then build=DEBUG; fi
	autogen="${TREE}/payloads/external/edk2/workspace/Build/UefiPayloadPkgX64/${build}_GCC/X64/FmpDevicePkg/FmpDxe/${guid}/DEBUG/AutoGen.c"
	[ -f "${autogen}" ] || die "${v}: ${autogen#"${TREE}/"} non c'e': FmpDxe non costruito?"
	openssl x509 -in "${O}/${CAPSULE_CERT}" -outform der \
		| python3 -c '
import re, sys
src = open(sys.argv[1]).read()
m = re.search(r"PcdFmpDevicePkcs7CertBufferXdr\[\d*\]\s*=\s*\{([^}]*)\}", src)
pcd = bytes(int(x, 16) for x in re.findall(r"0x[0-9A-Fa-f]+", m.group(1))) if m else b""
der = sys.stdin.buffer.read()
# XDR: lunghezza a 32 bit big endian e certificato, allineati a 4 byte
sys.exit(0 if der and pcd[:4] == len(der).to_bytes(4, "big") and pcd[4:4 + len(der)] == der
         and len(pcd) <= 4 + len(der) + 3 else 1)' "${autogen}" \
		|| die "${v}: FmpDxe non si fida di ${CAPSULE_CERT} (o non solo di quello): ${autogen#"${TREE}/"}"
	echo "  ok  ${v}: FmpDxe si fida solo di ${CAPSULE_CERT}"
}

# La capsula della ROM $1 in $2, come quelle delle release: il manifest RMAP
# delle CAPSULE_REGIONS (le regioni che il payload puo' riscrivere) e la
# firma PKCS#7 di GenerateCapsule di EDK2. $3 GUID, $4 versione, $5 minima,
# $6 la chiave privata con il suo certificato in un file, $7 il certificato.
# Il file intermedio sta accanto alla chiave. La usa anche update-test
generate_capsule() {
	local rom="$1" out="$2" guid="$3" fwv="$4" lsv="$5" signer="$6" cert="$7" r image rc=0 regions=()
	for r in "${CAPSULE_REGIONS[@]}"; do regions+=(-r "${r}"); done
	image="$(dirname "${signer}")/image.bin"
	python3 "${EDK2_DIR}/UefiPayloadPkg/Tools/AppendRmapManifest.py" -o "${image}" \
		"${regions[@]}" "${rom}" > /dev/null || return 1
	PYTHONPATH="${EDK2_DIR}/BaseTools/Source/Python" python3 \
		"${EDK2_DIR}/BaseTools/Source/Python/Capsule/GenerateCapsule.py" -e -o "${out}" \
		--guid "${guid}" --fw-version "${fwv}" --lsv "${lsv}" --capflag PersistAcrossReset \
		--signer-private-cert "${signer}" --other-public-cert "${cert}" --trusted-public-cert "${cert}" \
		"${image}" || rc=1
	rm -f "${image}"
	return "${rc}"
}

# La capsula della variante $1 in dist/, firmata con CAPSULE_SIGNING_KEY e
# ricontrollata con tools/update.py info (GUID, versioni, regioni, immagine
# uguale alla ROM, firma valida per keys/capsule-signing.pem)
make_capsule() {
	local v="$1" n guid lsv fwv tmp f date repo
	n="${DIST}/$(romname "${v}")"
	guid="$(sed -n 's/^CONFIG_DRIVERS_EFI_MAIN_FW_GUID="\(.*\)"$/\1/p' "${TREE}/build-${v}/.config")"
	lsv="$(sed -n 's/^CONFIG_DRIVERS_EFI_MAIN_FW_LSV=\(0x[0-9a-fA-F]*\)$/\1/p' "${TREE}/build-${v}/.config")"
	fwv="$(fw_version)"
	[ -n "${guid}" ] && [ -n "${lsv}" ] || die "${v}: GUID o LSV mancanti nel .config"
	# la data del commit, non quella della build
	date="$(git -C "${O}" log -1 --format=%cs 2>/dev/null || date -u +%F)"
	repo="${GITHUB_REPOSITORY:-debianita22/ourobios}"
	tmp="$(mktemp -d)"
	chmod 700 "${tmp}"
	# la chiave privata solo in questa cartella e solo per GenerateCapsule
	(
		umask 077
		trap 'rm -rf "${tmp}"' EXIT
		printf '%s\n' "${CAPSULE_SIGNING_KEY}" > "${tmp}/key.pem"
		[ "$(openssl pkey -in "${tmp}/key.pem" -pubout 2>/dev/null)" = \
			"$(openssl x509 -in "${O}/${CAPSULE_CERT}" -noout -pubkey)" ] \
			|| die "CAPSULE_SIGNING_KEY non e' la chiave di ${CAPSULE_CERT}"
		cat "${tmp}/key.pem" "${O}/${CAPSULE_CERT}" > "${tmp}/signer.pem"
		generate_capsule "${n}.rom" "${n}.cap" "${guid}" "${fwv}" "${lsv}" "${tmp}/signer.pem" \
			"${O}/${CAPSULE_CERT}" || die "${v}: capsula non fatta (AppendRmapManifest.py o GenerateCapsule)"
		# il pacchetto fwupd: la capsula, il metainfo e la loro firma
		mkdir "${tmp}/cab"
		cp "${n}.cap" "${tmp}/cab/firmware.bin"
		python3 "${O}/tools/fwupd-cab.py" metainfo --variant "${v}" --guid "${guid}" \
			--version "$(fw_quad)" --date "${date}" \
			--url "https://github.com/${repo}/releases/tag/${VERSION}-$(flavour "${v}")" \
			> "${tmp}/cab/firmware.metainfo.xml" || die "${v}: metainfo fallito"
		for f in firmware.bin firmware.metainfo.xml; do
			(cd "${tmp}/cab" && jcat-tool self-sign firmware.jcat "${f}" --kind sha256 > /dev/null \
				&& jcat-tool sign firmware.jcat "${f}" "${O}/${CAPSULE_CERT}" "${tmp}/key.pem" > /dev/null) \
				|| die "${v}: jcat-tool fallito su ${f}"
		done
		python3 "${O}/tools/fwupd-cab.py" pack --date "${date}" -o "${n}.cab" \
			"${tmp}/cab/firmware.bin" "${tmp}/cab/firmware.metainfo.xml" "${tmp}/cab/firmware.jcat" \
			|| die "${v}: fwupd-cab.py pack fallito"
	) || exit 1
	chmod 644 "${n}.cap" "${n}.cab"
	python3 "${O}/tools/update.py" info --cert "${O}/${CAPSULE_CERT}" --rom "${n}.rom" \
		--guid "${guid}" --fw-version "${fwv}" --lsv "${lsv}" "${n}.cap" \
		|| die "${v}: la capsula non passa tools/update.py info"
	python3 "${O}/tools/update.py" info --cert "${O}/${CAPSULE_CERT}" --guid "${guid}" \
		--fw-version "${fwv}" --lsv "${lsv}" --same-as "${n}.cap" "${n}.cab" \
		|| die "${v}: il pacchetto fwupd non passa tools/update.py info"
}

# I metadati del remote fwupd (configs/fwupd/ourobios.conf) in dist/: i
# .cab delle varianti, ciascuno con l'indirizzo che avra' nella sua release,
# e la firma jcat con CAPSULE_SIGNING_KEY. Li pubblica la release mrc (ci.sh
# publish); il remote li prende dall'ultima, releases/latest/download/
make_fwupd_metadata() {
	local v n repo tmp args=() cabs=()
	repo="${GITHUB_REPOSITORY:-debianita22/ourobios}"
	for v in "${VARIANTS[@]}"; do
		n="$(romname "${v}").cab"
		args+=(--cab "${DIST}/${n}" "https://github.com/${repo}/releases/download/${VERSION}-$(flavour "${v}")/${n}")
		cabs+=("${DIST}/${n}")
	done
	rm -f "${DIST}/${FWUPD_METADATA}" "${DIST}/${FWUPD_METADATA}.jcat"
	python3 "${O}/tools/fwupd-cab.py" metadata -o "${DIST}/${FWUPD_METADATA}" "${args[@]}" \
		|| die "metadati fwupd non fatti (tools/fwupd-cab.py metadata)"
	tmp="$(mktemp -d)"
	chmod 700 "${tmp}"
	(
		umask 077
		trap 'rm -rf "${tmp}"' EXIT
		printf '%s\n' "${CAPSULE_SIGNING_KEY}" > "${tmp}/key.pem"
		cd "${DIST}"
		jcat-tool self-sign "${FWUPD_METADATA}.jcat" "${FWUPD_METADATA}" --kind sha256 > /dev/null \
			&& jcat-tool sign "${FWUPD_METADATA}.jcat" "${FWUPD_METADATA}" "${O}/${CAPSULE_CERT}" "${tmp}/key.pem" > /dev/null
	) || die "jcat-tool fallito sui metadati fwupd"
	chmod 644 "${DIST}/${FWUPD_METADATA}" "${DIST}/${FWUPD_METADATA}.jcat"
	python3 "${O}/tools/fwupd-cab.py" check-metadata --cert "${O}/${CAPSULE_CERT}" "${DIST}/${FWUPD_METADATA}" \
		"${cabs[@]}" || die "i metadati fwupd non passano tools/fwupd-cab.py check-metadata"
}

# La chiave privata degli slot (VBOOT_SIGNING_KEY, base64 di
# firmware_data_key.vbprivk) nel file $1, se e' quella di
# keys/vboot/firmware_data_key.vbpubk. Spazi, a capo e virgolette attorno,
# che un copia e incolla puo' aggiungere, non contano. Un errore dice solo
# lunghezze, conteggi e i caratteri estranei, mai la chiave
vboot_key_to() {
	local why
	why="$(umask 077; VBK="${VBOOT_SIGNING_KEY}" python3 - "$1" "${O}/keys/vboot/firmware_data_key.vbpubk" <<'EOF'
import base64, binascii, os, re, struct, subprocess, sys

def fail(msg):
    print(msg)
    sys.exit(1)

s = re.sub(r"[\s\u200b-\u200d\u2060\ufeff]+", "", os.environ["VBK"]).strip("\"'")
try:
    data = base64.b64decode(s, validate=True)
except (binascii.Error, ValueError):
    odd = sorted({c for c in s if not re.fullmatch(r"[A-Za-z0-9+/=]", c)})
    fail("%d caratteri senza spazi (attesi 3144), %d fuori dall'alfabeto base64 (%s)%s"
         % (len(s), sum(c in odd for c in s), " ".join("U+%04X" % ord(c) for c in odd[:8]) or "nessuno",
            ", non multiplo di 4" if len(s) % 4 else ""))
# vbprivk: l'algoritmo in un u64 (7: RSA-4096 con SHA-256), poi la chiave in DER
if len(data) < 16 or int.from_bytes(data[:8], "little") != 7 or data[8] != 0x30:
    fail("%d byte in base64, ma non una chiave privata vboot RSA-4096 (.vbprivk)" % len(data))
out = subprocess.run(["openssl", "rsa", "-inform", "DER", "-noout", "-modulus"],
                     input=data[8:], capture_output=True).stdout.decode()
m = re.search(r"Modulus=([0-9A-F]+)", out)
if not m:
    fail("%d byte in base64: la chiave RSA non si legge (troncata?)" % len(data))
# vbpubk: l'offset della chiave, poi parole a 32 bit: n, n0inv, il modulo little endian
pub = open(sys.argv[2], "rb").read()
off = struct.unpack_from("<I", pub, 0)[0]
words = struct.unpack_from("<I", pub, off)[0]
if int(m.group(1), 16) != int.from_bytes(pub[off + 8:off + 8 + 4 * words], "little"):
    fail("una chiave RSA-4096, ma non quella di keys/vboot/firmware_data_key.vbpubk")
open(sys.argv[1], "wb").write(data)
EOF
)" || die "VBOOT_SIGNING_KEY: ${why:-non valida} (base64 -w0 firmware_data_key.vbprivk, una riga; docs/vboot.md)"
}

# La chiave degli slot nell'albero per la build, e solo per la build
install_vboot_key() {
	vboot_key_to "${TREE}/${VBOOT_PRIVKEY_TREE}"
	trap 'rm -f "${TREE}/${VBOOT_PRIVKEY_TREE}"' EXIT
	echo "  ok  VBOOT_SIGNING_KEY: la chiave di keys/vboot/firmware_data_key.vbpubk"
}

# Le chiavi di firma dell'ambiente, senza albero ne' build: un errore si vede
# nel primo minuto della CI, non dopo la toolchain (build.yml). Con --release
# devono esserci (CAPSULE_SIGNING_KEY con una versione vX.Y.Z)
cmd_keys() {
	local tmp
	say "chiavi di firma"
	tmp="$(mktemp -d)"
	chmod 700 "${tmp}"
	trap 'rm -rf "${tmp}"' EXIT
	if [ -n "${CAPSULE_SIGNING_KEY:-}" ]; then
		(umask 077; printf '%s\n' "${CAPSULE_SIGNING_KEY}" > "${tmp}/key.pem")
		[ "$(openssl pkey -in "${tmp}/key.pem" -pubout 2>/dev/null)" = \
			"$(openssl x509 -in "${O}/${CAPSULE_CERT}" -noout -pubkey)" ] \
			|| die "CAPSULE_SIGNING_KEY non e' la chiave di ${CAPSULE_CERT}"
		echo "  ok  CAPSULE_SIGNING_KEY: la chiave di ${CAPSULE_CERT}"
	elif [ "${RELEASE}" = yes ] && [ "$(fw_version)" != 0x00000000 ]; then
		die "--release ${VERSION}: senza CAPSULE_SIGNING_KEY niente capsula (docs/update.md)"
	else
		echo "  --  CAPSULE_SIGNING_KEY assente: niente capsula"
	fi
	if [ -n "${VBOOT_SIGNING_KEY:-}" ]; then
		vboot_key_to "${tmp}/firmware_data_key.vbprivk"
		echo "  ok  VBOOT_SIGNING_KEY: la chiave di keys/vboot/firmware_data_key.vbpubk"
	elif [ "${RELEASE}" = yes ]; then
		die "--release: senza VBOOT_SIGNING_KEY gli slot non si firmano con la chiave del progetto (docs/vboot.md)"
	else
		echo "  --  VBOOT_SIGNING_KEY assente: slot firmati con le chiavi di prova di vboot"
	fi
	rm -rf "${tmp}"
	trap - EXIT
}

# Gli slot RW della ROM della variante $1 si verificano con la chiave root del
# suo GBB, e in una build con la chiave del progetto quella root e' la nostra
check_vboot() {
	local v="$1" b="${TREE}/build-$1" f out tmp
	f="${b}/util/futility/futility"
	[ -x "${f}" ] || die "${v}: futility non c'e' (${f#"${TREE}/"})"
	out="$("${f}" verify "${b}/coreboot.rom" 2>&1)" || { echo "${out}" | tail -n 20; die "${v}: futility verify fallito"; }
	[ "$(grep -c 'Body verification succeeded' <<< "${out}")" = 2 ] \
		|| { echo "${out}" | tail -n 20; die "${v}: non si verificano tutti e due gli slot"; }
	if [ "${VBOOT_KEYS_REAL}" = yes ]; then
		# gbb -g esporta tutta l'area della chiave root (con il riempimento
		# a 0xFF dopo la chiave): si confrontano i byte della chiave
		tmp="$(mktemp)"
		"${f}" gbb -g --rootkey="${tmp}" "${b}/coreboot.rom" > /dev/null
		cmp -s -n "$(stat -c %s "${O}/keys/vboot/root_key.vbpubk")" "${tmp}" "${O}/keys/vboot/root_key.vbpubk" \
			|| { rm -f "${tmp}"; die "${v}: la chiave root nel GBB non e' keys/vboot/root_key.vbpubk"; }
		rm -f "${tmp}"
		echo "  ok  ${v}: slot A e B firmati, chiave root keys/vboot/root_key.vbpubk"
	else
		echo "  ok  ${v}: slot A e B firmati con le chiavi di prova di vboot (build di prova)"
	fi
}

cmd_roms() {
	need_prepared
	local v capsule=no
	if [ "$(fw_version)" != 0x00000000 ]; then
		if [ -n "${CAPSULE_SIGNING_KEY:-}" ]; then
			capsule=yes
			command -v jcat-tool > /dev/null || die "serve jcat-tool (pacchetto jcat) per il pacchetto fwupd"
		elif [ "${RELEASE}" = yes ]; then
			die "--release ${VERSION}: senza CAPSULE_SIGNING_KEY niente capsula (docs/update.md)"
		fi
	fi
	VBOOT_KEYS_REAL=no
	if [ -n "${VBOOT_SIGNING_KEY:-}" ]; then
		install_vboot_key
		VBOOT_KEYS_REAL=yes
	elif [ "${RELEASE}" = yes ]; then
		die "--release: senza VBOOT_SIGNING_KEY gli slot non si firmano con la chiave del progetto (docs/vboot.md)"
	else
		note warning "VBOOT_SIGNING_KEY assente: slot firmati con le chiavi di prova di vboot"
		echo "  !!  VBOOT_SIGNING_KEY assente: slot firmati con le chiavi di prova di vboot (build di prova)"
	fi
	if [ "${TOOLCHAIN}" = crossgcc ]; then
		xgcc_ok || die "crossgcc mancante in ${XGCC}: prima tools/build.sh toolchain"
		# nasm e iasl del crossgcc anche per EDK2, che li cerca nel PATH
		export PATH="${XGCC}/bin:${PATH}"
	fi
	mkdir -p "${DIST}"
	for v in "${VARIANTS[@]}"; do
		say ".config ${v}"
		variant_config "${v}"
		say "build ${v} ($(localversion "${v}"))"
		if ! mk "${v}" -j"${JOBS}" 2>&1 | tee "${TREE}/build-${v}/make.log"; then
			# con -j l'errore non e' per forza in fondo: anche le righe con "rror"
			grep -n -i -B2 -A6 'error' "${TREE}/build-${v}/make.log" | tail -n 60 > "${TREE}/build-${v}/errors.log" || true
			failure_report "build ${v}" "${TREE}/build-${v}/errors.log" "${TREE}/build-${v}/make.log"
			die "${v}: build fallita (build-${v}/make.log nell'albero)"
		fi
		# i file di questa variante di un giro precedente, anche di altre versioni
		rm -f "${DIST}"/w541-coreboot-*-"${v}"[.+-]*
		collect "${v}"
		say "verifica ${v}"
		local n args=(--variant "${v}" --localversion "$(localversion "${v}")"
			--cbfstool "${TREE}/build-${v}/cbfstool")
		n="${DIST}/$(romname "${v}")"
		if [ -x "${TREE}/build-${v}/util/cbfstool/ifittool" ]; then
			args+=(--ifittool "${TREE}/build-${v}/util/cbfstool/ifittool")
		fi
		if [ "${RELEASE}" = yes ]; then args+=(--release); fi
		"${O}/tools/verify-rom.sh" "${args[@]}" "${n}.rom" "${n}-8mb-chip.rom" "${n}-4mb-chip.rom" \
			|| die "${v}: la ROM non passa tools/verify-rom.sh"
		check_trusted_cert "${v}"
		check_vboot "${v}"
		if [ "${capsule}" = yes ]; then
			say "capsula ${v} ($(fw_version))"
			make_capsule "${v}"
		fi
	done
	if [ "${capsule}" = yes ]; then
		say "metadati fwupd"
		make_fwupd_metadata
	else
		rm -f "${DIST}/${FWUPD_METADATA}" "${DIST}/${FWUPD_METADATA}.jcat"
	fi
	(cd "${DIST}" && find . -maxdepth 1 -type f \( -name 'w541-coreboot-*' -o -name "${FWUPD_METADATA}*" \) \
		-printf '%f\n' | LC_ALL=C sort | xargs -r sha256sum > SHA256SUMS)
	say "dist"
	ls -l "${DIST}"
}

# La prova di fumo: il payload della variante, gia' costruito da roms, in una
# ROM per QEMU q35. Del W541 non si emula niente: girano il payload (le patch
# di EDK2 e LvglPkg) e il codice comune di coreboot. DRIVERS_EFI_FW_INFO con
# il GUID e la versione della variante, come sul W541: senza, FmpDxe del
# payload non sa che firmware e' e si ferma (misconfiguration). Niente
# USE_UEFI_VARIABLE_STORE: il romstage di q35 non ha il flash scrivibile.
cmd_smoke() {
	need_prepared
	command -v qemu-system-x86_64 > /dev/null \
		|| die "serve qemu-system-x86_64 (Debian/Ubuntu: apt install qemu-system-x86)"
	if [ "${TOOLCHAIN}" = crossgcc ]; then
		xgcc_ok || die "crossgcc mancante in ${XGCC}: prima tools/build.sh toolchain"
		export PATH="${XGCC}/bin:${PATH}"
	fi
	local v q fd guid lsv
	for v in "${VARIANTS[@]}"; do
		q="q35-${v}"
		fd="${TREE}/build-${v}/UEFIPAYLOAD.fd"
		[ -f "${fd}" ] || die "smoke ${v}: manca ${fd}, prima tools/build.sh roms"
		guid="$(sed -n 's/^CONFIG_DRIVERS_EFI_MAIN_FW_GUID="\(.*\)"$/\1/p' "${O}/configs/w541-${v}.defconfig")"
		lsv="$(sed -n 's/^CONFIG_DRIVERS_EFI_MAIN_FW_LSV=\(.*\)$/\1/p' "${O}/configs/w541-${v}.defconfig")"
		[ -n "${guid}" ] && [ -n "${lsv}" ] || die "smoke ${v}: GUID o LSV dell'ESRT non nel defconfig"
		say "smoke ${v}: coreboot per QEMU q35 con il payload di ${v}"
		rm -rf "${TREE:?}/build-${q}"
		mkdir -p "${TREE}/build-${q}"
		{
			echo "CONFIG_VENDOR_EMULATION=y"
			echo "CONFIG_BOARD_EMULATION_QEMU_X86_Q35=y"
			echo "CONFIG_CPU_QEMU_X86_TSEG_SMM=y"
			echo "CONFIG_SMMSTORE=y"
			echo "CONFIG_PAYLOAD_ELF=y"
			echo "CONFIG_PAYLOAD_FILE=\"${fd}\""
			echo "CONFIG_GENERIC_LINEAR_FRAMEBUFFER=y"
			echo "CONFIG_DRIVERS_EMULATION_QEMU_XRES=1920"
			echo "CONFIG_DRIVERS_EMULATION_QEMU_YRES=1080"
			echo "CONFIG_DRIVERS_EFI_FW_INFO=y"
			echo "CONFIG_DRIVERS_EFI_MAIN_FW_GUID=\"${guid}\""
			echo "CONFIG_DRIVERS_EFI_MAIN_FW_LSV=${lsv}"
			echo "CONFIG_DRIVERS_EFI_MAIN_FW_VERSION=$(fw_version)"
			echo "CONFIG_LOCALVERSION=\"$(localversion "${v}")-q35\""
			if [ "${TOOLCHAIN}" = host ]; then echo "CONFIG_ANY_TOOLCHAIN=y"; fi
		} > "${TREE}/build-${q}/defconfig"
		mk "${q}" defconfig KBUILD_DEFCONFIG="build-${q}/defconfig" > "${TREE}/build-${q}/defconfig.log" 2>&1 \
			|| { cat "${TREE}/build-${q}/defconfig.log"; die "smoke ${v}: make defconfig fallito"; }
		defconfig_kept "${TREE}/build-${q}" || die "smoke ${v}: il .config di q35 non ha tutto il defconfig"
		if ! mk "${q}" -j"${JOBS}" > "${TREE}/build-${q}/make.log" 2>&1; then
			grep -n -i -B2 -A6 'error' "${TREE}/build-${q}/make.log" | tail -n 40 || true
			die "smoke ${v}: build per q35 fallita (build-${q}/make.log nell'albero)"
		fi
		say "smoke ${v}: QEMU"
		python3 "${O}/tools/qemu-smoke.py" --out "${WORK}/smoke-${v}" "${TREE}/build-${q}/coreboot.rom" \
			|| die "smoke ${v}: il payload non passa la prova in QEMU (${WORK}/smoke-${v})"
	done
}

# Le righe di defconfig comuni al payload e alle ROM della prova
# dell'aggiornamento: q35, SMMSTORE come sul W541, l'ESRT e le capsule su
# disco con il GUID di prova
update_test_q35() {
	printf '%s\n' "CONFIG_VENDOR_EMULATION=y" "CONFIG_BOARD_EMULATION_QEMU_X86_Q35=y" \
		"CONFIG_CPU_QEMU_X86_TSEG_SMM=y" "CONFIG_SMMSTORE=y" "CONFIG_SMMSTORE_SIZE=0x80000" \
		"CONFIG_DRIVERS_EFI_VARIABLE_STORE=y" "CONFIG_DRIVERS_EFI_FW_INFO=y" \
		"CONFIG_DRIVERS_EFI_MAIN_FW_GUID=\"${UPDATE_TEST_GUID}\"" \
		"CONFIG_DRIVERS_EFI_MAIN_FW_LSV=${UPDATE_TEST_LSV}" \
		"CONFIG_DRIVERS_EFI_UPDATE_CAPSULES=y" "CONFIG_DRIVERS_EFI_CAPSULE_ON_DISK_SUPPORT=y"
	if [ "${TOOLCHAIN}" = host ]; then echo "CONFIG_ANY_TOOLCHAIN=y"; fi
}

# La prova dell'aggiornamento. Le ROM del W541 e le chiavi delle release non
# c'entrano: la chiave RSA delle capsule di prova nasce qui, in un sottoshell
# che la cancella quando esce, anche dopo un errore (una trap EXIT nella
# funzione prenderebbe il posto di quella di install_vboot_key). Il payload
# (EDK2 con le patch del progetto e le opzioni EDK2 del defconfig mrc, ma
# DEBUG e per q35) si costruisce in build-update-test dell'albero; le ROM di
# q35 con vboot in un worktree dell'albero con la patch di prova, che
# l'albero delle ROM non vede mai
cmd_update_test() {
	need_prepared
	local t ut ab rom
	local -a caps=()
	for t in qemu-system-x86_64 mformat mcopy mdir mmd mdel openssl clang ld.lld; do
		command -v "${t}" > /dev/null \
			|| die "update-test: serve ${t} (Debian/Ubuntu: apt install qemu-system-x86 mtools openssl clang lld)"
	done
	if [ "${TOOLCHAIN}" = crossgcc ]; then
		xgcc_ok || die "crossgcc mancante in ${XGCC}: prima tools/build.sh toolchain"
		export PATH="${XGCC}/bin:${PATH}"
	fi
	ut="${WORK}/update-test"
	ab="${WORK}/coreboot-update-test"
	rm -rf "${ut:?}"
	mkdir -p "${ut}"
	(
		trap 'rm -f "${ut}/key.pem" "${ut}/signer.pem" "${ut}/cert.cnf"' EXIT
		update_test_build "${ut}" "${ab}"
	)
	say "update-test: QEMU"
	for rom in "${UPDATE_TEST_ROMS[@]}"; do caps+=("${ut}/${rom%% *}.cap"); done
	local rc=0 last
	python3 "${O}/tools/qemu-update.py" --out "${ut}/out" --lock-test "${ut}/slot-lock-test.efi" "${caps[@]}" \
		2>&1 | tee "${ut}/qemu-update.log" || rc=$?
	if [ "${rc}" != 0 ]; then
		# nella CI il log del job non si legge sempre: l'esito di ogni passo,
		# il motivo e la fine della seriale dell'ultimo avvio in un'annotazione
		last="$(find "${ut}/out" -maxdepth 1 -name 'serial-*.log' -printf '%T@ %p\n' 2>/dev/null \
			| sort -n | tail -n 1 | cut -d' ' -f2-)"
		failure_report "update-test" "${ut}/qemu-update.log" ${last:+"${last}"}
		die "update-test: l'aggiornamento non passa la prova in QEMU (${ut}/out)"
	fi
}

# La chiave di prova, il payload, le ROM A, B, C e D e le loro capsule in $1;
# il worktree in $2. Il nome di ogni ROM ha versione e variante, come quello
# delle release: il setup le legge dallo slot (patch edk2/0020)
update_test_build() {
	local ut="$1" ab="$2" b d fd rom n ver
	say "update-test: la chiave delle capsule di prova"
	(
		umask 077
		printf '%s\n' '[req]' 'distinguished_name = dn' 'x509_extensions = ext' 'prompt = no' \
			'[dn]' 'O = ouroBIOS' 'CN = ouroBIOS update test in QEMU (not a release key)' '[ext]' \
			'basicConstraints = critical,CA:FALSE' 'keyUsage = critical,digitalSignature' \
			'subjectKeyIdentifier = hash' > "${ut}/cert.cnf"
		openssl req -x509 -new -newkey rsa:2048 -sha256 -days 1 -nodes -config "${ut}/cert.cnf" \
			-keyout "${ut}/key.pem" -out "${ut}/cert.pem" 2> /dev/null
	) || die "update-test: openssl req fallito"

	say "update-test: payload EDK2 DEBUG per q35, che si fida della chiave di prova"
	b="${TREE}/build-update-test"
	rm -rf "${b:?}"
	mkdir -p "${b}"
	{
		update_test_q35
		echo "CONFIG_DRIVERS_EFI_MAIN_FW_VERSION=${UPDATE_TEST_LSV}"
		echo "CONFIG_OPTION_BACKEND_NONE=y"
		echo "CONFIG_PAYLOAD_EDK2=y"
		grep -E '^CONFIG_EDK2_' "${O}/configs/w541-mrc.defconfig" || true
		echo "CONFIG_EDK2_DEBUG=y"
		# il setup grafico del W541 (LvglPkg): con la seriale sarebbe di testo
		echo "CONFIG_EDK2_USE_LVGL_RENDERER=y"
		echo "CONFIG_DRIVERS_EFI_CAPSULE_TRUSTED_PUBLIC_CERT=\"${ut}/cert.pem\""
		echo "CONFIG_LOCALVERSION=\"ouroBIOS-update-test-payload\""
	} > "${b}/defconfig"
	mk update-test defconfig KBUILD_DEFCONFIG="build-update-test/defconfig" > "${b}/defconfig.log" 2>&1 \
		|| { cat "${b}/defconfig.log"; die "update-test: make defconfig del payload fallito"; }
	defconfig_kept "${b}" || die "update-test: il .config del payload non ha tutto il defconfig"
	if ! mk update-test -j"${JOBS}" > "${b}/make.log" 2>&1; then
		failure_report "update-test: payload" "${b}/make.log"
		grep -n -i -B2 -A6 'error' "${b}/make.log" | tail -n 40 || true
		die "update-test: build del payload fallita (build-update-test/make.log nell'albero)"
	fi
	fd="${b}/UEFIPAYLOAD.fd"
	[ -f "${fd}" ] || die "update-test: la build non ha fatto ${fd}"

	say "update-test: worktree dell'albero con ${UPDATE_TEST_PATCH}"
	git -C "${TREE}" worktree remove --force "${ab}" > /dev/null 2>&1 || true
	rm -rf "${ab:?}"
	git -C "${TREE}" worktree prune
	git -C "${TREE}" worktree add -q --detach "${ab}" HEAD || die "update-test: git worktree add fallito"
	git -C "${ab}" apply "${O}/${UPDATE_TEST_PATCH}" || die "update-test: ${UPDATE_TEST_PATCH} non applica"
	# vboot, l'unico submodule che serve a q35, e il crossgcc: quelli dell'albero
	{ rmdir "${ab}/3rdparty/vboot" && ln -s "${TREE}/3rdparty/vboot" "${ab}/3rdparty/vboot"; } \
		|| die "update-test: 3rdparty/vboot nel worktree"
	if [ "${TOOLCHAIN}" = crossgcc ]; then ln -s "${XGCC}" "${ab}/util/crossgcc/xgcc"; fi

	for rom in "${UPDATE_TEST_ROMS[@]}"; do
		read -r n ver <<< "${rom}"
		say "update-test: ROM ${n} per q35 con vboot A/B, versione ${ver}"
		d="${ab}/build-${n}"
		mkdir -p "${d}"
		{
			update_test_q35
			echo "CONFIG_DRIVERS_EFI_MAIN_FW_VERSION=${ver}"
			echo "CONFIG_VBOOT=y"
			echo "CONFIG_VBOOT_SLOTS_RW_AB=y"
			echo "CONFIG_VBOOT_MOCK_SECDATA=y"
			echo "CONFIG_FMDFILE=\"src/mainboard/emulation/qemu-q35/update-test.fmd\""
			echo "CONFIG_PAYLOAD_ELF=y"
			echo "CONFIG_PAYLOAD_FILE=\"${fd}\""
			echo "CONFIG_DEBUG_SMI=y"
			printf 'CONFIG_LOCALVERSION="ouroBIOS-v%d.%d.%d-mrc+update-test-%s"\n' \
				$((ver >> 24)) $(((ver >> 16) & 0xff)) $((ver & 0xffff)) "${n}"
			if [ "${n}" = "${UPDATE_TEST_SCREEN}" ]; then
				echo "CONFIG_GENERIC_LINEAR_FRAMEBUFFER=y"
				echo "CONFIG_DRIVERS_EMULATION_QEMU_XRES=1920"
				echo "CONFIG_DRIVERS_EMULATION_QEMU_YRES=1080"
			fi
		} > "${d}/defconfig"
		mk_in "${ab}" "${n}" defconfig KBUILD_DEFCONFIG="build-${n}/defconfig" > "${d}/defconfig.log" 2>&1 \
			|| { cat "${d}/defconfig.log"; die "update-test: make defconfig della ROM ${n} fallito"; }
		defconfig_kept "${d}" || die "update-test: il .config della ROM ${n} non ha tutto il defconfig"
		if ! mk_in "${ab}" "${n}" -j"${JOBS}" > "${d}/make.log" 2>&1; then
			failure_report "update-test: ROM ${n}" "${d}/make.log"
			grep -n -i -B2 -A6 'error' "${d}/make.log" | tail -n 40 || true
			die "update-test: build della ROM ${n} fallita (${d}/make.log)"
		fi
	done

	say "update-test: l'applicazione UEFI che prova il blocco dell'altro slot"
	clang --target=x86_64-unknown-windows -std=c11 -O2 -Wall -Wextra -Werror -ffreestanding \
		-fno-stack-protector -mno-red-zone -mgeneral-regs-only -nostdlib -fuse-ld=lld \
		-Wl,-subsystem:efi_application -Wl,-entry:efi_main \
		-o "${ut}/slot-lock-test.efi" "${O}/tools/efi/slot-lock-test.c" \
		|| die "update-test: tools/efi/slot-lock-test.c non compila"

	say "update-test: capsule A, B, C e D, firmate con la chiave di prova"
	(umask 077; cat "${ut}/key.pem" "${ut}/cert.pem" > "${ut}/signer.pem") || die "update-test: signer.pem"
	for rom in "${UPDATE_TEST_ROMS[@]}"; do
		read -r n ver <<< "${rom}"
		generate_capsule "${ab}/build-${n}/coreboot.rom" "${ut}/${n}.cap" "${UPDATE_TEST_GUID}" "${ver}" \
			"${UPDATE_TEST_LSV}" "${ut}/signer.pem" "${ut}/cert.pem" || die "update-test: capsula ${n} non fatta"
	done
}

for c in "${CMDS[@]}"; do
	case "${c}" in
		prepare)       cmd_prepare ;;
		toolchain)     cmd_toolchain ;;
		config)        cmd_config ;;
		roms)          cmd_roms ;;
		smoke)         cmd_smoke ;;
		update-test)   cmd_update_test ;;
		keys)          cmd_keys ;;
		toolchain-key) cmd_toolchain_key ;;
	esac
done
