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
#                  dell'albero (e' li' che li cercano i defconfig)
#   toolchain      il crossgcc di coreboot: i386 con Ada (libgfxinit), iasl e
#                  nasm. La prima volta 30-60 minuti, poi resta in
#                  util/crossgcc/xgcc dell'albero (e nella cache della CI)
#   config         solo il .config di ogni variante, con il controllo che
#                  nessuna riga del defconfig sia andata persa
#   roms           .config, build, tools/verify-rom.sh, file in dist/
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
#                   verify-rom.sh --release (build.yml)
#   --work DIR      cartella di lavoro (default: work/ in questo repository)
#   --dist DIR      i file finali (default: dist/ in questo repository)
#   --jobs N        default: nproc
#
# Ogni ROM e' un'immagine completa del flash da 12 MiB (chip da 8 MiB + chip
# da 4 MiB): IFD, GbE e ME di blobs/ negli 8 MiB bassi, identici a quelli di
# legacy/coreboot-4.22/coreboot.rom, e coreboot nei 4 MiB alti. In dist/ anche
# le immagini dei due chip, per un programmatore esterno. Dall'interno si
# aggiorna solo la regione BIOS: flashrom --ifd -i bios (docs/flashing.md).
set -euo pipefail
O="$(cd "$(dirname "$0")/.." && pwd)"

# coreboot: il commit pinnato e il suo git describe (l'albero di lavoro e' un
# clone di quel commit solo, senza tag: la versione nelle ROM la diamo noi)
COREBOOT_REPO="https://github.com/coreboot/coreboot.git"
COREBOOT_COMMIT="26317964960feed491a4ef1daf8b61f26c2020bb"
COREBOOT_DESCRIBE="26.09-53-g26317964960f"

# I submodule che servono a questa board, "nome percorso URL": gli URL di
# .gitmodules sono review.coreboot.org, qui i mirror su GitHub. Gli altri non
# si scaricano e make gira con UPDATED_SUBMODULES=1 (niente fetch da solo).
SUBMODULES=(
	"vboot 3rdparty/vboot https://github.com/coreboot/vboot.git"
	"libhwbase 3rdparty/libhwbase https://github.com/coreboot/libhwbase.git"
	"libgfxinit 3rdparty/libgfxinit https://github.com/coreboot/libgfxinit.git"
	"intel-microcode 3rdparty/intel-microcode https://github.com/coreboot/intel-microcode.git"
)

# I file di blobs/ e assets/ che vanno nell'albero (w541/<nome>): i defconfig
# li cercano li'. mrc.bin lo usa solo la variante mrc.
TREE_FILES=(blobs/ifd.bin blobs/gbe.bin blobs/me.bin blobs/mrc.bin
	blobs/vbios_10de_11fc_1.rom assets/bootsplash.bmp)

# git am: il committer e le date fisse rendono uguale ogni volta il commit in
# cima all'albero (genbuild_h.sh ne prende data e hash)
export GIT_COMMITTER_NAME="w541-coreboot-env"
export GIT_COMMITTER_EMAIL="w541-coreboot-env@users.noreply.github.com"

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
		msg+="== ${f#"${WORK}/"}"$'\n'"$(tail -n 40 "${f}" | cut -c1-300)"$'\n'
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
		prepare|toolchain|config|roms|toolchain-key) CMDS+=("$1"); shift ;;
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

# Le patch di patches/series, una per riga (vuote e # saltate)
series() { sed -e 's/#.*//' -e 's/[[:space:]]*$//' "${O}/patches/series" | sed '/^$/d'; }

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
		for p in "${TREE_FILES[@]}"; do sha256sum "${p}"; done
	) | sha256sum | cut -d' ' -f1
}
need_prepared() {
	[ -f "${STAMP}" ] && [ "$(cat "${STAMP}")" = "$(state)" ] \
		|| die "l'albero in ${TREE} non e' pronto o e' di altre patch: prima tools/build.sh prepare (con le stesse --with)"
}

# make nell'albero, con la cartella di build della variante $1
mk() {
	local v="$1"; shift
	make -C "${TREE}" obj="build-${v}" DOTCONFIG="build-${v}/.config" \
		UPDATED_SUBMODULES=1 KERNELVERSION="${COREBOOT_DESCRIBE}" "$@"
}

# Il nome della variante $1 con le patch opzionali: mrc, mrc+test-peg-afe
flavour() {
	local f="$1" w
	for w in "${WITH[@]}"; do f+="+${w}"; done
	printf '%s' "${f}"
}
# CONFIG_LOCALVERSION della variante $1, e il nome dei suoi file in dist/
localversion() { printf 'w541-%s-%s' "${VERSION}" "$(flavour "$1")"; }
romname()      { printf 'w541-coreboot-%s-%s' "${VERSION}" "$(flavour "$1")"; }

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
	state > "${STAMP}"
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
	rm -rf "${TREE:?}/${obj}"
	mkdir -p "${TREE}/${obj}"
	{
		cat "${O}/configs/w541-${v}.defconfig"
		echo "CONFIG_LOCALVERSION=\"$(localversion "${v}")\""
		if [ "${TOOLCHAIN}" = host ]; then echo "CONFIG_ANY_TOOLCHAIN=y"; fi
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
	# ME e l'inizio vuoto della regione BIOS) e 0x800000-0xBFFFFF (coreboot,
	# CBFS_SIZE 0x400000)
	head -c 8388608 "${b}/coreboot.rom" > "${DIST}/${n}-8mb-chip.rom"
	tail -c 4194304 "${b}/coreboot.rom" > "${DIST}/${n}-4mb-chip.rom"
	cp "${b}/.config" "${DIST}/${n}.config"
	{
		echo "${n}"
		echo "coreboot ${COREBOOT_DESCRIBE} (${COREBOOT_COMMIT}) + $(series | wc -l) patches (patches/series)"
		local w
		for w in "${WITH[@]}"; do echo "+ patches/optional/${w}.patch"; done
		echo "CONFIG_LOCALVERSION=\"$(localversion "${v}")\""
		echo
		"${b}/cbfstool" "${b}/coreboot.rom" layout -w
		echo
		"${b}/cbfstool" "${b}/coreboot.rom" print -r COREBOOT
	} > "${DIST}/${n}-layout.txt"
}

cmd_roms() {
	need_prepared
	local v
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
	done
	(cd "${DIST}" && find . -maxdepth 1 -type f -name 'w541-coreboot-*' -printf '%f\n' | LC_ALL=C sort \
		| xargs -r sha256sum > SHA256SUMS)
	say "dist"
	ls -l "${DIST}"
}

for c in "${CMDS[@]}"; do
	case "${c}" in
		prepare)       cmd_prepare ;;
		toolchain)     cmd_toolchain ;;
		config)        cmd_config ;;
		roms)          cmd_roms ;;
		toolchain-key) cmd_toolchain_key ;;
	esac
done
