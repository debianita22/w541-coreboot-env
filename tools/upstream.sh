#!/bin/bash
# upstream.sh - coreboot o EDK2 si sono mossi dal pin? Lo lancia
# .github/workflows/upstream.yml ogni settimana. Solo avvisi: un issue per
# ciascuno, aggiornato a ogni giro e chiuso quando non c'e' piu' niente da
# fare. I pin si spostano a mano.
#
#   tools/upstream.sh coreboot [--dry-run]
#   tools/upstream.sh edk2 [--dry-run]
#
# coreboot  la serie di patches/ applica ancora su main? Qualche patch e' gia'
#           entrata upstream (stesso Subject)? C'e' un tag di release dopo il
#           pin? Se si', issue "coreboot: ...".
# edk2      il ramo uefipayload_AAMM piu' recente di MrChromebox e' andato
#           oltre il commit pinnato nei defconfig? Se si', issue "EDK2: ...".
#
# Con --dry-run scrive soltanto cosa farebbe (niente issue).
set -euo pipefail
O="$(cd "$(dirname "$0")/.." && pwd)"
WHAT="${1:-}"
DRY=no
[ "${2:-}" = "--dry-run" ] && DRY=yes

note() { if [ -n "${GITHUB_ACTIONS:-}" ]; then echo "::$1 title=${WHAT}::$2"; fi; echo "$2"; }
die()  { note error "$*"; exit 1; }
TMP="$(mktemp -d)"
trap 'rm -rf "${TMP}"' EXIT

pin() { sed -n "s/^$1=\"\([^\"]*\)\".*/\1/p" "${O}/tools/build.sh"; }

# L'issue aperto con il titolo che comincia per $1, se c'e'
open_issue() {
	W541_PREFIX="$1" gh issue list --state open --search "\"$1\" in:title" --json number,title \
		--jq '[.[] | select(.title | startswith(env.W541_PREFIX))][0].number // empty'
}

# Apre o aggiorna l'issue ($1 prefisso, $2 titolo, $3 testo)
report() {
	local num
	if [ "${DRY}" = yes ]; then
		note notice "$2 (prova: nessun issue)"
		printf '%s\n' "$3"
		return 0
	fi
	num="$(open_issue "$1")"
	if [ -n "${num}" ]; then
		gh issue edit "${num}" --title "$2" --body "$3" > /dev/null
		note notice "$2: issue #${num} aggiornato"
	else
		gh issue create --title "$2" --body "$3" > /dev/null
		note notice "$2: issue aperto"
	fi
}

# Chiude l'issue con prefisso $1, se c'e' ($2 il commento)
settle() {
	local num
	note notice "$2"
	[ "${DRY}" = no ] || return 0
	num="$(open_issue "$1")"
	if [ -n "${num}" ]; then gh issue close "${num}" --comment "$2" > /dev/null; fi
}

