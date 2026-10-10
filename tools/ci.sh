#!/bin/bash
# ci.sh - i passi di build.yml attorno alla build: la versione, le note e la
# pubblicazione delle due release. La build vera e' tools/build.sh.
#
#   tools/ci.sh version              job setup: version, publish, prerelease
#                                    (in GITHUB_OUTPUT)
#   tools/ci.sh notes DIST VARIANTE  le note della release di quella variante
#                                    (stdout, in inglese come il README)
#   tools/ci.sh publish DIST         job release: <versione>-mrc e
#                                    <versione>-nri, con i file di DIST
#
# Regole (le stesse dei commenti di build.yml):
#   - versione vX.Y.Z o vX.Y.Z-qualcosa; con un trattino, o con la casella
#     prerelease, tutte e due le release sono pre-release
#   - nri e' sempre una pre-release: upstream la RAM init nativa di Haswell e'
#     ancora "[NOT COMPLETE]"
#   - una release che non e' pre-release solo da un commit del ramo principale
#   - "latest" solo alla mrc della versione piu' alta (sort -V)
set -euo pipefail
O="$(cd "$(dirname "$0")/.." && pwd)"

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die()  { printf '\033[31m[x] %s\033[0m\n' "$*" >&2; exit 1; }
summ() { echo "$*" >> "${GITHUB_STEP_SUMMARY:-/dev/null}"; }
# un'annotazione del job: si legge anche dall'API, senza scaricare il log
note() {
	local level="$1" title="$2" msg="$3"
	[ -n "${GITHUB_ACTIONS:-}" ] || return 0
	msg="${msg//'%'/'%25'}"; msg="${msg//$'\r'/}"; msg="${msg//$'\n'/'%0A'}"
	title="${title//'%'/'%25'}"; title="${title//:/'%3A'}"; title="${title//,/'%2C'}"
	echo "::${level} title=${title}::${msg}"
}
fail() { note error "$1" "$2"; die "$2"; }

VARIANTS=(mrc nri)
# Il titolo della variante nelle release
title_of() {
	case "$1" in
		mrc) echo "MRC (Intel mrc.bin RAM init)" ;;
		nri) echo "NRI (native RAM init)" ;;
	esac
}

# Un valore assegnato in tools/build.sh (COREBOOT_COMMIT, ...)
pin() { sed -n "s/^$1=\"\([^\"]*\)\".*/\1/p" "${O}/tools/build.sh"; }

# Il commit $1 e' nella storia del ramo principale di origin?
on_default_branch() {
	local c
	c="$(git -C "${O}" rev-parse --verify -q "$1^{commit}")" || return 1
	if [ "$(git -C "${O}" rev-parse --is-shallow-repository)" = true ]; then
		git -C "${O}" fetch -q --unshallow origin || return 1
	fi
	git -C "${O}" fetch -q --no-tags origin "+refs/heads/${DEFAULT_BRANCH}:refs/remotes/origin/${DEFAULT_BRANCH}" || return 1
	git -C "${O}" merge-base --is-ancestor "${c}" "refs/remotes/origin/${DEFAULT_BRANCH}"
}

# Il commit del tag $1 su origin, vuoto se non c'e' (il ^{} di un tag annotato)
tag_commit() {
	local out
	out="$(git -C "${O}" ls-remote --tags origin "refs/tags/$1" "refs/tags/$1^{}")" || return 1
	awk -v t="refs/tags/$1" '$2 == t "^{}" { p = $1 } $2 == t { l = $1 } END { print (p != "" ? p : l) }' <<< "${out}"
}

# Le release con il tag $1: righe "<id> <bozza true|false>". Le bozze le vede
# solo chi puo' scrivere (il job release).
release_ids() {
	W541_TAG="$1" gh api --paginate "repos/${GITHUB_REPOSITORY}/releases?per_page=100" \
		--jq '.[] | select(.tag_name == env.W541_TAG) | "\(.id) \(.draft)"'
}

# Il tag della release "latest", vuoto se non ce n'e'
latest_tag() {
	local out
	if out="$(gh api "repos/${GITHUB_REPOSITORY}/releases/latest" --jq .tag_name 2>&1)"; then
		printf '%s\n' "${out}"
	else
		case "${out}" in *"HTTP 404"*|*"Not Found"*) return 0 ;; esac
		echo "${out}" >&2
		return 1
	fi
}

