# Blobs

Binary files from the Lenovo firmware of a ThinkPad W541 (the one the 4.22
image in `legacy/` was built for; its MAC address is below). `SHA256SUMS`
pins them: `tools/build.sh` and `tools/ci-check.sh` stop if a file changes.
They are proprietary (Intel, Lenovo, NVIDIA), not covered by the GPL of the
rest of the repository.

| File | Size | What | In the release images |
|---|---|---|---|
| `ifd.bin` | 4 KiB | Intel flash descriptor: GbE `0x1000`, ME `0x3000-0x4FFFFF`, BIOS `0x500000-0xBFFFFF` | yes, with all regions unlocked, CPU read access to the ME region and the AltMeDisable strap set by `me_cleaner -S` |
| `gbe.bin` | 8 KiB | GbE configuration, MAC address `54:ee:75:5c:68:de` | yes, unchanged, at `0x1000` |
| `me.bin` | 5108 KiB | Intel ME firmware 9.1.32.1002, complete | yes, reduced by `me_cleaner -S` to ROMP and BUP |
| `mrc.bin` | 186 KiB | Haswell memory reference code, the same as in the 4.22 image | `mrc` variant only, at `0xFFFA0000` |
| `vbios_10de_11fc_1.rom` | 94.5 KiB | NVIDIA Quadro K2100M VBIOS (PCI `10de:11fc`) | yes, as `pci10de,11fc.rom`, handed to the OS through ACPI `_ROM` |
| `vbios_8086_0406_1.rom` | 64 KiB | Intel HD Graphics 4600 VBIOS (PCI `8086:0406`), modified | no: libgfxinit initializes the Intel GPU |
| `vbios_8086_0416_1.rom` | 64 KiB | Intel VBIOS with PCI ID `8086:0416`; its checksum byte is wrong | no |
| `opvk.inc` | 230 bytes | NVIDIA Optimus key: the `OPVK` buffer that method `GOBT` of the Lenovo DSDT returns, as comma-separated bytes | yes, in the DSDT (patch 0036): the Windows NVIDIA driver asks for it through NVOP function 0x10 and keeps Optimus off without it; Linux does not need it |

The descriptor, GbE and ME regions that the build produces from these files
are byte-identical to the first 5 MiB of `legacy/coreboot-4.22/coreboot.rom`;
`tools/verify-rom.sh` checks it on every build. Another W541 has its own
descriptor, GbE (MAC address) and ME version: an update with `flashrom --ifd
-i bios` leaves them alone, and `docs/flashing.md` shows how to build the
8 MiB chip image from your own backup for an external programmer.