check_coreboot() {
	local repo commit desc base head tag newtag="" work="${TMP}" ahead when p sub stop="" merged=()
	repo="$(pin COREBOOT_REPO)"
	commit="$(pin COREBOOT_COMMIT)"
	desc="$(pin COREBOOT_DESCRIBE)"
	[ -n "${repo}" ] && [ -n "${commit}" ] && [ -n "${desc}" ] || die "pin di coreboot non trovato in tools/build.sh"
	base="${desc%%-*}"
	head="$(git ls-remote "${repo}" refs/heads/main | cut -f1)"
	[ -n "${head}" ] || die "main non trovato in ${repo}"
	# l'ultimo tag di release (YY.MM), se e' dopo quello del pin
	tag="$(git ls-remote --tags "${repo}" | sed -n 's|.*refs/tags/\([0-9][0-9]\.[0-9][0-9]\(\.[0-9]*\)\{0,1\}\)$|\1|p' | sort -V | tail -n 1)"
	if [ -n "${tag}" ] && [ "${tag}" != "${base}" ] && [ "$(printf '%s\n' "${tag}" "${base}" | sort -V | tail -n 1)" = "${tag}" ]; then
		newtag="${tag}"
	fi
	if [ "${head}" = "${commit}" ]; then
		settle "coreboot:" "coreboot main e' al commit pinnato (${commit:0:12})"
		return 0
	fi

	# main dalla data del pin: i soggetti per le patch gia' upstream e
	# l'albero su cui provare la serie
	when="$(gh api "repos/coreboot/coreboot/commits/${commit}" --jq .commit.committer.date)"
	git init -q "${work}/cb"
	git -C "${work}/cb" fetch -q --shallow-since="${when}" "${repo}" main
	git -C "${work}/cb" checkout -q --detach FETCH_HEAD
	ahead="$(git -C "${work}/cb" rev-list --count HEAD)"
	git -C "${work}/cb" log --format=%s HEAD > "${work}/subjects"

	export GIT_COMMITTER_NAME="w541-coreboot-env" GIT_COMMITTER_EMAIL="w541-coreboot-env@users.noreply.github.com"
	while read -r p; do
		# il Subject della mail, anche se va a capo, senza [PATCH]
		sub="$(sed -n '/^Subject: /{:a;N;/\n[^ ]/!ba;p;q}' "${O}/patches/${p}" | sed '$d' | tr -d '\n' \
			| sed 's/^Subject: \(\[PATCH[^]]*\] \)\{0,1\}//; s/^ *//')"
		if grep -qxF -- "${sub}" "${work}/subjects"; then merged+=("${p}: ${sub}"); fi
		if [ -z "${stop}" ] && ! git -C "${work}/cb" am -q "${O}/patches/${p}" > /dev/null 2>&1; then
			git -C "${work}/cb" am --abort > /dev/null 2>&1 || true
			stop="${p}"
		fi
	done < <(sed -e 's/#.*//' -e 's/[[:space:]]*$//' "${O}/patches/series" | sed '/^$/d')

	if [ -z "${stop}" ] && [ ${#merged[@]} -eq 0 ] && [ -z "${newtag}" ]; then
		settle "coreboot:" "coreboot main (${head:0:12}, ~${ahead} commit dopo il pin): la serie applica, nessuna patch upstream, nessun tag nuovo"
		return 0
	fi
	local title="coreboot:" body
	[ -z "${newtag}" ] || title+=" release ${newtag},"
	[ -z "${stop}" ] || title+=" la serie si ferma a ${stop%%-*},"
	[ ${#merged[@]} -eq 0 ] || title+=" ${#merged[@]} patch upstream,"
	title="${title%,}"
	body="$(
		echo "coreboot \`main\` e' a \`${head:0:12}\`, circa ${ahead} commit dopo il pin \`${desc}\` (\`tools/build.sh\`)."
		echo
		if [ -n "${newtag}" ]; then echo "- **Release nuova**: \`${newtag}\` (il pin e' su \`${base}\`)."; fi
		if [ -n "${stop}" ]; then
			echo "- **La serie non applica su main**: si ferma a \`${stop}\` (le patch prima applicano)."
		else
			echo "- La serie di \`patches/series\` applica su main."
		fi
		if [ ${#merged[@]} -gt 0 ]; then
			echo "- **Gia' upstream** (stesso Subject su main), da togliere dalla serie:"
			printf '  - `%s`\n' "${merged[@]}"
		fi
		echo
		echo "Per spostare il pin (a mano):"
		echo "1. \`COREBOOT_COMMIT\` e \`COREBOOT_DESCRIBE\` (\`git describe --abbrev=12\`) in \`tools/build.sh\`;"
		echo "2. le patch ribasate e riesportate con \`git format-patch --zero-commit\`, \`patches/series\` aggiornato;"
		echo "3. \`tools/build.sh prepare config\`, poi push su un ramo \`ci-test/...\` per la build di prova;"
		echo "4. se e' verde, merge e una release (pre-release finche' non e' provata sul portatile)."
		echo
		echo "Questo issue lo aggiorna e lo chiude \`upstream.yml\`."
	)"
	report "coreboot:" "${title}" "${body}"
}

check_edk2() {
	local rev branch head cmp status ahead behind last
	rev="$(sed -n 's/^CONFIG_EDK2_TAG_OR_REV="\([0-9a-f]\{40\}\)"$/\1/p' "${O}/configs/w541-mrc.defconfig")"
	[ -n "${rev}" ] || die "CONFIG_EDK2_TAG_OR_REV (un commit) non trovato in configs/w541-mrc.defconfig"
	# i rami delle release di MrChromebox: uefipayload_AAMM (i vecchi AAAAMM,
	# _redux, _rwl_* e gli altri no)
	branch="$(git ls-remote --heads https://github.com/mrchromebox/edk2.git 'uefipayload_*' \
		| sed -n 's|.*refs/heads/\(uefipayload_[0-9]\{4\}\)$|\1|p' | sort -V | tail -n 1)"
	[ -n "${branch}" ] || die "nessun ramo uefipayload_* in mrchromebox/edk2"
	head="$(git ls-remote --heads https://github.com/mrchromebox/edk2.git "refs/heads/${branch}" | cut -f1)"
	if [ "${head}" = "${rev}" ]; then
		settle "EDK2:" "EDK2: ${branch} e' al commit pinnato (${rev:0:12})"
		return 0
	fi
	cmp="$(gh api "repos/mrchromebox/edk2/compare/${rev}...${head}" --jq '"\(.status) \(.ahead_by) \(.behind_by)"')"
	read -r status ahead behind <<< "${cmp}"
	last="$(gh api "repos/mrchromebox/edk2/compare/${rev}...${head}" \
		--jq '.commits[-15:] | reverse | .[] | "- `\(.sha[0:12])` \(.commit.message | split("\n")[0])"')"
	local title body
	if [ "${status}" = ahead ]; then
		title="EDK2: ${branch}, ${ahead} commit dopo il pin"
	else
		title="EDK2: ${branch} non contiene il pin (${status}, +${ahead}/-${behind})"
	fi
	body="$(cat <<EOF
MrChromebox \`${branch}\` e' a \`${head:0:12}\`; i defconfig sono pinnati a \`${rev:0:12}\` (\`CONFIG_EDK2_TAG_OR_REV\`).
Confronto: ${status}, ${ahead} commit avanti, ${behind} indietro.

Gli ultimi:
${last}

Per spostare il pin (a mano): \`CONFIG_EDK2_TAG_OR_REV\` in tutti e due i defconfig, poi push su un ramo \`ci-test/...\` per la build di prova; prima di una release, prova sul portatile (boot, menu di setup, variabili UEFI in SMMSTORE, S3).

Questo issue lo aggiorna e lo chiude \`upstream.yml\`.
EOF
)"
	report "EDK2:" "${title}" "${body}"
}

case "${WHAT}" in
	coreboot) check_coreboot ;;
	edk2)     check_edk2 ;;
	*) die "uso: upstream.sh coreboot|edk2 [--dry-run]" ;;
esac
