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
which are at the same offsets, so settings, boot entries, Secure Boot keys,
VPD and event log stay. From then on, updates are capsules.

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

On the laptop: not yet. The first v1.3.0 release is a pre-release.
