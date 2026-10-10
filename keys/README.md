# Keys

`capsule-signing.pem` is the certificate the firmware trusts for update
capsules ([docs/update.md](../docs/update.md)): `tools/build.sh` copies it
into the coreboot tree, where the EDK2 payload build turns it into the
certificate FmpDxe checks the PKCS#7 signature of every capsule against. A
capsule installs only if it is signed by its private key.

| | |
|---|---|
| Subject | `O=w541-coreboot-env, CN=W541 coreboot capsule signing` |
| Key | RSA 3072, self-signed, valid until 2126 (the firmware ignores dates) |
| SHA-256 | `BE:A3:E5:65:60:43:87:F6:61:07:82:33:4B:90:BB:7E:97:35:AF:16:08:16:AB:CA:78:8F:3F:C1:24:9C:6A:B5` |

```sh
openssl x509 -in keys/capsule-signing.pem -noout -subject -fingerprint -sha256
```

The private key is never in the repository (`tools/ci-check.sh` fails on
one). It is the GitHub Actions secret `CAPSULE_SIGNING_KEY` (Settings →
Secrets and variables → Actions), the whole PEM file, which the release
build passes to `tools/build.sh`, and an offline copy with the maintainer.

A new key: `tools/capsule-key.sh DIR` writes `capsule-signing.key` (for the
secret) and `capsule-signing.pem` (for this folder). Laptops already in the
field trust the old certificate until they run a firmware with the new one:
the first release signed with the new key installs with flashrom, or comes
after one transition release signed with the old key that carries the new
certificate.
