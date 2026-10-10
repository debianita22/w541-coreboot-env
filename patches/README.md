# Patches

`series` lists the patches that `tools/build.sh` applies with `git am`, in
order, to coreboot at the commit pinned in `tools/build.sh`
(`COREBOOT_COMMIT`). Every file is a `git format-patch` mail; they are written
for upstream review (one change each, with the reasoning in the message).

| Patches | What |
|---|---|
| 0001-0003 | NVIDIA dGPU of the W541: shared `mainboard.c`, runtime power management, power state kept across S3 |
| 0004-0007 | setup menu (CFR) for the Haswell ThinkPads, with fixes to the OC mailbox driver and to the Intel ME option default |
| 0008-0010 | Haswell native RAM init: sense amplifier offsets, write leveling offset, reset when the S3 fast path fails |
| 0011-0012 | wake from S3 on lid open and Fn |
| 0013-0022 | PCIe interrupt routing, ACPI backlight, Bluetooth and WWAN state on resume, Fn hotkeys, xHCI MaxPorts, USB over-current mapping, AES-NI lock, battery `_UID`, Mini-HD audio clock dividers |
| 0023-0024 | Kconfig fixes this configuration needs: EDK2 serial console only with a UART driver, NVIDIA VBIOS without an Intel VBIOS |
| 0025-0029 | options for the Haswell CPU and northbridge: graphics aperture (also with `mrc.bin`, reprogrammed after raminit), graphics stolen memory (native RAM init), SpeedStep, Turbo Boost, C-states, VT-x (a change applies through a full reset), VT-d |
| 0030-0033 | options for the Haswell ThinkPads: cooling policy (active trip point from GNVS), battery charge threshold presets, ExpressCard and Thunderbolt ports on the W541 |
| 0034-0035 | the setup menu in categories (General, Energy Saver, Security, Keyboard, Hardware), with the H8 options placed by the mainboard |
| 0036 | the NVIDIA Optimus key for the Windows driver (NVOP function 0x10), included in the DSDT from `blobs/opvk.inc`, which `tools/build.sh` copies into the tree |
| 0037 | no "Null dereference" error when the DRAM clearing at boot writes the first page, which starts at address zero |
| 0038 | no S3 resume attempt after a power button override, which leaves the S3 sleep type behind |
| 0039-0042 | where a boot or an S3 resume stopped, with `CMOS_POST`: a POST code as soon as the CMOS bank of a boot is chosen, one after each romstage step, the number of lines `mrc.bin` has printed, codes around loading ramstage |
| 0043 | S3 resume: the cache-as-RAM region is filled with the code uncached again, as before coreboot commit `97dbfd309`; with the code cached, one 64-byte line of the 256 KiB region (the size of the Haswell L2) is lost, and `mrc.bin` hung on it while restoring the memory on resume |
| 0044 | with the BIOS lock on, the event log written from SMM too (S3/S5 entry, GSMI, power button), with InSMM.STS and BIOSWE set around the writes as for SMMSTORE |
| 0045-0046 | Security options: *BIOS Lock* (`bios_lock` of `BOOTMEDIA_SMM_BWP_RUNTIME_OPTION`: only SMM writes the flash, off by default) and *Clear memory at power-on* (`clear_dram_on_boot`, on by default; 1.7 s with 32 GB) |
| 0047 | the machine's data from the VPD as the OEM firmware reports it: the machine type as the base board product too, the system serial on the chassis; machine type, serial and UUID in the About card of the setup menu |

## EDK2 and LvglPkg

`edk2/` and `lvglpkg/` hold the patches to the payload: `edk2/series` on
MrChromebox's EDK2 at the commit of the defconfigs
(`CONFIG_EDK2_TAG_OR_REV`), `lvglpkg/series` on its `LvglPkg` submodule (the
graphical setup UI) at the commit that EDK2 pins. `tools/build.sh prepare`
fetches EDK2 into the coreboot tree, where the payload build expects it, and
applies both series as changes, not commits: coreboot's EDK2 Makefile resets
a clean tree to the pinned commit but leaves a modified one alone.

| Patches | What |
|---|---|
| `edk2/0001-0002` | the setup menu (CFR) in categories, each its own form, listed on the front page; booleans as check boxes |
| `edk2/0003` | a link to the supervisor password (UserAuthenticationDxe) at the end of the *Security* category |
| `edk2/0004` | the PS/2 mouse on the SIO bus, so that Ps2MouseDxe drives the TrackPoint and the touchpad |
| `edk2/0005` | the setup UI named *System Preferences* |
| `edk2/0006` | the messages of the password driver (UserAuthenticationDxe) as HII popups, so that the graphical UI draws them |
| `edk2/0007` | Ps2MouseDxe at 100 reports per second and 8 counts/mm, every waiting packet taken at each poll, packets with the middle button pressed recognized |
| `lvglpkg/0001-0003` | a 1.25x UI scale, a PCD for the default scale, the stock LVGL widgets styled from `LvglTheme.h` |
| `lvglpkg/0004` | the setup UI redesigned as System Preferences: menu bar, icon grid on the front page, windows with cards, switches and drop-down menus |
| `lvglpkg/0005` | password questions in dialogs (current, new, confirmation), as the text UI does |
| `lvglpkg/0006` | the cursor moved by relative pointers (PS/2 TrackPoint and touchpad) |
| `lvglpkg/0007-0010` | fixes from the review of the UI: UTF-8 text fields, keys kept from the form while a popup is up, the previous screen deleted, information popups titled as such |
| `lvglpkg/0011` | a dynamic wallpaper (sky gradient and soft orbs of color, in a palette that follows the hour), the menu bar and the dock translucent and blurred over it, softer shadows |
| `lvglpkg/0012` | the cursor moved by the speed of the pointer: 12 px/mm when slow, up to 3x when fast, the same move counted the same whether it arrives in one read or several |

## Optional patches

`optional/` holds patches that never go into a release. `tools/build.sh --with
NAME` applies them after the series and adds `+NAME` to the version and the
file names; `tools/verify-rom.sh --release` rejects such an image.

| Patch | What | Status |
|---|---|---|
| `test-peg-afe` | programs the PEG PHY (AFE) recipe when neither MRC nor the native RAM init does, to compare dGPU link speed and AER errors | test: runs on every boot, whatever the NVIDIA GPU setting |
| `test-charge-behaviour` | ACPI charge behaviour (inhibit charge, force discharge) on the W541, as on the T440p | test: the EC registers were only verified on the T440p |

## Updating the series

After a change in a coreboot tree based on `COREBOOT_COMMIT`:

```sh
git format-patch --zero-commit -o /path/to/w541-coreboot-env/patches/ <COREBOOT_COMMIT>..HEAD
ls /path/to/w541-coreboot-env/patches/0*.patch | xargs -n1 basename > /path/to/w541-coreboot-env/patches/series
```

The same for EDK2 (a tree at `CONFIG_EDK2_TAG_OR_REV`) and for its LvglPkg
submodule (at the commit EDK2 pins), into `edk2/` and `lvglpkg/`, each with
its own `series`. Leave out `MdeModulePkg/Logo/Logo.bmp`, which the payload
build overwrites with the boot splash.

Then `tools/build.sh prepare config` checks that the series apply and that
both configurations keep every option. `tools/ci-check.sh` checks that each
`series` and its files agree.
