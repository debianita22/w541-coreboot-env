# Firmware updates with UEFI capsules

Each release also has a signed UEFI capsule per variant,
`w541-coreboot-vX.Y.Z-mrc.cap` and `-nri.cap`. A W541 that already runs
v1.3.0 or later installs it from Linux, without flashrom (v1.2.x, with the
flash map before vboot, installs the capsules of v1.2.x only: see
[The first time](#the-first-time)):

```sh
curl -LO https://raw.githubusercontent.com/debianita22/ourobios/main/tools/update.py
sudo python3 update.py status
sudo python3 update.py stage --latest
sudo systemctl reboot
```

At the next boot the firmware checks the capsule, writes it with a progress
bar under the logo (about a minute), and starts again on the new version.
From v1.3.0 it writes the RW slot that is not running, and vboot goes back
to the old one if the new one does not verify or does not boot
([docs/vboot.md](vboot.md)). From v1.3.4, with the project's remote set up
once, `fwupdmgr update`, GNOME Software and KDE Discover stage the capsule
in the same way ([The fwupd remote](#the-fwupd-remote)).

- [What changes and what stays](#what-changes-and-what-stays)
- [The first time](#the-first-time)
- [tools/update.py](#toolsupdatepy)
- [What happens at boot](#what-happens-at-boot)
- [From the setup, with a USB drive](#from-the-setup-with-a-usb-drive)
- [fwupd](#fwupd)
- [Versions, variants, downgrades](#versions-variants-downgrades)
- [Going back](#going-back)
- [Without tools/update.py](#without-toolsupdatepy)
- [If it goes wrong](#if-it-goes-wrong)
- [How it works](#how-it-works)
- [The signing key](#the-signing-key)

## What changes and what stays

A capsule carries the whole 12 MiB image of the release, but the firmware
writes only a few of its regions ([flash map](flashing.md)):

| Region | |
|---|---|
| `RW_SECTION_A` or `RW_SECTION_B` | **written**, the slot that is not running: the new firmware |
| the other slot | kept: the firmware that runs now, where vboot goes back if the new one fails |
| `RW_MRC_CACHE` | **erased**: the first boot trains the memory again, a few seconds longer |
| `RW_NVRAM` | a record appended: *try* the new slot, two tries |
| `WP_RO` | kept: bootblock, vboot's root key, `mrc.bin` |
| `SMMSTORE` | kept: settings, boot entries, Secure Boot keys, supervisor password |
| `RO_VPD` | kept: serial number, machine type, UUID |
| `RW_ELOG` | kept: event log |
| descriptor, GbE, ME | kept |

(Up to v1.2.x: `COREBOOT`, the whole 4 MiB chip, and `RW_MRC_CACHE`.)

So, unlike flashrom, a capsule needs no `tools/vpd.py copy` and resets no
setting. Blocks that are already identical are not written again.
*BIOS Lock* and *Firmware protection* can stay on: the firmware writes the
capsule itself, from SMM, and never `WP_RO`.

## The first time

The capsules of v1.3.0 and later need a firmware with the same flash map,
v1.3.0 or later. Install it once with flashrom: from v1.0.3-v1.2.x as in
[docs/flashing.md](flashing.md#from-v12x-to-v130) (settings and VPD stay),
from other firmware as in [docs/flashing.md](flashing.md#internal-update)
(VPD copied into the image first, *BIOS Lock* off). From there on, every
update can be a capsule. `update.py status` tells whether the running
firmware applies them (*mappa*: *vboot*, or *senza vboot* for v1.2.x), and `stage` refuses a
capsule for the other map.

v1.2.0 to v1.2.2 apply the capsules of v1.2.x, which rewrite the whole
4 MiB chip, without A/B.

## tools/update.py

Python 3 and nothing else; it needs root, an EFI boot and the EFI system
partition (ESP) mounted (`/boot/efi`, `/efi`; elsewhere: `--esp DIR`).

| Command | |
|---|---|
| `status` | running variant and version, the lowest version it accepts, the result of the last update, the capsule waiting for the next boot |
| `stage --latest` | downloads the newest release of the running variant from GitHub (`--pre`: pre-releases too; `nri` releases are always pre-releases), checks its `SHA256SUMS` |
| `stage FILE.cap` | a capsule already downloaded |
| `cancel` | removes the waiting capsule and the request |
| `info FILE.cap` | variant, version, regions and signer of a capsule (or of the fwupd package, `FILE.cab`) |
| `rollback FLASH.bin` | back to the release in the slot that is not running, the one before the last update: see [Going back](#going-back) |
| `vboot FLASH.bin` | from a copy of the flash (`sudo flashrom -p internal -r FLASH.bin`): the slot of the next boot and its tries, the slot of this boot and of the one before, and whether vboot found one broken ([docs/vboot.md](vboot.md)) |

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
*ultimo* (last), the result of the update. Releases before v1.3.0 (and
v1.3.0-rc1 and rc2) never put the result in the ESRT: there *ultimo* stays
*nessun aggiornamento con capsula*.

## What happens at boot

With the bit set, coreboot gives the payload write access to the whole flash
for that boot only, and the payload, before anything else runs:

1. clears the bit, so that the next boot is a normal one whatever happens;
2. on battery, waits up to a minute for the AC adapter or for a charge of at
   least 25%, otherwise postpones the update (the capsule stays on the ESP:
   `update.py stage` again to retry);
3. reads and deletes the files in `\EFI\UpdateCapsule` of every EFI system
   partition and USB drive;
4. verifies each capsule: signature, variant, version, flash layout;
5. finds the slot that is running in `RW_NVRAM`, writes the other one,
   showing a progress bar under the logo and *Updating the firmware: do not
   turn off the computer*, and asks vboot to try it at the next boot;
6. shows *Firmware updated, restarting* (or *failed* and the reason) and
   restarts.

The next boot runs the new slot; when it reaches the boot logo, the payload
tells vboot that it works. If it does not verify, or two boots in a row do
not get that far, vboot boots the old slot: `update.py status` then shows
the old version.

A capsule that fails a check writes nothing: the laptop restarts on the old
firmware, and `update.py status` shows why (*firma non valida*: signature,
*versione non accettata*: version, *immagine non valida*: no region
manifest or the wrong size, *mappa del flash diversa*: a capsule for
another flash layout). If there is no capsule to apply, the firmware gives
the write access back and boots on.

## From the setup, with a USB drive

From v1.2.2, without an OS: *System Preferences* (Esc at power-on) →
*System Configuration* → *Firmware Update* shows the installed version, the
lowest version a capsule may have, and the result of the last update
(version and *successful*, or why it was refused).

1. Copy the `.cap` of the release into the folder `EFI/UpdateCapsule` of a
   USB drive formatted FAT (FAT32), and nothing else in that folder.
2. Connect the drive and the AC adapter, open *Firmware Update* and choose
   *Update from a USB drive*.
3. The setup checks the file (a capsule of the variant that runs, not older
   than the lowest accepted version, the only file in `EFI\UpdateCapsule` of
   the USB drives and of the ESP) and asks to confirm, with the two
   versions. *Yes* restarts: the firmware applies the capsule as in
   [What happens at boot](#what-happens-at-boot), signature check included,
   deletes the file from the drive and starts again on the new version.

If the setup refuses (*No update found*, *not an update for this firmware*,
*older than the oldest accepted update*, *N files*), nothing is changed.

## fwupd

From v1.2.2 each release also has `w541-coreboot-vX.Y.Z-mrc.cab` (and
`-nri.cab`): the same capsule as an fwupd package, with its metadata (the
ESRT GUID of the variant, the version in fwupd's *triplet* format) and a
`firmware.jcat` that signs both with the capsule key. fwupd checks the
variant, the version and the AC adapter, copies the capsule unchanged to
`\EFI\UpdateCapsule\fwupd-<GUID>.cap` and sets the same `OsIndications`
bit as `tools/update.py`: the firmware applies it in the same way, and after
the reboot `fwupdmgr get-devices` shows the new version and `fwupdmgr
get-history` the result. The project certificate must be in `/etc/pki/fwupd`
first, once:

```sh
sudo install -m 644 keys/capsule-signing.pem /etc/pki/fwupd/ourobios.pem
sudo systemctl restart fwupd
sudo fwupdmgr install --allow-reinstall w541-coreboot-vX.Y.Z-mrc.cab
```

Without it fwupd does not trust the metadata of the package, so it does not
take the version format from it, and the install stops at *Firmware version
formats were different, device was 'number' and release is 'triplet'*
(before any signature message: the ESRT gives fwupd a bare number, the
package says how to read it). The same certificate serves the remote below.

### The fwupd remote

From v1.3.4 the mrc release also has `ourobios.xml.gz`, the metadata of the
project's fwupd remote: the `.cab` of both variants of that release, with
their address and checksums, signed by the capsule key
(`ourobios.xml.gz.jcat`). The remote,
[`configs/fwupd/ourobios.conf`](../configs/fwupd/ourobios.conf), downloads
it from the latest release, so that `fwupdmgr update`, GNOME Software and
KDE Discover offer each new release to the variant that runs. Once, from a
clone of the repository:

```sh
sudo install -m 644 configs/fwupd/ourobios.conf /etc/fwupd/remotes.d/
sudo install -m 644 keys/capsule-signing.pem /etc/pki/fwupd/ourobios.pem
sudo systemctl restart fwupd
fwupdmgr refresh
```

Then, at each release:

```sh
fwupdmgr get-updates                       # ouroBIOS (mrc) System Update, New version: ...
fwupdmgr update
sudo systemctl reboot
```

fwupd loads the metadata only with a signature from a trusted certificate,
downloads the `.cab` from the release, checks its SHA-256 against the
metadata and installs it only if its own `firmware.jcat` is from a trusted
certificate too: the one in `/etc/pki/fwupd` serves both (fwupd puts the
certificates of `/etc/pki/fwupd` and `/etc/pki/fwupd-metadata` in one
trust store, verified with 2.0.20: either directory alone works).
`fwupd-refresh.timer`, GNOME Software and Discover refresh the metadata
about once a day.

- The remote offers releases, not release candidates: those go with
  `update.py stage` or their `.cab` by hand. An rc and its release have one
  version for fwupd (v1.3.4-rc2 and v1.3.4 are both 1.3.4), so from the rc
  `fwupdmgr update` finds nothing: `fwupdmgr reinstall` installs the
  release, as `update.py stage --latest` does.
- The metadata of a release has its nri too: a laptop that runs nri is
  offered nri, and an mrc one mrc.
- Until v1.3.4 is the latest release, `fwupdmgr refresh` finds no metadata
  (404).
- Without the certificate the refresh fails (*signatures were required,
  but none supplied*) and a `.cab` by hand stops at *Firmware version
  formats were different* (see above). Do not set `OnlyTrusted=false`
  instead.
- To remove it: delete the two files and restart fwupd.

Tested here with fwupd 2.0.20, its daemon on a simulated ESRT (v1.3.3) and
a copy of the release pages on a local server, with the redirect of
`releases/latest/download`: `fwupdmgr refresh`, `get-updates` (1.3.4, from
the metadata of v1.3.4-rc1 signed by a test key) and `update`, which
downloaded the `.cab` of the v1.3.4-rc1 release from GitHub and staged the
capsule with the `OsIndications` bit; then, with the metadata of
v1.3.4-rc2 as published, `refresh` and `reinstall` from the remote, the
capsule in the ESP identical to the `.cap` of the release. Refused: a
`.cab` with another checksum (*checksum invalid*), the metadata and the
package without the certificate.

### A package by hand

fwupd installs only packages signed by a certificate in `/etc/pki/fwupd`, so
trust the project certificate once:

```sh
sudo install -m 644 keys/capsule-signing.pem /etc/pki/fwupd/ourobios.pem
sudo systemctl restart fwupd
fwupdmgr get-devices                       # System Firmware, version 16908289 (= 1.2.1)
sudo fwupdmgr install w541-coreboot-v1.2.2-mrc.cab
sudo systemctl reboot
```

`--allow-older` and `--allow-reinstall` as in
[Versions, variants, downgrades](#versions-variants-downgrades). Tested
here with fwupd 2.0.20 on a simulated ESRT (the staging; the boot side is
the one of `tools/update.py`).

- Without the certificate fwupd refuses the package (*firmware signature
  missing or not trusted*, or *version formats were different*): do not set
  `OnlyTrusted=false` instead.
- A certificate in `/etc/pki/fwupd` is trusted for any device, not only for
  the firmware: whoever holds the project key could sign a package that fwupd
  would install on that computer for another device, and the remote could
  offer it, if the same person could also publish a release of the project.
  Remove the files when you no longer need them.
- The releases are not on LVFS: without the remote, download the `.cab` of
  the release.
- `tools/update.py info FILE.cab` checks a package: capsule, metadata,
  signatures; `tools/fwupd-cab.py check-metadata --cert
  keys/capsule-signing.pem ourobios.xml.gz` the metadata of the remote.

## Versions, variants, downgrades

The version in the capsule and in the firmware's ESRT entry is the release
number: v1.2.3 is `0x01020003`, 16908291 (`fwupdmgr get-devices` shows one or
the other, depending on the version format it picks).
A pre-release has the number of its release: v1.3.0-rc1 and v1.3.0 are both
1.3.0. `tools/update.py` tells them apart by the release written in the
image, so from an rc to a later rc or to the release it installs as usual;
back from the release to an rc, or the same one again, needs
`--allow-older` (fwupd sees one version: `--allow-reinstall`).

The firmware refuses capsules older than its lowest supported version,
v1.3.0 (v1.2.0 for v1.2.x): an older release goes back with flashrom. Older
capsules from that version up install with `--allow-older`.

`mrc` and `nri` are different firmware for the ESRT (each its own GUID):
a capsule installs only on its own variant. The GUIDs changed with the flash
map in v1.3.0, so fwupd and other tools do not offer a v1.3.0 package to a
v1.2.x firmware. To switch variant, flash the
other image with flashrom ([docs/flashing.md](flashing.md#internal-update)).

## Going back

After an update the slot that is not running still holds the release
before it, until the next update writes over it. `update.py rollback`
goes back to that release:

```sh
sudo modprobe -r spi_intel_platform spi_intel
sudo flashrom -p internal -r flash.bin
sudo python3 update.py rollback --dry-run flash.bin
sudo python3 update.py rollback flash.bin
sudo systemctl reboot
```

From the copy of the flash, taken in the boot that runs it, it finds the
slot this boot came from in `RW_NVRAM` and the release in the other one,
downloads the capsule of that release from GitHub (`--capsule FILE` for
one already downloaded), checks that it carries the same release for that
slot, and stages it with the same checks as `stage`. The firmware applies
it as any capsule: it finds the slot already written, so it writes
nothing there, clears `RW_MRC_CACHE` and asks vboot to try that slot,
with two tries; if it does not boot, vboot comes back to the slot of
now. `--dry-run` stops before staging and says how many 4 KiB blocks of
the slot differ from the release (none, normally).

It refuses when the other slot holds the same release, a newer one (vboot
already came back from it: `stage` tries it again), the other variant
(flashrom), or no whole release (empty, damaged or half written), and
when the copy of the flash is not of the running boot. Going back further
than the other slot works as before: `stage --allow-older FILE.cap`, which
writes the older release into the slot that is not running.

### From the setup

From v1.3.4, without an OS: *System Preferences* (Esc at power-on) →
*System Configuration* → *Firmware Update* shows the slot that runs and the
release in the other one (*Other slot*). *Go back to* that release asks to
confirm, then restarts into it as `update.py rollback` does: vboot tries the
slot at the next two boots and comes back to the slot of now if it does not
boot; only `RW_MRC_CACHE` is cleared, so the first boot trains the memory
again. Settings, boot entries and Secure Boot keys stay.

The setup checks what a capsule would: it refuses an empty or damaged slot,
the release that runs, the other variant and a release older than the
oldest accepted update; vboot checks the signature at the next boot. With a
newer release in the other slot (after going back), the entry reads *Switch
to* that release.

Only the setup can do it: the firmware refuses the switch once it starts a
boot option (an OS loader, the UEFI Shell), after `ExitBootServices()` and
after a resume from suspend, so an OS goes back only with a signed capsule
and its checks. What runs before the boot options, a `Driver####` or
`SysPrep####` option or the option ROM of a device, can still ask for it
(with Secure Boot it must be signed), but from v1.3.4 coreboot checks the
release of the other slot itself, as the setup does: the same variant, not
older than the lowest supported version of the running release (patch
0065). Without *BIOS Lock*, flashrom from the OS can rewrite the vboot data
anyway.

## Without tools/update.py

Any tool that delivers capsules on disk works the same way:

- fwupd: its remote or the `.cab` of the release, see [fwupd](#fwupd). For v1.2.0 and
  v1.2.1, which have no `.cab`, `sudo fwupdtool install-blob
  w541-coreboot-vX.Y.Z-mrc.cap` with the *System Firmware* device should
  stage the capsule the same way (not tested).
- By hand: copy the capsule into `EFI/UpdateCapsule/` on the ESP and set bit
  2 (`0x4`) of `OsIndications-8be4df61-93ca-11d2-aa0d-00e098032b8c` in
  efivarfs, keeping the other bits (4 bytes of attributes `07 00 00 00`, then
  the 8-byte value, in one write; `chattr -i` first if the file is
  immutable).

## If it goes wrong

- *Battery too low, firmware update postponed*: connect the AC adapter, then
  `sudo python3 update.py stage FILE.cap` (or `--latest`) and reboot again.
- *Firmware update failed (Security Violation)*: the capsule is not signed by
  the key of the project. *(Aborted)*: version below the lowest supported, a
  capsule without the region manifest, or one for another flash layout.
  *(Not Ready)*: a capsule for the other variant.
- *Firmware update requested, but the flash is not writable in this boot*:
  the request was set in a way coreboot did not see (for instance an
  `OsIndications` variable left half-written by a crash); the laptop boots
  on, stage the capsule again.
- *No firmware update found*: the bit was set but there was no file on an
  ESP; the laptop boots on.
- Power lost while writing: from v1.3.0 the slot being written does not
  verify, and vboot boots the old one; stage the capsule again. Up to
  v1.2.x the 4 MiB chip may be incomplete and the laptop may not start:
  recover it with an external programmer and the `-4mb-chip.rom` of a
  release, as in [docs/flashing.md](flashing.md#recovery).
- The new version does not show up after the update: vboot went back to the
  old slot (the new one did not verify, or did not boot twice). *ultimo*
  still shows the update as done: it is the result of writing the slot, not
  of booting it; `update.py status` adds a *nota* when the version that runs
  is not the one of the last capsule.
  `update.py vboot` on a copy of the flash shows which release is in each
  slot; a copy taken in the first boot after going back also shows
  *precedente: slot X, fallito* when the new slot did not verify (the next
  boot overwrites it). `cbmem -c` of that boot (*try_count used up; falling
  back*) tells the case of the two tries.

The event log (`tools/elog.py`, `elogtool`) shows the reboots of an update.

## How it works

- coreboot (`DRIVERS_EFI_FW_INFO`) tells the payload the GUID and the version
  of the firmware, which the EDK2 payload publishes in the ESRT through
  FmpDxe, as the system firmware (`fw_type` 1, patch `edk2/0011`); Linux
  shows it in `/sys/firmware/efi/esrt`. The ESRT is built again at every
  boot (EsrtFmpDxe, patch `edk2/0018`), with the version and the result of
  the last capsule (`last_attempt_version`, `last_attempt_status`), which
  FmpDxe keeps in its `FmpState` variable.
- `OsIndicationsSupported` announces capsules on disk. When `OsIndications`
  asks for them, coreboot (`DRIVERS_EFI_CAPSULE_ON_DISK_SUPPORT`) switches
  its SMMSTORE handler to the whole flash for that boot only.
- The payload (patches `edk2/0008-0013`) applies the capsules on disk before
  EndOfDxe, the point after which FmpDxe refuses to write: the payload has no
  PEI phase that could load them on a later boot. FmpDxe checks the PKCS#7
  signature against the certificate built into the ROM
  (`keys/capsule-signing.pem`) and the version against the lowest supported
  one; `FmpDeviceSmmLib` writes the regions listed by the manifest appended
  to the image (`RW_MRC_CACHE RW_SECTION_A RW_SECTION_B`, `CAPSULE_REGIONS`
  in `tools/build.sh`), through the SMMSTORE handler: of the two slots only
  the one that is not running, then a record in `RW_NVRAM` with *try next*
  and two tries (patch `edk2/0015`). An image without a manifest, or whose
  regions are not where the running firmware's flash map has them, is
  refused: upstream, such an image rewrites the whole BIOS region, settings
  and VPD included.
- After writing, the payload restarts. Without anything to write, it gives
  the whole-flash access back to coreboot (patch 0050) before Driver####
  options, option ROMs or the OS run, or restarts if that fails. It asks the
  SMMSTORE handler whether the flash is writable rather than trusting
  `OsIndications`, so that a variable store crafted to look different to
  coreboot and to the payload cannot leave it writable for the OS. coreboot
  also closes the decision on S3 resume (patch 0052): upstream, the SMM
  handler is reloaded on resume with the decision pending, the payload
  never runs, and the OS could take it and write the flash past the BIOS
  lock.
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
the old key and the new certificate inside. fwupd needs the new certificate
too, in `/etc/pki/fwupd`: until then its remote refuses the metadata of the
releases signed with the new key.
