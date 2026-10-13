# Verified boot and A/B updates

From v1.3.0 the firmware is split, as on Chromebooks and in Dasharo, into a
read-only part that is never updated by software and two copies of the rest,
the RW slots A and B. coreboot's vboot, in the read-only part, checks the
signature of a slot before it runs it. An update writes the slot that is
not running and then asks vboot to try it: if the write is cut short, if
the new slot does not verify, or if it does not boot, the laptop starts
again from the old slot.

- [Flash map](#flash-map)
- [What happens at boot](#what-happens-at-boot)
- [What an update does](#what-an-update-does)
- [Firmware protection](#firmware-protection)
- [Keys](#keys)
- [Limits](#limits)
- [Moving from v1.2.x](#moving-from-v12x)
- [How it is tested](#how-it-is-tested)
- [Testing on the laptop](#testing-on-the-laptop)

## Flash map

[`configs/w541.fmd`](../configs/w541.fmd), in the 12 MiB of the two chips:

| Range | Chip | Region | |
|---|---|---|---|
| `0x000000-0x4FFFFF` | 8 MiB | descriptor, GbE, ME | from `blobs/`, never written |
| `0x500000-0x597FFF` | 8 MiB | `RW_MRC_CACHE`, `SMMSTORE`, `RO_VPD`, `RW_ELOG` | where they were up to v1.2.x (then 32 KiB unused) |
| `0x5A0000-0x5AFFFF` | 8 MiB | `RW_NVRAM` | vboot's state: the slot to try, the tries left |
| `0x5B0000-0x5BFFFF` | 8 MiB | `RECOVERY_MRC_CACHE` | required by vboot, unused |
| `0x5C0000-0x88FFFF` | both | `RW_SECTION_A` | slot A: romstage, ramstage, the EDK2 payload, signed |
| `0x890000-0xB5FFFF` | 4 MiB | `RW_SECTION_B` | slot B, the same |
| `0xB60000-0xBFFFFF` | 4 MiB | `WP_RO` | FMAP, GBB (vboot's root key), bootblock with verstage, `mrc.bin`, microcode |

The read-only part holds only what is needed to verify and start a slot:
three copies of a 2 MiB payload do not fit in the 7 MiB BIOS region, so
there is no recovery firmware in it (see [Limits](#limits)).

## What happens at boot

1. The bootblock, in `WP_RO`, runs verstage: it reads `RW_NVRAM`, picks
   the slot to boot (*try next*), and checks its keyblock against the root
   key in the GBB, then the signature and the hash of the whole slot.
2. If the slot verifies, romstage, ramstage and the payload are loaded from
   it. The memory training (`mrc.bin`, or the native RAM init of the
   `nri` variant in romstage) runs as before.
3. If it does not, vboot marks it failed and restarts from the other one.
4. Once the payload reaches its console (the boot logo), it tells coreboot
   that the slot works (`SMMSTORE_CMD_VBOOT_SLOT_GOOD`, patches 0054 and
   `edk2/0014`).

A resume from suspend uses the slot the system booted from.

## What an update does

A capsule ([docs/update.md](update.md)) carries both slots. The payload,
in the boot that applies it:

1. reads `RW_NVRAM` and finds the slot that is running;
2. writes the other one (and clears `RW_MRC_CACHE`, so that the new code
   trains the memory again), verifying every block;
3. appends a record to `RW_NVRAM`: *try next* the new slot, two tries;
4. restarts.

The next boot runs the new slot. Every boot that reaches the payload
confirms it; if two boots in a row do not get there (a hang, a power-off),
vboot goes back to the old slot. If the new slot does not verify, it goes
back at once. The update after that writes the other slot, so the slots
alternate. Until then the old slot keeps the release before:
`update.py rollback` goes back to it ([docs/update.md](update.md#going-back)).

The capsule never writes `WP_RO`, `SMMSTORE`, `RO_VPD` or `RW_ELOG`.

## Firmware protection

*Security* → *Flash protection* → *Firmware protection*, **on by
default**: at every boot coreboot write-protects `WP_RO` with a Protected
Range Register of the SPI controller and locks it until the next reset
(patches 0055-0056). Nothing that runs later, the OS, flashrom or SMM
included, can change the bootblock or the root key, so a slot signed with
another key never runs. Updates with a capsule still install: they write
only the slots.

Turn it off only to write the whole BIOS region with flashrom (switching
variant, an update of the read-only part), then on again. *BIOS Lock*, in
the same menu, still controls who may write the rest of the flash.

## Keys

The root key in the GBB and the keyblock of the firmware data key are in
[`keys/vboot`](../keys/vboot/) ([keys/README.md](../keys/README.md)).
The release build signs both slots with the private firmware data key, the
GitHub Actions secret `VBOOT_SIGNING_KEY`; `tools/build.sh` checks that it
matches `keys/vboot/firmware_data_key.vbpubk`, and that the root key in the
GBB of the image is `keys/vboot/root_key.vbpubk`. A build without the
secret signs the slots with vboot's test keys: such an image works, but
release capsules do not install on it (their slots do not verify against
its root key, and vboot keeps the old one).

## Limits

- **No recovery path.** If neither slot verifies (both damaged, or a root
  key that does not match), the boot stops after verstage, without a
  display. Recovery is with an external programmer, as before
  ([docs/flashing.md](flashing.md#recovery)): the `-4mb-chip.rom` of a
  release holds `WP_RO` and slot B, enough to boot.
- **The read-only part is never updated by a capsule.** A fix in the
  bootblock, in `mrc.bin` or in the early microcode needs flashrom with
  *Firmware protection* off. Everything after verstage, the native RAM
  init included, is in the slots.
- **The slots must fit the read-only part already on the laptops.** The
  bootblock and verstage leave their state in cache-as-RAM for romstage:
  vboot's work buffer, the early console, the timestamps, the FMAP and
  CBFS caches. Their addresses come from the build, their format from the
  coreboot commit. A release that moved the coreboot pin or that layout
  would install slots that look for that state where the old read-only
  part does not leave it: they would not boot, and after two tries vboot
  would go back to the old slot. [`configs/ro-interface.txt`](../configs/ro-interface.txt)
  records the read-only part on the laptops (coreboot and cache-as-RAM of
  v1.3.0), and `tools/ci-check.sh` and `tools/verify-rom.sh` stop a build
  that differs. Moving the pin takes either checking that the new slots
  still read what the old read-only part leaves, or a release that writes
  the read-only part as well, with flashrom. For the same reason the early
  console (3 KiB, where vboot's messages no longer fit: `Pre-CBMEM
  romstage console overflowed`) can only grow with such a release.
- **No rollback protection for the slots.** vboot keeps its anti-rollback
  data in the TPM on Chromebooks; here it is mocked
  (`VBOOT_MOCK_SECDATA`), so that the firmware does not create spaces in
  the TPM, and the TPM is started by ramstage as without vboot (patch
  0057). Capsules still refuse versions older than the lowest supported
  one (v1.3.0), but whoever can write the flash can put back an older slot
  signed by the project.
- The layout keeps the regions written at runtime where they were, but the
  slots are new: moving to v1.3.0 needs flashrom once.

## Moving from v1.2.x

See [docs/flashing.md](flashing.md#from-v12x-to-v130): flashrom writes
every region of the new map except `SMMSTORE`, `RO_VPD` and `RW_ELOG`,
which are at the same offsets since v1.0.3, so settings, boot entries,
Secure Boot keys, VPD and event log stay. From then on, updates are
capsules.

## How it is tested

At every build, before a release (`tools/build.sh update-test`, the
*update-test* job of `build.yml`, `tools/qemu-update.py`): in QEMU (q35),
with the EDK2 payload of the project's patches (a DEBUG build that trusts a
key made for the test), coreboot's common code with the project's patches,
vboot with two RW slots and its nvdata in flash, and the W541's map cut down
to the regions an update touches. Three ROMs, A, B and C, and their capsules,
made as those of the releases; each capsule goes into `\EFI\UpdateCapsule`
of an EFI system partition with the capsule-on-disk bit of `OsIndications`,
as `update.py stage` puts it; QEMU starts from off at every reset:

- capsule B while A runs: the payload writes slot B and `RW_MRC_CACHE`, asks
  vboot to try B for two boots and restarts; slot A and the read-only part
  are unchanged, the capsule is gone from the ESP and the bit from
  `OsIndications`. vboot boots B and the payload confirms it;
- capsule C: it goes to slot A, which is no longer running, and boots;
- capsule B again, the release already in the other slot (`update.py
  rollback`): with QEMU's trace of the flash, not one byte written to the
  slots or the read-only part; vboot switches to B;
- capsule A, then one byte of the new slot changed in the flash: the
  signature check fails, the laptop restarts from B and records A as failed;
- capsule A again, then two boots cut right after verstage chose A:
  *try_count used up; falling back to slot B*;
- the ESRT at every boot: the version of the slot that runs, and as last
  attempt the version of the capsule, status 0 (patch `edk2/0018`). After a
  fallback the last attempt is still the capsule that was written, with
  status 0: the ESRT does not say that vboot went back (`update.py status`
  notes the version mismatch);
- after the five capsules, the read-only part is the one of ROM A, and a
  UEFI variable written before the first update is still in the store.

By hand, in QEMU with v1.3.0: `RW_NVRAM` full, the payload erases it and
writes the request at the start.

On the W541, v1.3.0-rc1: the move from v1.0.9 with flashrom kept settings
and VPD; vboot verified and booted slot A; verstage wrote `RW_NVRAM` from
the bootblock; the Protected Range Register covered `WP_RO` and the SPI
configuration was locked. A capsule wrote its request to try slot B, but
the next boot stayed on A: verstage, which runs before romstage sets up
the ACPI I/O space, read the sleep state from the legacy DMA controller,
took the cold boot for an S3 resume and kept the slot of the last boot
(`update.py vboot-work`: *S3_RESUME*, no *CHOSE_SLOT*). Patch 0058 fixes
it in v1.3.0-rc2; being in the bootblock, the fix reaches a laptop only
with flashrom. The update itself was recorded (`FmpState`, `Capsule####`
in `update.py vars`), but the ESRT that Linux reads had no last attempt:
EsrtDxe publishes a copy that capsules on disk never updated. v1.3.0-rc3
builds the ESRT from FmpDxe at every boot (patch `edk2/0018`).

Still on rc1, a later boot read a sleep state that was not a resume: vboot
tried slot B, which verified and booted from the 4 MiB chip, and the
payload confirmed it. In that boot an rc3 capsule, applied from slot B,
wrote slot A (the one not running, across the two chips) and asked to try
it; the boot after took a resume again and stayed on B. `update.py vboot`
on a copy of the flash: RO rc1, slot A rc3, slot B rc1, *prossimo: slot
A*; with `--image` and the rc3 capsule, all 720 blocks of slot A equal to
the release (487 had changed from rc1, 16 of them on the 4 MiB chip).

v1.3.0-rc3, installed with flashrom over rc1: the two boots after it chose
the slot from nvdata (`vboot-work`: *CHOSE_SLOT*, no *S3_RESUME*), the
second with *Firmware protection* back on; a suspend came back to the
desktop with *S3_RESUME* on the same slot. With the protection on, the
rc3 capsule asked for slot B: the next boot tried it and the payload
confirmed it, and `update.py vboot --image` found the read-only part and
both slots equal to the release. Three more boots stayed on B, the second
using the last try (`prossimo: slot B`, `corrente: slot B, -`), and
`update.py status` showed *ultimo: v1.3.0, riuscito*.

v1.3.0-rc5, from its capsule applied on rc3 (slot A rewritten from B):
the ME's interface gone from `lspci`, no `mei` lines in the kernel log,
the dGPU's HD audio probed with its four HDMI/DP outputs, no `SF size`
message in ramstage. Fallback (step 6), with the programmer at hand but
not needed: the first 64 KiB of `FW_MAIN_A` zeroed with flashrom, the
next power-on reset once in the bootblock (event log: last POST code
0x21) and booted rc3 from B, with *precedente: slot A, fallito*; the rc5
capsule applied from B rewrote all of slot A, which booted and was
confirmed.

## Testing on the laptop

All six steps passed on the W541 with the release candidates (above);
they are kept for the next releases and other laptops. From rc2 or later
a release installs with its capsule (step 4); from rc1, with flashrom. QEMU does
not show what depends on the laptop's SPI controller: vboot writing
`RW_NVRAM` from the bootblock, the payload writing a slot across the two
chips, the Protected Range Register; nor how long the hash of a 2.8 MiB slot
takes. Keep the external programmer ready with the `-4mb-chip.rom` of
v1.2.2, or the last 4 MiB of your backup: written to the 4 MiB chip, it
brings back the old firmware whatever goes wrong below. `cbmem` is
coreboot's `util/cbmem` ([docs/flashing.md](flashing.md#diagnosing-a-hang)).

1. **Install** as in [docs/flashing.md](flashing.md#from-v12x-to-v130).
2. **First boot**: a few seconds of black screen (memory training), then
   the splash with the logo.

   ```sh
   sudo cbmem -1 | grep -E 'area FW_MAIN_[AB] found|BM-LOCKDOWN'
   #   FMAP: area FW_MAIN_A found @ 5d0000 (2883520 bytes)    the slot in use
   #   BM-LOCKDOWN: Enabled bootmedia protection
   cat /sys/class/dmi/id/bios_version          # CBET4000 ouroBIOS-v1.3.0-mrc
   sudo dmesg | grep 'efi: EFI v'              # ... by ouroBIOS
   sudo python3 tools/update.py status         # mappa: vboot (update.py of main)
   sudo modprobe -r spi_intel_platform spi_intel
   sudo flashrom -p internal 2>&1 | grep -E 'PR[0-4]|locked down'
   #   SPI Configuration is locked down.
   #   PR0: Warning: 0x00b60000-0x00bfffff is read-only.
   sudo flashrom -p internal -r flash.bin && python3 tools/update.py vboot flash.bin
   #   RW_NVRAM: 1 record (or more): vboot wrote it from the bootblock
   #   corrente: slot A
   sudo cbmem -r 0x78007343 > vbwork.bin && python3 tools/update.py vboot-work vbwork.bin
   #   stato: ... CHOSE_SLOT, and no S3_RESUME in contesto: vboot chose the
   #   slot from nvdata, as on every cold boot (S3_RESUME only after a suspend)
   ```

   verstage prints *Slot A is selected* in the bootblock, before the
   memory: the 3 KiB console kept until then usually loses it, so look for
   the slot after the memory, in *FW_MAIN_A*.

   In the setup: *Security* → *Flash protection* → *Firmware protection*
   on; *Firmware Update* shows 1.3.0.
3. **Suspend**: `systemctl suspend`, then wake with the lid or the power
   button, twice.
4. **A/B switch** with the same capsule: slot B already holds what the
   capsule carries for it (the code of each slot is linked at its own
   address, so A and B differ by a few hundred bytes), so nothing is
   written there, but vboot is asked to try B.

   ```sh
   sudo python3 tools/update.py stage --allow-older w541-coreboot-v1.3.0-mrc.cap
   sudo systemctl reboot
   sudo cbmem -1 | grep 'area FW_MAIN_[AB] found'    # FW_MAIN_B
   ```

   Reboot three more times: always slot B. A new slot gets two tries; if
   the payload could not mark it good, the third boot would go back to A.
   `sudo python3 tools/update.py status` shows *ultimo: v1.3.0, riuscito*
   (the version of the capsule and the result of writing it).
   The state vboot keeps in `RW_NVRAM`, from a copy of the flash:

   ```sh
   sudo flashrom -p internal -r flash.bin
   python3 tools/update.py vboot flash.bin
   #   contenuto:  RO, slot A, slot B: the release written in each
   #   prossimo:   slot B           (the slot of the next boot, and its tries)
   #   corrente:   slot B, -        (the slot of this boot: "in prova", "confermato")
   #   precedente: slot B, -
   ```
5. **A whole slot written by the capsule** (programmer at hand, *BIOS
   Lock* off for the flashrom writes of steps 5 and 6): erase
   slot A, which is not running, and let the capsule rewrite all of it,
   2.8 MiB across the two chips.

   ```sh
   cp w541-coreboot-v1.3.0-mrc.rom bad.rom
   dd if=/dev/zero of=bad.rom bs=64K seek=92 count=45 conv=notrunc    # RW_SECTION_A, 0x5C0000
   sudo flashrom -p internal --fmap-file bad.rom -i RW_SECTION_A -w bad.rom
   sudo systemctl reboot                             # still slot B
   sudo python3 tools/update.py stage --allow-older w541-coreboot-v1.3.0-mrc.cap
   sudo systemctl reboot
   sudo cbmem -1 | grep 'area FW_MAIN_[AB] found'    # FW_MAIN_A
   sudo flashrom -p internal -r flash.bin
   python3 tools/update.py vboot --image w541-coreboot-v1.3.0-mrc.cap flash.bin
   #   slot A  ... = w541-coreboot-v1.3.0-mrc.cap, tutti i 720 blocchi da 4 KiB
   ```
6. **Fallback** (programmer at hand): spoil the slot that runs, A, and
   reboot: vboot must find it broken, write that to `RW_NVRAM` and boot B.
   With B running, the same with `seek=138` (`FW_MAIN_B`, 0x8A0000) and
   `-i RW_SECTION_B`: the boot goes back to A.

   ```sh
   cp w541-coreboot-v1.3.0-mrc.rom bad.rom
   dd if=/dev/zero of=bad.rom bs=64K seek=93 count=1 conv=notrunc     # FW_MAIN_A, 0x5D0000
   sudo flashrom -p internal --fmap-file bad.rom -i RW_SECTION_A -w bad.rom
   sudo systemctl reboot
   sudo cbmem -1 | grep 'area FW_MAIN_[AB] found'    # FW_MAIN_B
   sudo flashrom -p internal -r flash.bin && python3 tools/update.py vboot flash.bin
   #   precedente: slot A, fallito
   sudo python3 tools/update.py stage --allow-older w541-coreboot-v1.3.0-mrc.cap
   sudo systemctl reboot                             # slot A again, repaired
   ```

   The boot that finds A broken restarts before coreboot keeps its log:
   what is left of it is *precedente: slot A, fallito*. A laptop that
   restarts in a loop at power-on here, without the logo, cannot save
   `RW_NVRAM` from the bootblock: write the 4 MiB chip with the programmer.

Whatever does not match, keep `sudo cbmem -c > cbmem.txt` and the output
of the commands.