# $1 e' una versione piu' alta di $2? sort -V, col trattino come ~ perche'
# v1.1.0-rc1 venga prima di v1.1.0. Il -mrc dei tag si toglie prima.
newer() {
	local a b
	a="$(printf '%s' "${1%-mrc}" | sed 's/-/~/g')"; b="$(printf '%s' "${2%-mrc}" | sed 's/-/~/g')"
	[ "${a}" != "${b}" ] && [ "$(printf '%s\n' "${a}" "${b}" | sort -V | tail -n 1)" = "${a}" ]
}

# Job setup: versione, se si pubblica, se e' una pre-release
cmd_version() {
	: "${GITHUB_EVENT_NAME:?}" "${GITHUB_REF_NAME:?}" "${GITHUB_RUN_NUMBER:?}" "${GITHUB_REPOSITORY:?}" "${DEFAULT_BRANCH:?}"
	local version publish=false prerelease=false v t rels
	if [ "${GITHUB_EVENT_NAME}" = push ] && [ "${GITHUB_REF_TYPE:-}" = tag ]; then
		version="${GITHUB_REF_NAME}"; publish=true
	elif [ "${GITHUB_EVENT_NAME}" = workflow_dispatch ] && [ -n "${IN_VERSION:-}" ]; then
		version="${IN_VERSION}"; publish=true
		prerelease="${IN_PRERELEASE:-false}"
	else
		version="ci-${GITHUB_RUN_NUMBER}-$(git -C "${O}" rev-parse --short=7 HEAD)"
	fi
	if [ "${publish}" = true ]; then
		[[ "${version}" =~ ^v[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.]+)?$ ]] \
			|| fail "Versione" "versione '${version}': vX.Y.Z o vX.Y.Z-rcN"
		case "${version}" in
			*-mrc|*-nri) fail "Versione" "${version}: la versione senza -mrc/-nri, i due tag li crea la release" ;;
			*-*) prerelease=true ;;
		esac
		if [ "${prerelease}" != true ]; then
			if [ "${GITHUB_EVENT_NAME}" = workflow_dispatch ] && [ "${GITHUB_REF_NAME}" != "${DEFAULT_BRANCH}" ]; then
				fail "Versione" "da ${GITHUB_REF_NAME} solo pre-release (casella prerelease o vX.Y.Z-rcN)"
			fi
			on_default_branch "${GITHUB_SHA:-HEAD}" \
				|| fail "Versione" "${version}: il commit non e' su ${DEFAULT_BRANCH}; da altri rami solo pre-release"
		fi
		for v in "${VARIANTS[@]}"; do
			t="${version}-${v}"
			if [ "${GITHUB_EVENT_NAME}" = workflow_dispatch ]; then
				[ -z "$(tag_commit "${t}")" ] || fail "Versione" "il tag ${t} esiste gia': un'altra versione"
			fi
			rels="$(release_ids "${t}")" || fail "Versione" "elenco delle release illeggibile"
			if grep -q ' false$' <<< "${rels}"; then fail "Versione" "la release ${t} esiste gia'"; fi
		done
	fi
	{
		echo "version=${version}"
		echo "publish=${publish}"
		echo "prerelease=${prerelease}"
	} >> "${GITHUB_OUTPUT:-/dev/null}"
	summ "### ${version}"
	if [ "${publish}" != true ]; then
		summ "Build di prova: nessuna release, le ROM negli artifact del run."
	elif [ "${prerelease}" = true ]; then
		summ "Release ${version}-mrc e ${version}-nri, tutte e due pre-release."
	else
		summ "Release ${version}-mrc (latest se e' la piu' alta) e ${version}-nri (pre-release)."
	fi
	echo "versione ${version}, release ${publish}, pre-release ${prerelease}"
}

