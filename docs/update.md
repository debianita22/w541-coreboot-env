# Firmware updates with UEFI capsules

From v1.2.0 each release also has a signed UEFI capsule per variant,
`w541-coreboot-vX.Y.Z-mrc.cap` and `-nri.cap`. A W541 that already runs
v1.2.0 or later installs it from Linux, without flashrom:

```sh
curl -LO https://raw.githubusercontent.com/debianita22/w541-coreboot-env/main/tools/update.py
sudo python3 update.py status
sudo python3 update.py stage --latest
sudo systemctl reboot
```

At the next boot the firmware checks the capsule, writes it with a progress
bar under the logo (about a minute; do not power off), and starts again on
the new version.

- [What changes and what stays](#what-changes-and-what-stays)
- [The first time](#the-first-time)
- [tools/update.py](#toolsupdatepy)
- [What happens at boot](#what-happens-at-boot)
- [Versions, variants, downgrades](#versions-variants-downgrades)
- [Without tools/update.py](#without-toolsupdatepy)
- [If it goes wrong](#if-it-goes-wrong)
- [How it works](#how-it-works)
- [The signing key](#the-signing-key)

## What changes and what stays

A capsule carries the whole 12 MiB image of the release, but the firmware
writes only two of its regions ([flash map](flashing.md)):

| Region | |
|---|---|
| `COREBOOT` (FMAP and CBFS, the 4 MiB chip) | **written**: the new firmware |
| `RW_MRC_CACHE` | **erased**: the first boot trains the memory again, a few seconds longer |
| `SMMSTORE` | kept: settings, boot entries, Secure Boot keys, supervisor password |
| `RO_VPD` | kept: serial number, machine type, UUID |
| `RW_ELOG` | kept: event log |
| descriptor, GbE, ME | kept |

So, unlike flashrom, a capsule needs no `tools/vpd.py copy` and resets no
setting. Blocks that are already identical are not written again.
*BIOS Lock* can stay on: the firmware writes the capsule itself, from SMM.

## The first time

The firmware that applies capsules is v1.2.0. Install v1.2.0, or any later
release, once with flashrom, as in [docs/flashing.md](flashing.md#internal-update)
(VPD copied into the image first, *BIOS Lock* off). From there on, every
update can be a capsule. `update.py status` tells whether the running
firmware applies them.

## tools/update.py

Python 3 and nothing else; it needs root, an EFI boot and the EFI system
partition (ESP) mounted (`/boot/efi`, `/efi`; elsewhere: `--esp DIR`).

| Command | |
|---|---|
| `status` | running variant and version, the lowest version it accepts, the result of the last update, the capsule waiting for the next boot |
| `stage --latest` | downloads the newest release of the running variant from GitHub (`--pre`: pre-releases too; `nri` releases are always pre-releases), checks its `SHA256SUMS` |
| `stage FILE.cap` | a capsule already downloaded |
| `cancel` | removes the waiting capsule and the request |
| `info FILE.cap` | variant, version, regions and signer of a capsule |

`stage` checks what the firmware would check, so that a reboot is not
wasted: the variant (an `nri` capsule is refused on `mrc`), the version (not
below the lowest accepted one; the same or an older one only with
`--allow-older`), the regions, the AC adapter (`--on-battery` to skip), and,
with `openssl` installed and `keys/capsule-signing.pem` next to the script
(a clone of the repository) or `--cert`, the signature. Then it copies the
capsule to `\EFI\UpdateCapsule\w541-coreboot.cap` on the ESP and sets the
*capsule on disk* bit of the `OsIndications` UEFI variable. `--reboot`
restarts straight away.

After the reboot, `update.py status` shows the new version and, under
*ultimo* (last), the result of the update.

## What happens at boot

With the bit set, coreboot gives the payload write access to the whole flash
for that boot only, and the payload, before anything else runs:

1. clears the bit, so that the next boot is a normal one whatever happens;
2. on battery, waits up to a minute for the AC adapter or for a charge of at
   least 25%, otherwise postpones the update (the capsule stays on the ESP:
   `update.py stage` again to retry);
3. reads and deletes the files in `\EFI\UpdateCapsule` of every EFI system
   partition;
4. verifies each capsule: signature, variant, version, flash layout;
5. writes it, showing a progress bar under the logo and *Updating the
   firmware: do not turn off the computer*;
6. shows *Firmware updated, restarting* (or *failed* and the reason) and
   restarts.

A capsule that fails a check writes nothing: the laptop restarts on the old
firmware, and `update.py status` shows why (*firma non valida*: signature,
*versione non accettata*: version). If there is no capsule to apply, the
firmware gives the write access back and boots on.

## Versions, variants, downgrades

The version in the capsule and in the firmware's ESRT entry is the release
number: v1.2.3 is `0x01020003`, 16908291 (`fwupdmgr get-devices` shows one or
the other, depending on the version format it picks).
A pre-release has the number of its release: v1.3.0-rc1 and v1.3.0 are both
1.3.0, so going from one to the other needs `--allow-older`.

The firmware refuses capsules older than its lowest supported version,
v1.2.0: an older release goes back with flashrom. Older capsules from v1.2.0
up install with `--allow-older`.

`mrc` and `nri` are different firmware for the ESRT (each its own GUID):
a capsule installs only on its own variant. To switch variant, flash the
other image with flashrom ([docs/flashing.md](flashing.md#internal-update)).

## Without tools/update.py

Any tool that delivers capsules on disk works the same way:

- fwupd: the firmware appears in `fwupdmgr get-devices` (*System
  Firmware*) with its version and the result of the last update. There is no
  `.cab` in the releases for `fwupdmgr install`; `sudo fwupdtool
  install-blob w541-coreboot-vX.Y.Z-mrc.cap` with that device should stage
  the capsule the same way (not tested here).
- By hand: copy the capsule into `EFI/UpdateCapsule/` on the ESP and set bit
  2 (`0x4`) of `OsIndications-8be4df61-93ca-11d2-aa0d-00e098032b8c` in
  efivarfs, keeping the other bits (4 bytes of attributes `07 00 00 00`, then
  the 8-byte value, in one write; `chattr -i` first if the file is
  immutable).

## If it goes wrong

- *Battery too low, firmware update postponed*: connect the AC adapter, then
  `sudo python3 update.py stage FILE.cap` (or `--latest`) and reboot again.
- *Firmware update failed (Security Violation)*: the capsule is not signed by
  the key of the project. *(Aborted)*: version below the lowest supported,
  or a capsule for another flash layout. *(Not Ready)*: a capsule for the
  other variant.
- *No firmware update found*: the bit was set but there was no file on an
  ESP; the laptop boots on.
- Power lost while writing: the 4 MiB chip may be incomplete and the laptop
  may not start. Recover it with an external programmer and the
  `-4mb-chip.rom` of a release, as in [docs/flashing.md](flashing.md#recovery).
  Settings, VPD and event log are on the other chip and survive. Keep the
  programmer at hand for the first capsule update of a laptop: it is the
  first time the firmware writes the 4 MiB chip itself (a capsule of the
  version already installed writes nothing there, all its blocks are the
  same).

The event log (`tools/elog.py`, `elogtool`) shows the reboots of an update.

## How it works

- coreboot (`DRIVERS_EFI_FW_INFO`) tells the payload the GUID and the version
  of the firmware, which the EDK2 payload publishes in the ESRT through
  FmpDxe; Linux shows it in `/sys/firmware/efi/esrt`.
- `OsIndicationsSupported` announces capsules on disk. When `OsIndications`
  asks for them, coreboot (`DRIVERS_EFI_CAPSULE_ON_DISK_SUPPORT`) switches
  its SMMSTORE handler to the whole flash for that boot only.
- The payload (patches `edk2/0008-0010`) applies the capsules on disk before
  EndOfDxe, the point after which FmpDxe refuses to write: the payload has no
  PEI phase that could load them on a later boot. FmpDxe checks the PKCS#7
  signature against the certificate built into the ROM
  (`keys/capsule-signing.pem`) and the version against the lowest supported
  one; `FmpDeviceSmmLib` writes the regions listed by the manifest appended
  to the image (`RW_MRC_CACHE COREBOOT`, `CAPSULE_REGIONS` in
  `tools/build.sh`), only if the flash layout is the same, through the
  SMMSTORE handler.
- After writing, the payload restarts. Without anything to write, it gives
  the whole-flash access back to coreboot (patch 0050) before Driver####
  options, option ROMs or the OS run, or restarts if that fails. It asks the
  SMMSTORE handler whether the flash is writable rather than trusting
  `OsIndications`, so that a variable store crafted to look different to
  coreboot and to the payload cannot leave it writable for the OS.
- Capsules in memory (`UpdateCapsule()` at runtime, then a warm reset) go
  through the same checks; with `Clear memory at power-on`, coreboot keeps
  them out of the clearing (patch 0048). Whether the memory survives the
  reset with `mrc.bin` or the native RAM init is untested: capsules on disk
  are the supported way.

## The signing key

The ROMs trust one certificate, `keys/capsule-signing.pem`: an RSA-3072
self-signed certificate made by `tools/capsule-key.sh`. Its private key is
the GitHub Actions secret `CAPSULE_SIGNING_KEY` of the repository, the only
place `tools/build.sh` reads it from; a release `vX.Y.Z` does not build
without it, and the build checks that the key matches the certificate, that
the payload trusts the certificate, and the capsule it signed
(`tools/update.py info`). Keep an offline copy of the key: without it, the
next release has to be flashed with flashrom.

To change key (see `keys/README.md`): make a new pair with
`tools/capsule-key.sh`, put the certificate in `keys/` and the key in the
secret, and release. That release is signed with the new key, so the
firmware in the field, which trusts the old one, refuses it: install it once
with flashrom. Or, to stay with capsules, sign one transition release with
the old key and the new certificate inside.
