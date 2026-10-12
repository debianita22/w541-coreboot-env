# Changelog

What changes from one release to the next. The notes of each release on
GitHub include its section; release candidates take the section of the
version they lead to. For the releases before v1.3.0, see their notes on
the [releases page](https://github.com/debianita22/ourobios/releases).

## v1.3.2

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