# Le note della release della variante $2, dai file di $1
cmd_notes() {
	local d="${1:?cartella con le ROM}" v="${2:?variante}" version rom chip8 chip4 cfg lay repo sha cb cbdesc edk2 npatch nedk2 pre=no
	: "${VERSION:?}"
	repo="${GITHUB_REPOSITORY:-debianita22/w541-coreboot-env}"
	sha="${GITHUB_SHA:-$(git -C "${O}" rev-parse HEAD)}"
	version="${VERSION}"
	rom="w541-coreboot-${version}-${v}.rom"
	chip8="w541-coreboot-${version}-${v}-8mb-chip.rom"
	chip4="w541-coreboot-${version}-${v}-4mb-chip.rom"
	cfg="w541-coreboot-${version}-${v}.config"
	lay="w541-coreboot-${version}-${v}-layout.txt"
	for f in "${rom}" "${chip8}" "${chip4}" "${cfg}" "${lay}"; do [ -f "${d}/${f}" ] || die "${d}/${f} non c'e'"; done
	cb="$(pin COREBOOT_COMMIT)"
	cbdesc="$(pin COREBOOT_DESCRIBE)"
	edk2="$(sed -n 's/^CONFIG_EDK2_TAG_OR_REV="\(.*\)"$/\1/p' "${O}/configs/w541-${v}.defconfig")"
	npatch="$(sed -e 's/#.*//' -e '/^[[:space:]]*$/d' "${O}/patches/series" | wc -l)"
	nedk2="$(sed -e 's/#.*//' -e '/^[[:space:]]*$/d' "${O}/patches/edk2/series" "${O}/patches/lvglpkg/series" | wc -l)"
	case "${v}" in nri) pre=yes ;; esac
	case "${version}" in *-*) pre=yes ;; esac
	if [ "${PRERELEASE:-false}" = true ]; then pre=yes; fi

	echo "coreboot for the **Lenovo ThinkPad W541**, ${version}, $(title_of "${v}") variant."
	echo
	if [ "${v}" = mrc ]; then
		cat <<'EOF'
RAM is initialized by Intel's `mrc.bin`, the same blob as in the coreboot 4.22
image this repository started from (`legacy/coreboot-4.22`): the conservative
choice.
EOF
	else
		cat <<EOF
