#!/bin/bash
# vboot-keys.sh - le chiavi di vboot del progetto (docs/vboot.md)
#
#   FUTILITY=/percorso/futility tools/vboot-keys.sh DIR
#
# futility e' quello di coreboot (util/futility, lo costruisce la build in
# build-<variante>/util/futility/futility). In DIR, che non deve contenerle:
#
#   root_key.vbpubk           la chiave root, nel GBB della regione RO: le
#   firmware.keyblock         ROM accettano solo slot RW firmati da una chiave
#   recovery_key.vbpubk       che questa ha firmato (firmware.keyblock).
#   kernel_subkey.vbpubk      Pubbliche: vanno in keys/vboot/.
#   firmware_data_key.vbpubk
#
#   firmware_data_key.vbprivk firma gli slot RW a ogni release: in base64 nel
#                             secret VBOOT_SIGNING_KEY del repository GitHub
#                             (base64 -w0 firmware_data_key.vbprivk) e in un
#                             backup offline. Mai nel repository.
#   root_key.vbprivk          serve solo per firmare un'altra chiave dati
#                             (un keyblock nuovo): solo offline.
#   recovery_key.vbprivk,     non servono (questo firmware non ha recovery
#   kernel_subkey.vbprivk     ne' verifica il kernel): si possono cancellare.
#
# RSA-4096 con SHA-256. Una ROM si fida della chiave root che ha nel GBB:
# cambiare chiave root vuol dire riscrivere la regione RO (flashrom con
# "Firmware protection" spenta, o un programmatore esterno).
set -euo pipefail
umask 077

die() { printf '[x] %s\n' "$*" >&2; exit 1; }

DIR="${1:-}"
[ -n "${DIR}" ] || die "uso: FUTILITY=... tools/vboot-keys.sh DIR"
FUTILITY="${FUTILITY:-futility}"
command -v "${FUTILITY}" >/dev/null || die "serve futility (FUTILITY=build-mrc/util/futility/futility)"
command -v openssl >/dev/null || die "serve openssl"
mkdir -p "${DIR}"
for k in root_key recovery_key firmware_data_key kernel_subkey; do
	[ ! -e "${DIR}/${k}.vbpubk" ] && [ ! -e "${DIR}/${k}.vbprivk" ] || die "${DIR}: ci sono gia' chiavi"
done

tmp="$(mktemp -d)"
trap 'rm -rf "${tmp}"' EXIT
for k in root_key recovery_key firmware_data_key kernel_subkey; do
	openssl genrsa -out "${tmp}/${k}.pem" 4096 2>/dev/null
	"${FUTILITY}" --vb1 create --hash_alg 2 "${tmp}/${k}.pem" "${DIR}/${k}" > /dev/null
done
# il keyblock della chiave dati, firmato dalla root: flag 7 = modo normale e
# sviluppatore, non recovery (come le devkeys di vboot)
"${FUTILITY}" sign --signprivate "${DIR}/root_key.vbprivk" --flags 7 \
	--datapubkey "${DIR}/firmware_data_key.vbpubk" --outfile "${DIR}/firmware.keyblock" > /dev/null
"${FUTILITY}" show --publickey "${DIR}/root_key.vbpubk" "${DIR}/firmware.keyblock" > /dev/null \
	|| die "il keyblock non si verifica con la chiave root"
chmod 644 "${DIR}"/*.vbpubk "${DIR}/firmware.keyblock"
echo "chiavi in ${DIR}:"
ls -l "${DIR}"
echo
echo "secret VBOOT_SIGNING_KEY: base64 -w0 ${DIR}/firmware_data_key.vbprivk"
echo "pubbliche in keys/vboot/: *.vbpubk e firmware.keyblock"
echo "offline: root_key.vbprivk e firmware_data_key.vbprivk"
