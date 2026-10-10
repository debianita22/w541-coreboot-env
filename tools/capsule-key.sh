#!/bin/bash
# capsule-key.sh - una nuova chiave per firmare le capsule di aggiornamento
# (docs/update.md)
#
#   tools/capsule-key.sh DIR
#
# In DIR, che non deve contenerle gia':
#   capsule-signing.key  la chiave privata RSA-3072 (PEM, PKCS#8): va nel
#                        secret CAPSULE_SIGNING_KEY del repository GitHub e in
#                        un backup offline. Mai nel repository.
#   capsule-signing.pem  il certificato autofirmato della chiave: va in
#                        keys/capsule-signing.pem, da li' nelle ROM, che
#                        accettano solo capsule firmate da questa chiave.
#
# Il firmware non guarda le date del certificato (EDK2 verifica la firma
# PKCS#7 con X509_V_FLAG_NO_CHECK_TIME) e accetta il certificato stesso come
# ancora di fiducia; i 100 anni di validita' servono solo alla verifica di
# openssl in tools/build.sh. Una ROM che si fida di un certificato accetta
# le capsule della chiave nuova solo dopo averne installata una (firmata
# dalla vecchia) che porta il certificato nuovo: cambiare chiave non chiede
# flashrom, perderla si'.
set -euo pipefail
umask 077

die() { printf '[x] %s\n' "$*" >&2; exit 1; }

DIR="${1:-}"
[ -n "${DIR}" ] || die "uso: tools/capsule-key.sh DIR"
command -v openssl >/dev/null || die "serve openssl"
mkdir -p "${DIR}"
KEY="${DIR}/capsule-signing.key"
CERT="${DIR}/capsule-signing.pem"
[ ! -e "${KEY}" ] && [ ! -e "${CERT}" ] || die "${DIR}: ci sono gia' capsule-signing.key o .pem"

cfg="$(mktemp)"
trap 'rm -f "${cfg}"' EXIT
cat > "${cfg}" << 'EOF'
[req]
distinguished_name = dn
x509_extensions = ext
prompt = no
[dn]
O = w541-coreboot-env
CN = W541 coreboot capsule signing
[ext]
basicConstraints = critical,CA:FALSE
keyUsage = critical,digitalSignature
subjectKeyIdentifier = hash
EOF
openssl req -x509 -new -newkey rsa:3072 -sha256 -days 36500 -nodes \
	-config "${cfg}" -keyout "${KEY}" -out "${CERT}" 2>/dev/null \
	|| die "openssl req fallito"
chmod 600 "${KEY}"
chmod 644 "${CERT}"

echo "chiave privata: ${KEY} (secret CAPSULE_SIGNING_KEY + backup offline)"
echo "certificato:    ${CERT} (keys/capsule-signing.pem)"
openssl x509 -in "${CERT}" -noout -subject -enddate -fingerprint -sha256
