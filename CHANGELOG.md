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
- The version in the ESRT and in the capsules is four bytes, X.Y.Z.R
  (fwupd's *quad* format): a release is X.Y.Z.0, its candidate N the
  release before with R = N (v1.3.7-rc2 is 1.3.6.2). Before, the
  candidates had the number of their release, so fwupd saw one version and
  offered neither a later rc nor the release after an rc. The setup page and
  `tools/update.py` show the tag (v1.3.7-rc2), `fwupdmgr` the four numbers;
  a W541 on v1.3.6-rc1 or older (X.Y.Z in the old layout, read as 1.3.0.6)
  updates to any of the new numbers as usual
  ([docs/update.md](docs/update.md#versions-variants-downgrades)), and the
  setup page and `tools/update.py` still show the old numbers by their tag
  (v1.3.5, not v1.3.1-rc5); `tools/update.py` still reads the `.cab` of
  those releases (format *triplet*). `tools/ci-check.sh` refuses a lowest
  supported version between v1.3.0 and v1.3.6, where the two layouts
  overlap. The *Go back to* of the setup and coreboot's check behind it
  (patches `edk2/0020` and 0065) read the version from the release name in
  the other slot in the same layout, `-rcN` included: in the old one, once
  the lowest supported version is raised, they would have refused a newer
  release (v1.3.7 read as `0x01030007`, below v1.3.6). The update test in
  QEMU numbers its ROMs so that a parser left on the old layout fails it.
- A testing remote for fwupd, `configs/fwupd/ourobios-testing.conf`, off
  until turned on (`fwupdmgr enable-remote ourobios-testing`): the metadata
  of the latest release, candidates included, from the assets of the fixed
  `testing` entry of the releases page, which every release replaces when
  it is the highest so far ([docs/update.md](docs/update.md#the-fwupd-remote)).
- Network boot: *iPXE Network Boot* in the boot menu (iPXE 2025.12 with its
  Intel driver, for the I217 of the W541, and nothing of EDK2's network
  stack: patches 0075 and `edk2/0022`), at the end of the boot order. PXE,
  HTTP and iPXE scripts; Ctrl-B at the banner for its shell.
- Legacy BIOS boot: *CSMWrap Legacy Boot* in the boot menu
  ([CSMWrap](https://github.com/CSMWrap/CSMWrap), the CSM and VGA BIOS of
  SeaBIOS as a UEFI application, built by `tools/build.sh prepare`:
  patches 0076 and `edk2/0023`), for what does not boot from UEFI, from
  SATA, NVMe and USB drives through SeaBIOS's own drivers; one logical
  processor stays with CSMWrap. While Secure Boot is on the entry goes,
  and the firmware refuses to start CSMWrap even from a `Boot####`,
  `BootNext` or `SysPrep####` written from the OS (a file of the firmware
  volume is not checked against the Secure Boot keys: patch `edk2/0025`,
  after v1.3.6-rc2).
- *General* → *Boot* in the setup turns each of the two off (patch 0077).
  The smoke test in QEMU starts both from the boot manager: iPXE gets its
  address from QEMU's DHCP, downloads a script over TFTP and runs it;
  CSMWrap boots the master boot record of a SATA disk. The payload's
  image in memory grows to 11 MiB (patch `edk2/0024`); each flash slot
  keeps about 215 KiB free with `mrc` and 148 KiB with `nri` (about 510
  and 443 before).
- A screen of its own for the firmware update (patch `edk2/0026`, after
  v1.3.6-rc3), instead of a white bar under the logo: the ouroboros of the
  logo lights up from its head around to its tail as the progress, and the
  bar under it is a map of the flash region being written, one column per
  4 KiB block (rewritten, already as in the capsule, still to do, the one
  being written). Around them the versions and the vboot slots, what is
  being done, the percentage, how much was rewritten and how much was
  already there, the time left, and what not to do; at the end the snake
  winks and the screen counts down to the reset; after a failure it shows
  the block that failed and that the slot in use was not touched.
  FmpDeviceSmmLib publishes what it writes for the screen; the text is
  Montserrat, as in the setup, which `tools/ui-font.py` turns into a compact
  font from the files of LvglPkg when the tree is prepared. The firmware
  that is running draws it: it shows from the update after the one that
  installs it. It takes about 18 KiB of each flash slot, measured between
  v1.3.6-rc3 and rc4: about 195 KiB stay free with `mrc` and 129 KiB with
  `nri`.
- Reproducible builds: the release build of each variant is built a second
  time from scratch in CI, CSMWrap and iPXE included, and compared bit by
  bit with the first (`tools/build.sh repro`). The build date was already
  the date of the commit; the payload now has no debug directory in its PE
  images (patch `edk2/0021`, `GenFw --zero`), whose CodeView entries named
  the `.dll` of each module with the absolute path of the build, 116 of
  them. With the same compilers (coreboot's cross toolchain; GCC and
  binutils of Ubuntu 24.04 for the payload, iPXE and CSMWrap, named in the
  `-layout.txt` of the releases after v1.3.6-rc2) the stages and the
  payload of a release can be rebuilt from its tag and compared; the whole
  ROM also needs the project's vboot key, which signs the slots.

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
