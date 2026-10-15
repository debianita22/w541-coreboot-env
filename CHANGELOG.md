# Changelog

What changes from one release to the next. The notes of each release on
GitHub include its section; release candidates take the section of the
version they lead to. For the releases before v1.3.0, see their notes on
the [releases page](https://github.com/debianita22/ourobios/releases).

## v1.3.6

- coreboot and EDK2 moved to their upstream of October 2026 (coreboot
  `26.09-280-gacdbfd47295d`, 227 commits after the pin of v1.3.0; EDK2
  `9c0cb3a58343`). All the patches apply as they are. The read-only part
  on the laptops stays the one of v1.3.0: the slots of the new pin still
  read what it leaves in cache-as-RAM (vboot's shared data and nvdata, the
  early console, the timestamps, the FMAP and CBFS caches: same structs,
  same addresses), checked by hand and recorded as a `rw-ok` line in
  `configs/ro-interface.txt`, which `tools/ci-check.sh` and
  `tools/verify-rom.sh` accept. The update test in QEMU and the two
  variants pass on the new pin.

## v1.3.5

- No more `thinkpad_acpi: acpi_evalf(\BLTH, vd, ...) failed: AE_NOT_FOUND`
  (and `\WGSV`) on the console at every shutdown and reboot: the two
  methods the driver calls to save the Bluetooth and WWAN state for S4/S5
  are in the DSDT now (patch 0067).
- A clean kernel log: no more *ACPI Warning: SystemIO range ... conflicts
  with OpRegion ... (\_SB.PCI0.GPLV)*, three times at every boot (the
  GPIO methods of Lynx Point's ASL, which no board uses, are gone: patch
  0068), and no more *ACPI: \: failed to evaluate _DSM ... (0x1001)* from
  iwlwifi (the cards behind the root ports have a device node, as on the
  OEM firmware: patch 0069).
- The USB ports described as the OEM firmware does (`_UPC`, `_PLD`): the
  OS knows which port is a connector and on which side, which carries the
  camera or the fingerprint reader, and which has nothing; the port devices
  of the Lynx Point-H xHCI were those of the -LP chipset (patches 0070,
  0071).
- Wake-on-LAN is possible: the Ethernet controller has its ACPI device
  with `_PRW`, and the Wi-Fi and WWAN root ports theirs, as on the OEM
  firmware (patch 0072). Nothing wakes the laptop unless the OS enables
  it (`ethtool -s enp0s25 wol g`).
- The battery trip point (`_BTP`, the `alarm` attribute of the battery in
  Linux), as on the OEM firmware (patch 0073).
- `_S3D` on the devices and `_RMV` on the WWAN and ExpressCard slots, as
  on the OEM firmware (patch 0074).

## v1.3.4

- Going back without an OS: the *Firmware Update* page of the setup shows
  the slot that runs and the release in the other one, and *Go back to*
  restarts into that release, which vboot tries at the next two boots and
  leaves if it does not boot, as `update.py rollback` does. The setup
  checks what a capsule would (not the release that runs, the same
  variant, not older than the oldest accepted update), and only the setup
  can do it: the payload locks the switch before it starts a boot option
  (an OS loader, the UEFI Shell) and at `ExitBootServices()`, coreboot on
  resume from suspend (patches 0065, `edk2/0020` and `lvglpkg/0015`,
  [docs/update.md](docs/update.md#from-the-setup)). What runs before the
  lock (a `Driver####` or `SysPrep####` option, an option ROM) gets the
  same checks from coreboot itself: the variant, and not older than the
  lowest supported version of the running release.
- An fwupd remote: the mrc release carries the metadata of both variants,
  signed by the capsule key (`ourobios.xml.gz`, `.jcat`), and
  `configs/fwupd/ourobios.conf` downloads it from the latest release, so
  that `fwupdmgr update`, GNOME Software and KDE Discover offer the
  releases (not the release candidates). It needs the project certificate
  in `/etc/pki/fwupd`, as the packages already did
  ([docs/update.md](docs/update.md#the-fwupd-remote)).
- The battery charge behaviour from Linux on the W541 too
  (`/sys/class/power_supply/BAT0/charge_behaviour`: `inhibit-charge`,
  `force-discharge`), as on the T440p (patch 0066, until v1.3.3 the
  optional `test-charge-behaviour`): the Lenovo ACPI tables of the two
  laptops declare the same embedded controller registers.
- The update test in QEMU goes back from the setup too, with the keys sent
  by QEMU, and checks that a boot option cannot switch slot, before or after
  `ExitBootServices()` (`tools/efi/slot-lock-test.c`). It also fills
  `RW_NVRAM` before a capsule, cuts the power halfway through a slot (vboot
  stays on the old one, the capsule staged again finishes it) and applies a
  capsule from a USB drive.

## v1.3.3

- EDK2 at MrChromebox's `0d39245e6`: writing a Secure Boot key from
  Linux (`sbctl enroll-keys`, `efi-updatevar`) no longer faults in the
  variable driver, which verifies authenticated variables outside SMM,
  and no longer takes the EFI runtime services down (CryptoPkg,
  `RuntimeFreeMem()`). The three commits before it load an Intel GOP
  driver and VBT from CBFS, which this firmware does not use: libgfxinit
  draws the screen.
- v1.3.3-rc1 is the same firmware with another version, tested on the
  W541: the capsule update from v1.3.2, suspend and resume. From it:
  `sudo python3 update.py stage --latest`.
- The smoke test of the build also checks the UEFI variables: in two
  boots of the emulated laptop the UEFI Shell writes a test variable, and
  it must be in SMMSTORE, then rewritten with the first copy deleted.
- The build tests the capsule update in QEMU before a release
  (`tools/build.sh update-test`, `tools/qemu-update.py`, a job beside the
  ROMs): three q35 images with vboot's two RW slots and its nvdata in flash,
  the EDK2 payload of these patches and capsules made as those of the
  releases. Each capsule must write only the slot that is not running;
  vboot tries it and the payload confirms it; the capsule of the release
  already in the other slot (`update.py rollback`) writes nothing to the
  slots; vboot goes back to the old slot when the new one does not verify
  or does not reach the payload; the ESRT is checked at every boot
  ([docs/vboot.md](docs/vboot.md#how-it-is-tested)).
- `tools/update.py status` adds a note when the version that runs is not
  the one of the last capsule, written with success: vboot went back to the
  other slot, or the flash was rewritten afterwards.

## v1.3.2

- The firmware is built from the same coreboot, EDK2, patches and blobs
  as v1.3.1; only its version changes. It takes a laptop on v1.3.2-rc1,
  which also had patch 0064, back to that code:
  `sudo python3 update.py stage --latest`.
- `tools/idle-report.py`: package and core C-states, RAPL and battery
  power and memory traffic at idle, and the power state of the devices
  that can keep the processor awake: ASPM, LTR and runtime PM of each PCI
  device, SATA link power management, USB autosuspend, graphics
  ([docs/flashing.md](docs/flashing.md#power-at-idle)).
- `tools/pm-regs.py`: the power management registers of processor,
  System Agent and PCH, as the firmware left them.
- `tools/update.py stage` goes from a release candidate to its release,
  or to a later candidate, without `--allow-older`.
- `tools/update.py rollback FLASH.bin`: back to the release in the slot
  that is not running, the one before the last update, with its signed
  capsule; the firmware rewrites nothing and only switches slot
  ([docs/update.md](docs/update.md#going-back)).
- The build boots the EDK2 payload of each variant in QEMU before a
  release (`tools/build.sh smoke`, `tools/qemu-smoke.py`): it must open
  the setup and answer the keys.
- Docs: the boot menu timeout is set in the setup menu, *Boot Manager* →
  *Auto Boot Time-out*; NVIDIA's own driver never powers this GPU down;
  where the boot time goes; a SATA link that never sleeps keeps the
  processor out of the package C-states below PC2, about 7 W at idle
  ([docs/flashing.md](docs/flashing.md#power-at-idle)). v1.3.2-rc1 also
  asked for the maximum ratio of the processor before `mrc.bin` (patch
  0064): the processor stayed at 800 MHz, and the patch is gone.

## v1.3.1

- VT-d: the remapping engine of the devices other than the graphics now
  handles the 2 MiB and 1 GiB pages it reports (patch 0063). Up to
  v1.3.0, with `intel_iommu=on`, Linux 7.2 used them for SATA transfers,
  which faulted and read back zeros (`csum failed` on btrfs); with those
  releases use `intel_iommu=on,sp_off` or `iommu=pt`.
- TPM 1.2: one TCPA table, coreboot's, with the events EDK2 measures
  (patch edk2/0019), so Linux reads the whole event log.
- The build checks that the RW slots still fit the read-only part that
  is already on the laptops.

## v1.3.0

- Verified boot with two RW slots: a capsule writes the slot that is not
  running and tries it, and vboot goes back to the other one if the new
  one does not verify or does not boot ([docs/vboot.md](docs/vboot.md)).
  *Firmware protection* (*Security*) write-protects the read-only part at
  every boot. From v1.2.x the move is one write with flashrom.
- The firmware is named ouroBIOS, with its logo in the setup menu and on
  the boot splash.
- The ESRT reports the result of the last update (patch edk2/0018).
- verstage reads the real sleep state (patch 0058): before, it could take
  a cold boot for a resume from S3 and keep the slot of the last boot
  instead of trying a new one.
- With *Intel Management Engine* disabled, the ME's PCI interface is
  hidden also when the ME is reduced by me_cleaner (patch 0059); the HD
  audio of the NVIDIA GPU gets its interrupt (patch 0060); the flash size
  is read right from the descriptor (patches 0061-0062).
- `tools/update.py`: `vboot`, `vboot-work` and `vars`.
