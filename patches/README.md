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

Then `tools/build.sh prepare config` checks that the series applies and that
both configurations keep every option. `tools/ci-check.sh` checks that
`series` and the files agree.
