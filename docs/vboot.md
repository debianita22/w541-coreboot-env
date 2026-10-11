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
- [How it was tested](#how-it-was-tested)
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
alternate.

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

## How it was tested

In QEMU (q35), with the same EDK2 payload and coreboot patches, a map with
the same regions and vboot's nvdata in flash:

- a capsule written to the slot that is not running, the other slot and
  the read-only part unchanged, *try next* the new slot with two tries; the
  new slot boots and is confirmed; the next capsule goes to the other slot;
- a new slot cut short (half of it erased): the signature check fails and
  the laptop restarts from the old slot;
- a new slot that never reaches the payload: two tries, then
  *try_count used up; falling back*, from the old slot;
- `RW_NVRAM` full: the payload erases it and writes the request at the start.

## Testing on the laptop

v1.3.0-rc1 is the first release with vboot to run on the W541. QEMU does
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
   sudo cbmem -1 | grep -E 'Slot [AB] is selected|BM-LOCKDOWN|nvdata'
   #   Slot A is selected
   #   BM-LOCKDOWN: Enabled bootmedia protection
   #   and no "failed to save nvdata": vboot wrote RW_NVRAM from the bootblock
   cat /sys/class/dmi/id/bios_version          # ouroBIOS-v1.3.0-rc1-mrc
   sudo dmesg | grep 'efi: EFI v'              # ... by ouroBIOS
   sudo python3 tools/update.py status         # mappa: vboot, versione 1.3.0
   sudo flashrom -p internal 2>&1 | grep -E 'PR[0-4]|locked down'
   #   a PRx line: 0x00b60000-0x00bfffff is read-only
   ```

   In the setup: *Security* → *Flash protection* → *Firmware protection*
   on; *Firmware Update* shows 1.3.0.
3. **Suspend**: `systemctl suspend`, then wake with the lid or the power
   button, twice.
4. **A/B switch** with the same capsule: slot B already holds the same
   code, so nothing is written there, but vboot is asked to try B.

   ```sh
   sudo python3 tools/update.py stage --allow-older w541-coreboot-v1.3.0-rc1-mrc.cap
   sudo systemctl reboot
   sudo cbmem -1 | grep 'Slot [AB] is selected'      # Slot B is selected
   ```

   Reboot three more times: always slot B. A new slot gets two tries; if
   the payload could not mark it good, the third boot would go back to A.
   The state vboot keeps in `RW_NVRAM`, from a copy of the flash:

   ```sh
   sudo flashrom -p internal -r flash.bin
   python3 tools/update.py vboot flash.bin
   #   prossimo:   slot B           (the slot of the next boot, and its tries)
   #   corrente:   slot B, -        (the slot of this boot: "in prova", "confermato")
   #   precedente: slot B, -
   ```
5. **A whole slot written by the capsule** (programmer at hand): erase
   slot A, which is not running, and let the capsule rewrite all of it,
   2.8 MiB across the two chips.

   ```sh
   cp w541-coreboot-v1.3.0-rc1-mrc.rom bad.rom
   dd if=/dev/zero of=bad.rom bs=64K seek=$((0x5C0000 / 0x10000)) count=45 conv=notrunc
   sudo flashrom -p internal --fmap-file bad.rom -i RW_SECTION_A -w bad.rom
   sudo systemctl reboot                             # still slot B
   sudo python3 tools/update.py stage --allow-older w541-coreboot-v1.3.0-rc1-mrc.cap
   sudo systemctl reboot
   sudo cbmem -1 | grep 'Slot [AB] is selected'      # Slot A is selected
   ```
6. **Fallback** (programmer at hand): spoil the slot that runs, A, and
   reboot: vboot must find it broken, write that to `RW_NVRAM` and boot B.

   ```sh
   cp w541-coreboot-v1.3.0-rc1-mrc.rom bad.rom
   dd if=/dev/zero of=bad.rom bs=64K seek=$((0x5D0000 / 0x10000)) count=1 conv=notrunc
   sudo flashrom -p internal --fmap-file bad.rom -i RW_SECTION_A -w bad.rom
   sudo systemctl reboot
   sudo cbmem -1 | grep 'Slot [AB] is selected'      # Slot B is selected
   sudo flashrom -p internal -r flash.bin && python3 tools/update.py vboot flash.bin
   #   precedente: slot A, fallito
   sudo python3 tools/update.py stage --allow-older w541-coreboot-v1.3.0-rc1-mrc.cap
   sudo systemctl reboot                             # slot A again, repaired
   ```

   The boot that finds A broken restarts before coreboot keeps its log:
   what is left of it is *precedente: slot A, fallito*. A laptop that
   restarts in a loop at power-on here, without the logo, cannot save
   `RW_NVRAM` from the bootblock: write the 4 MiB chip with the programmer.

Whatever does not match, keep `sudo cbmem -c > cbmem.txt` and the output
of the commands.
