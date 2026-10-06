# Flashing and recovery

The W541 has two SPI flash chips that the chipset sees as one 12 MiB space: an
8 MiB chip (`0x000000-0x7FFFFF`: descriptor, GbE, Intel ME) and a 4 MiB chip
(`0x800000-0xBFFFFF`: coreboot). Each release has the complete image and the
two chip images; the lower 8 MiB are the same in every release.

- [Before you start](#before-you-start)
- [Boot loader fallback path](#boot-loader-fallback-path)
- [Serial number in VPD](#serial-number-in-vpd)
- [Internal update](#internal-update)
- [First boot and settings](#first-boot-and-settings)
- [External programmer](#external-programmer)
- [Recovery](#recovery)

## Before you start

- The laptop must already run coreboot with a writable flash, like the 4.22
  image in `legacy/` and every image from this repository (unlocked
  descriptor, `BOOTMEDIA_LOCK_NONE`). The Lenovo firmware does not allow it:
  the first flash needs an [external programmer](#external-programmer).
- AC adapter connected, battery charged.
- `flashrom` 1.2 or later (`sudo apt install flashrom`).
- Back up the whole flash, twice, and keep the copies off the laptop:

  ```sh
  sudo flashrom -p internal -r backup1.rom
  sudo flashrom -p internal -r backup2.rom
  cmp backup1.rom backup2.rom && echo identical
  ```

  If flashrom cannot map the flash (`/dev/mem` errors), boot once with
  `iomem=relaxed` on the kernel command line.
- Check the download: `sha256sum -c SHA256SUMS --ignore-missing`.

## Boot loader fallback path

Flashing also writes the empty `SMMSTORE` region of the image, so the UEFI
boot entries and the setup settings start from scratch. EDK2 then boots the
removable-media path of each disk, `\EFI\BOOT\BOOTX64.EFI`. Put your boot
loader there before flashing:

- Debian or Ubuntu with GRUB:

  ```sh
  echo 'grub-efi-amd64 grub2/force_efi_extra_removable boolean true' | sudo debconf-set-selections
  sudo grub-install --target=x86_64-efi --efi-directory=/boot/efi --removable
  ```

  The first line keeps the copy up to date when GRUB is updated.
- systemd-boot: `sudo bootctl install` installs the fallback copy too.

If you forgot: at power-on press Esc, open *Boot Maintenance Manager* →
*Boot From File* and pick the boot loader (for example
`\EFI\debian\grubx64.efi`), then recreate the entry from Linux with
`efibootmgr --create --disk /dev/nvme0n1 --part 1 --label debian --loader '\EFI\debian\grubx64.efi'`
(your disk and partition).

## Serial number in VPD

The firmware reads the SMBIOS serial number and BIOS version from the `RO_VPD`
region, as the 4.22 configuration did. The region is empty in the release
images. If you stored values there with the `vpd` tool, copy the region from
your backup into the image before flashing; `cbfstool` addresses it by name,
so a different offset does not matter:

```sh
cbfstool backup1.rom read -r RO_VPD -f ro_vpd.bin
tr -d '\377' < ro_vpd.bin | wc -c            # 0: empty, nothing to copy
cp w541-coreboot-v1.0.0-mrc.rom w541-flash.rom
cbfstool w541-flash.rom write -r RO_VPD -f ro_vpd.bin
```

Then flash `w541-flash.rom` (its checksum no longer matches `SHA256SUMS`).
`cbfstool` is `work/coreboot/build-mrc/cbfstool` after `tools/build.sh`, or
`make -C util/cbfstool` in any coreboot tree.

## Internal update

```sh
sudo flashrom -p internal --ifd -i bios -w w541-coreboot-v1.0.0-mrc.rom
```

`--ifd -i bios` writes only the BIOS region (`0x500000-0xBFFFFF`) and keeps
the descriptor, GbE and ME already on the machine: on another W541 those are
its own, with its own MAC address. flashrom must end with `VERIFIED`. If it
does not, do not power off: write the image (or your backup) again
straight away.

To switch between the `mrc` and `nri` variants, flash the other image the
same way.

## First boot and settings

- The first boot trains the memory and saves the result in `RW_MRC_CACHE`:
  a few seconds of black screen before the boot splash. It happens again
  after every flash, since the cache is empty in the image.
- All the LEDs blinking with a black screen mean that coreboot stopped on a
  fatal error (`H8_FLASH_LEDS_ON_DEATH`), for example a failed memory
  initialization: see [Recovery](#recovery).
- Settings: Esc at power-on (*Boot Options/Settings*), *Device Manager* →
  *Platform Setup Menu*:
  - *Graphics* → *NVIDIA discrete GPU*: **off by default**; when on, the
    driver can power the GPU down when idle;
  - *Processor*: *CPU PL1/PL2 power limit (W)* and *CPU power limit lock*;
  - *System*: *Intel Management Engine*, *Non-maskable Interrupts*,
    *Restore AC Power Loss*.

  Changes apply at the next boot.

## External programmer

Needed for the first flash over the Lenovo firmware, and for recovery. The
chips are SOIC-8 on the mainboard: the
[Libreboot W541 guide](https://libreboot.org/docs/install/w541_external.html)
and the Lenovo hardware maintenance manual show how to reach them.

- Battery and AC adapter disconnected.
- Keep the chip you are not flashing deselected: Libreboot recommends tying
  its pin 1 (chip select) to 3.3 V, ideally through a 47 Ω resistor.
- Read each chip twice and compare before writing. With a CH341A:

  ```sh
  sudo flashrom -p ch341a_spi -r chip-a.rom && sudo flashrom -p ch341a_spi -r chip-b.rom && cmp chip-a.rom chip-b.rom
  sudo flashrom -p ch341a_spi -w w541-coreboot-v1.0.0-mrc-8mb-chip.rom      # on the 8 MiB chip
  sudo flashrom -p ch341a_spi -w w541-coreboot-v1.0.0-mrc-4mb-chip.rom      # on the 4 MiB chip
  ```

  Add `-c <chip>` if flashrom lists several matching chips.

The `-8mb-chip.rom` image carries the descriptor, ME and MAC address of the
laptop the blobs come from. On any other W541 write only `-4mb-chip.rom` and
leave the 8 MiB chip as it is: its own descriptor and ME boot coreboot, and
the part of the old BIOS left in `0x500000-0x7FFFFF` is never used.

## Recovery

If the laptop does not boot after a flash, write the 4 MiB chip externally
with a working image: the `-4mb-chip.rom` of a previous release, or the last
4 MiB of your backup.

```sh
dd if=backup1.rom of=backup-4mb.rom bs=1M skip=8      # 0x800000-0xBFFFFF
dd if=backup1.rom of=backup-8mb.rom bs=1M count=8     # 0x000000-0x7FFFFF, if needed
```

The 8 MiB chip only needs writing if it was changed.