RAM is initialized by coreboot's own native code (NRI), with no \`mrc.bin\`.
Upstream coreboot still labels the Haswell native RAM init *[NOT COMPLETE]*,
so this variant is always published as a pre-release. If the laptop does not
come up after flashing it (black screen, no boot), recovery needs an external
programmer: see [Recovery](https://github.com/${repo}/blob/main/docs/flashing.md#recovery).
EOF
	fi
	if [ "${pre}" = yes ] && [ "${v}" = mrc ]; then
		echo
		echo "**Pre-release**: not yet confirmed on real hardware. Keep a backup of your flash and an external programmer at hand."
	fi
	cat <<EOF

| File | Use |
|---|---|
| \`${rom}\` | **complete 12 MiB flash image**: descriptor (IFD), GbE, Intel ME and coreboot |
| \`${chip4}\` | its last 4 MiB: all of coreboot, for an external programmer on the 4 MiB chip |
| \`${chip8}\` | its first 8 MiB: descriptor, GbE and ME of the machine the blobs come from, for the 8 MiB chip |
| \`${cfg}\` | the complete coreboot \`.config\` |
| \`${lay}\` | flash layout (FMAP) and CBFS contents |
| \`SHA256SUMS\` | checksums of the files above |

Flash layout (\`configs/w541.fmd\`): the first 5 MiB (IFD, GbE, ME) are
byte-identical to the coreboot 4.22 image in \`legacy/coreboot-4.22\`:
unlocked regions, ME reduced by \`me_cleaner -S\`, and the GbE region with the
MAC address of the machine these blobs come from. Then, still on the 8 MiB
chip, the regions coreboot writes at runtime (RAM training, UEFI variables,
VPD, event log), empty. coreboot itself is alone on the 4 MiB chip. An
update from Linux writes only the BIOS region, as below: it keeps the
machine's own descriptor, ME and MAC address.

**Update from Linux**, on a W541 that already runs coreboot with an unlocked
flash: only the BIOS region is written (details, VPD and recovery in
[docs/flashing.md](https://github.com/${repo}/blob/main/docs/flashing.md)).

\`\`\`sh
sha256sum -c SHA256SUMS --ignore-missing
sudo flashrom -p internal -r backup-\$(date +%F).rom        # whole 12 MiB, keep it off the laptop
ls -l backup-*.rom                                        # 12582912 bytes (8388608: add -p internal:ich_spi_mode=hwseq)
sudo flashrom -p internal --ifd -i bios -w ${rom}
\`\`\`

**External programmer** (recovery): \`${chip4}\` on the 4 MiB chip is all of
coreboot; leave the 8 MiB chip as it is. \`${chip8}\` would give the laptop
the descriptor, ME and MAC address of another machine: see
[docs/flashing.md](https://github.com/${repo}/blob/main/docs/flashing.md#external-programmer).

If flashrom says "Opened /dev/mtd0" and finds an 8192 kB "Opaque flash chip",
the kernel owns the SPI controller: run \`sudo modprobe -r spi_intel_platform
spi_intel\` first ([details](https://github.com/${repo}/blob/main/docs/flashing.md#before-you-start)).
If flashrom cannot map the flash, boot once with \`iomem=relaxed\` on the
kernel command line. Flashing resets the UEFI settings and boot entries: the
firmware then boots \`\\EFI\\BOOT\\BOOTX64.EFI\` from each disk, so install your
boot loader at that path too (Debian: \`sudo grub-install --removable\`), or
pick it once with *Boot From File* in the boot manager (Esc at power-on).

The NVIDIA GPU is **off by default**, as in upstream coreboot: turn it on in
the setup menu (Esc at power-on), *Hardware* → *NVIDIA discrete GPU*.

**Suspend to RAM (S3) does not resume yet**: the laptop sleeps and does not
come back. Use suspend-to-idle meanwhile (\`mem_sleep_default=s2idle\` on the
kernel command line). The event log records each entry into S3 and, after a
resume that stopped, its last POST code: [diagnosing a hang](https://github.com/${repo}/blob/main/docs/flashing.md#diagnosing-a-hang).

Inside: coreboot \`${cbdesc}\` ([${cb:0:12}](https://github.com/coreboot/coreboot/commit/${cb}))
with [${npatch} patches](https://github.com/${repo}/tree/${sha}/patches), EDK2 payload
(MrChromebox [\`${edk2:0:12}\`](https://github.com/mrchromebox/edk2/commit/${edk2})
with [${nedk2} patches](https://github.com/${repo}/blob/${sha}/patches/README.md#edk2-and-lvglpkg)
to EDK2 and LvglPkg: the setup menu laid out like System Preferences, mouse
support for the TrackPoint and the touchpad),
libgfxinit for the Intel GPU, the NVIDIA Quadro K2100M VBIOS exposed to the OS
through ACPI \`_ROM\` and the Optimus key the Windows NVIDIA driver asks for,
CPU microcode from coreboot's intel-microcode. Built with the coreboot cross
toolchain.
EOF

	# i cambi dall'ultima release di questa variante (pubblicata, antenata di
	# questo commit); serve la storia (checkout con fetch-depth 0)
	local prev="" t
	if [ -n "${GH_TOKEN:-}" ] && command -v gh >/dev/null 2>&1; then
		for t in $(gh api "repos/${repo}/releases?per_page=100" \
				--jq ".[] | select(.draft | not) | .tag_name | select(endswith(\"-${v}\"))" 2>/dev/null); do
			[ "${t}" != "${version}-${v}" ] || continue
			if git -C "${O}" merge-base --is-ancestor "${t}" "${sha}" 2>/dev/null; then
				prev="${t}"
				break
			fi
		done
	fi
	if [ -n "${prev}" ]; then
		echo
		echo "Changes since ${prev}:"
		git -C "${O}" log --no-merges --format='- %s' "${prev}..${sha}"
	fi
	cat <<EOF

Built by GitHub Actions from [\`${sha:0:12}\`](https://github.com/${repo}/commit/${sha}).
EOF
}

# Job release: le due release, bozze prima e pubblicate insieme alla fine
cmd_publish() {
	local d="${1:?cartella con le ROM}" sha v t tag rels id draft cur latest=false pre
	: "${VERSION:?}" "${GITHUB_REPOSITORY:?}" "${GITHUB_SHA:?}" "${DEFAULT_BRANCH:?}"
	sha="$(git -C "${O}" rev-parse --verify -q "${GITHUB_SHA}^{commit}")" || fail "Pubblica" "commit ${GITHUB_SHA} non trovato"
	pre="${PRERELEASE:-false}"
	case "${VERSION}" in *-*) pre=true ;; esac
	if [ "${pre}" != true ] && ! on_default_branch "${sha}"; then
		fail "Pubblica" "${sha:0:12} non e' su ${DEFAULT_BRANCH}: da qui solo pre-release"
	fi
	# "latest" solo alla mrc piu' alta, mai a una pre-release
	if [ "${pre}" != true ]; then
		cur="$(latest_tag)" || fail "Pubblica" "la release latest non si legge"
		if [ -z "${cur}" ] || newer "${VERSION}" "${cur}"; then latest=true; fi
	fi
	for v in "${VARIANTS[@]}"; do
		t="${VERSION}-${v}"
		say "${t}"
		# il tag, se c'e' gia', deve essere sul commit della build
		tag="$(tag_commit "${t}")" || fail "Pubblica" "origin non risponde (git ls-remote)"
		if [ -n "${tag}" ] && [ "${tag}" != "${sha}" ]; then
			fail "Pubblica" "il tag ${t} e' su ${tag:0:12}, la build su ${sha:0:12}"
		fi
		# una release pubblicata ferma tutto; le bozze sono di un tentativo
		# fallito: via
		rels="$(release_ids "${t}")" || fail "Pubblica" "elenco delle release illeggibile"
		while read -r id draft; do
			[ -n "${id}" ] || continue
			[ "${draft}" = true ] || fail "Pubblica" "la release ${t} e' gia' pubblicata"
			echo "  bozza ${id} di un tentativo precedente: la cancello"
			gh api -X DELETE "repos/${GITHUB_REPOSITORY}/releases/${id}" > /dev/null
		done <<< "${rels}"
		# i file della variante e un SHA256SUMS solo loro
		rm -rf "${d}/release-${v}"
		mkdir -p "${d}/release-${v}"
		local f
		for f in "w541-coreboot-${VERSION}-${v}.rom" "w541-coreboot-${VERSION}-${v}-8mb-chip.rom" \
			"w541-coreboot-${VERSION}-${v}-4mb-chip.rom" "w541-coreboot-${VERSION}-${v}.config" \
			"w541-coreboot-${VERSION}-${v}-layout.txt"; do
			[ -f "${d}/${f}" ] || fail "Pubblica" "${f} non c'e' negli artifact"
			cp "${d}/${f}" "${d}/release-${v}/"
		done
		(cd "${d}/release-${v}" && sha256sum -- w541-coreboot-* > SHA256SUMS)
		cmd_notes "${d}" "${v}" > "${d}/release-${v}/NOTES.md"
		local flags=()
		if [ "${pre}" = true ] || [ "${v}" = nri ]; then flags+=(--prerelease); fi
		gh release create "${t}" --repo "${GITHUB_REPOSITORY}" --draft --target "${sha}" \
			--title "ThinkPad W541 coreboot ${VERSION} - $(title_of "${v}")" \
			--notes-file "${d}/release-${v}/NOTES.md" "${flags[@]}"
		(cd "${d}/release-${v}" && gh release upload "${t}" --repo "${GITHUB_REPOSITORY}" --clobber \
			./w541-coreboot-* SHA256SUMS)
	done
	# pubblicate insieme: mai una sola delle due visibile per un errore a meta'
	for v in "${VARIANTS[@]}"; do
		t="${VERSION}-${v}"
		if [ "${v}" = mrc ]; then
			gh release edit "${t}" --repo "${GITHUB_REPOSITORY}" --draft=false --latest="${latest}"
		else
			gh release edit "${t}" --repo "${GITHUB_REPOSITORY}" --draft=false --latest=false
		fi
		summ "### Pubblicata: https://github.com/${GITHUB_REPOSITORY}/releases/tag/${t}"
		note notice "Release" "${t} pubblicata"
	done
	if [ "${pre}" = true ]; then
		echo "  mrc e nri pre-release"
	elif [ "${latest}" = true ]; then
		echo "  ${VERSION}-mrc latest, nri pre-release"
	else
		echo "  ${VERSION}-mrc non latest (resta ${cur}), nri pre-release"
	fi
}

case "${1:-}" in
	version) cmd_version ;;
	notes)   shift; cmd_notes "$@" ;;
	publish) shift; cmd_publish "$@" ;;
	*) die "uso: ci.sh version | notes DIST VARIANTE | publish DIST" ;;
esac
