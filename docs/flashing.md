# Flashing and recovery

The W541 has two SPI flash chips that the chipset sees as one 12 MiB space:
an 8 MiB chip (`0x000000-0x7FFFFF`) and a 4 MiB chip (`0x800000-0xBFFFFF`).
The images use the map in [`configs/w541.fmd`](../configs/w541.fmd):

| Range | Chip | Contents |
|---|---|---|
| `0x000000-0x4FFFFF` | 8 MiB | descriptor, GbE, Intel ME: the same in every release, from `blobs/` |
| `0x500000-0x593FFF` | 8 MiB | `RW_MRC_CACHE`, `SMMSTORE`, `RO_VPD`: written by coreboot at runtime, empty in the image |
| `0x594000-0x7FFFFF` | 8 MiB | unused |
| `0x800000-0xBFFFFF` | 4 MiB | `FMAP` and CBFS: all of coreboot |

Each release has the complete image and the two chip images.

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
- `flashrom` (`sudo apt install flashrom`). With two chips it switches to
  hardware sequencing by itself and reads and writes all 12 MiB.
- Back up the whole flash, twice, and keep the copies off the laptop:

  ```sh
  sudo flashrom -p internal -r backup1.rom
  sudo flashrom -p internal -r backup2.rom
  cmp backup1.rom backup2.rom && echo identical
  ls -l backup1.rom                            # 12582912 bytes
  ```

  A backup of 8388608 bytes covers only the first chip: flashrom used
  software sequencing. Read again with
  `sudo flashrom -p internal:ich_spi_mode=hwseq -r backup1.rom`, and use the
  same `-p` for every command below.
- If flashrom cannot map the flash (`/dev/mem` errors), boot once with
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
- Arch-based systems with GRUB: `sudo grub-install --target=x86_64-efi
  --efi-directory=/boot/efi --removable` (or `/boot`, where the ESP is
  mounted: `findmnt -t vfat`).
- systemd-boot: `sudo bootctl install` installs the fallback copy too.

If you forgot: at power-on press Esc, open *Boot Manager* → *Boot From
File* and pick the boot loader (for example `\EFI\cachyos\grubx64.efi`),
then recreate the entry from Linux with
`efibootmgr --create --disk /dev/sda --part 1 --label cachyos --loader '\EFI\cachyos\grubx64.efi'`
(your disk, partition and path).

## Serial number in VPD

The firmware reads the SMBIOS serial number and BIOS version from the `RO_VPD`
region, as the 4.22 configuration did. The region is empty in the release
images. If you stored values there with the `vpd` tool, copy the region from
your backup into the image before flashing; `cbfstool` addresses it by name,
so a different offset does not matter:

```sh
cbfstool backup1.rom read -r RO_VPD -f ro_vpd.bin
tr -d '\377' < ro_vpd.bin | wc -c            # 0: empty, nothing to copy
cp w541-coreboot-v1.0.2-mrc.rom w541-flash.rom
cbfstool w541-flash.rom write -r RO_VPD -f ro_vpd.bin
```

Then flash `w541-flash.rom` (its checksum no longer matches `SHA256SUMS`).
`cbfstool` is `work/coreboot/build-mrc/cbfstool` after `tools/build.sh`, or
`make -C util/cbfstool` in any coreboot tree.

## Internal update

```sh
sudo flashrom -p internal --ifd -i bios -w w541-coreboot-v1.0.2-mrc.rom
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
  after every flash, since the cache is empty in the image. Suspend (S3)
  needs that saved training: test it after the second boot.
- All the LEDs blinking with a black screen mean that coreboot stopped on a
  fatal error (`H8_FLASH_LEDS_ON_DEATH`), for example a failed memory
  initialization: see [Recovery](#recovery).
- Settings: Esc at power-on opens the setup menu, a grid of icons. Arrow
  keys, Enter and Esc, or the TrackPoint and touchpad, move around; F10
  saves, F9 loads the defaults of every category, not only the open one.
  - *Hardware* → *NVIDIA discrete GPU*: **off by default**; when on, the
    driver can power the GPU down when idle. *Hardware* also has the
    graphics aperture and, with the native RAM init (`nri`), the graphics
    stolen memory (DVMT), the ExpressCard and Thunderbolt ports and the
    radios;
  - *Energy Saver*: battery charge thresholds (*OS controlled* keeps what
    TLP or `thinkpad_acpi` sets), SpeedStep, Turbo Boost, C-states, CPU
    PL1/PL2 limits and lock, cooling policy, *Restore AC Power Loss*;
  - *Security*: VT-x, VT-d, *Intel Management Engine* and the supervisor
    password. The processor keeps the VT-x setting until it is powered
    off, so after a change the next boot switches the laptop off and on
    once by itself. With a password set, Esc at power-on asks for it
    before the setup menu, boot entries included, opens; without Esc the
    laptop boots as usual. A forgotten password goes away when the image
    is flashed again from Linux, since that also writes the empty
    `SMMSTORE` region: every setting and boot entry resets too;
  - *Keyboard*: Fn/Ctrl swap, F1-F12 or special keys, TrackPoint and
    touchpad;
  - *General*: what the laptop is (model, processor, memory, firmware and
    EC versions), beeps, *Non-maskable Interrupts*.

  Changes apply at the next boot. Graphics stolen memory and aperture
  change the memory map: do not change them between suspend and resume.

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
  sudo flashrom -p ch341a_spi -w w541-coreboot-v1.0.2-mrc-4mb-chip.rom      # on the 4 MiB chip
  ```

  Add `-c <chip>` if flashrom lists several matching chips.

`-4mb-chip.rom` is all of coreboot. It boots with an 8 MiB chip that
already carries a coreboot image for this layout, from this repository or
from the builds in `legacy/`: the runtime regions on that chip may hold
anything, coreboot and EDK2 check them and start over.

`-8mb-chip.rom` carries the descriptor, the ME and the **MAC address of the
machine the blobs come from**. Write it only to a chip you cannot keep
(corrupted, or a Lenovo descriptor you want replaced by the unlocked one),
knowing that the laptop takes that MAC address; otherwise build the 8 MiB
image from your own backup, which keeps your descriptor, ME and MAC:

```sh
dd if=backup1.rom of=my-8mb-chip.rom bs=1M count=5        # your IFD, GbE, ME
dd if=w541-coreboot-v1.0.2-mrc.rom bs=1M skip=5 count=3 >> my-8mb-chip.rom
```

On the Lenovo firmware the descriptor is locked and the ME is complete:
converting a laptop needs its own backup processed as in `legacy/`
(`ifdtool -u`, `me_cleaner -S`), which this repository does not do.

## Recovery

If the laptop does not boot after a flash, write the 4 MiB chip externally
with a working image: the `-4mb-chip.rom` of a previous release, or the last
4 MiB of your backup. The 8 MiB chip needs writing only if it was changed
(see above for an image with your own descriptor and MAC address).

```sh
dd if=backup1.rom of=backup-4mb.rom bs=1M skip=8      # 0x800000-0xBFFFFF
dd if=backup1.rom of=backup-8mb.rom bs=1M count=8     # 0x000000-0x7FFFFF, if needed
```

With the `nri` variant, a failed RAM training stops with the LEDs blinking:
try again with two modules (one per channel), then recover as above.
