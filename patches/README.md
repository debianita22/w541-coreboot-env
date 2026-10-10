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
| `lvglpkg/0001-0003` | a 1.25x UI scale, a PCD for the default scale, the stock LVGL widgets styled from `LvglTheme.h` |
| `lvglpkg/0004` | the setup UI redesigned as System Preferences: menu bar, icon grid on the front page, windows with cards, switches and drop-down menus |
| `lvglpkg/0005` | password questions in dialogs (current, new, confirmation), as the text UI does |
| `lvglpkg/0006` | the cursor moved by relative pointers (PS/2 TrackPoint and touchpad) |

## Optional patches

`optional/` holds patches that never go into a release. `tools/build.sh --with
NAME` applies them after the series and adds `+NAME` to the version and the
file names; `tools/verify-rom.sh --release` rejects such an image.

| Patch | What | Status |
|---|---|---|
| `test-peg-afe` | programs the PEG PHY (AFE) recipe when neither MRC nor the native RAM init does, to compare dGPU link speed and AER errors | test: runs on every boot, whatever the NVIDIA GPU setting |
| `test-charge-behaviour` | ACPI charge behaviour (inhibit charge, force discharge) on the W541, as on the T440p | test: the EC registers were only verified on the T440p |
| `local-optimus-key` | returns the NVIDIA Optimus key that the Windows driver asks for, from a local `src/mainboard/lenovo/haswell/acpi/opvk.inc` | local only: the key belongs to NVIDIA, extract it from your own OEM firmware; never commit it |

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
