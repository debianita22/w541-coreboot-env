# ThinkPad W541 coreboot

[![MRC release](https://img.shields.io/github/v/release/debianita22/w541-coreboot-env?include_prereleases&sort=semver&filter=*-mrc&style=flat-square&label=mrc)](https://github.com/debianita22/w541-coreboot-env/releases)
[![NRI release](https://img.shields.io/github/v/release/debianita22/w541-coreboot-env?include_prereleases&sort=semver&filter=*-nri&style=flat-square&label=nri)](https://github.com/debianita22/w541-coreboot-env/releases)
[![Check](https://img.shields.io/github/actions/workflow/status/debianita22/w541-coreboot-env/check.yml?branch=main&style=flat-square&label=check)](https://github.com/debianita22/w541-coreboot-env/actions/workflows/check.yml)
[![Build](https://img.shields.io/github/actions/workflow/status/debianita22/w541-coreboot-env/build.yml?style=flat-square&label=build)](https://github.com/debianita22/w541-coreboot-env/actions/workflows/build.yml)
[![License](https://img.shields.io/github/license/debianita22/w541-coreboot-env?style=flat-square)](LICENSE)

---

[coreboot](https://www.coreboot.org) firmware for the **Lenovo ThinkPad W541**
(Intel Haswell, Lynx Point QM87, NVIDIA Quadro K2100M), with the
[EDK2](https://github.com/mrchromebox/edk2) UEFI payload. GitHub Actions builds
it from a pinned upstream coreboot commit plus the patches in this repository
and publishes two complete flash images per version:

| Release | RAM initialization | Status |
|---|---|---|
| `vX.Y.Z-mrc` | Intel `mrc.bin`, the blob from the coreboot 24.08 image that ran on this W541 ([blobs/README.md](blobs/README.md)) | the conservative choice |
| `vX.Y.Z-nri` | coreboot's native RAM init (NRI), no `mrc.bin` | always a pre-release: upstream still labels it *[NOT COMPLETE]* ([Libreboot](https://libreboot.org/docs/install/w541_external.html) ships it on the W541) |

Everything else is the same in the two variants.

> [!WARNING]
> Flashing firmware can leave the laptop unable to boot. Keep a backup of the
> whole flash and have an external SPI programmer at hand before you start:
> see [docs/flashing.md](docs/flashing.md).

## What a release contains

| File | Use |
|---|---|
| `w541-coreboot-<version>-<variant>.rom` | complete 12 MiB flash image: descriptor, GbE, Intel ME and coreboot |
| `…-4mb-chip.rom` | last 4 MiB of the image: all of coreboot, for an external programmer on the 4 MiB chip |
| `…-8mb-chip.rom` | first 8 MiB of the image: descriptor, GbE and ME **of the machine the blobs come from**, for the 8 MiB chip |
| `….config` | the complete coreboot configuration |
| `…-layout.txt` | flash map (FMAP) and CBFS contents |
| `SHA256SUMS` | checksums |

The W541 flash is two chips seen as one 12 MiB space. The layout is
[`configs/w541.fmd`](configs/w541.fmd):

| Range | Chip | Contents |
|---|---|---|
| `0x000000-0x000FFF` | 8 MiB | Intel flash descriptor (IFD), all regions unlocked |
| `0x001000-0x002FFF` | 8 MiB | GbE, with the MAC address of the machine the blobs come from |
| `0x003000-0x4FFFFF` | 8 MiB | Intel ME 9.1, reduced with `me_cleaner -S` (ROMP and BUP only, AltMeDisable set) |
| `0x500000-0x597FFF` | 8 MiB | regions coreboot writes at runtime, empty in the image: `RW_MRC_CACHE` (RAM training), `SMMSTORE` (UEFI variables and settings), `RO_VPD`, `RW_ELOG` (event log) |
| `0x598000-0x7FFFFF` | 8 MiB | unused (`0xFF`) |
| `0x800000-0xBFFFFF` | 4 MiB | `FMAP` and `COREBOOT` (CBFS): all of coreboot |

The regions written at runtime sit on the 8 MiB chip, as in the coreboot
24.08 build the laptop runs today and in Libreboot's images for this board;
coreboot's default map would put them on the 4 MiB chip, where saving the
RAM training is not proven to work, and S3 resume depends on it. coreboot
itself lives alone on the 4 MiB chip, so `…-4mb-chip.rom` is a complete
coreboot for recovery with an external programmer.

The first 5 MiB are byte-identical to `legacy/coreboot-4.22/coreboot.rom`,
the image this project started from: `tools/verify-rom.sh` stops a release
otherwise. On a W541 that already runs coreboot, an update rewrites only the
BIOS region (`flashrom --ifd -i bios`), so the descriptor, the ME and the MAC
address on the machine stay as they are.

Inside coreboot:

- coreboot `26.09-53-g26317964960f` plus the [patch series](patches/), EDK2
  from MrChromebox pinned to a tested commit plus its own
  [patches](patches/README.md#edk2-and-lvglpkg), with the boot splash in
  `assets/`;
- libgfxinit for the Intel GPU (no Intel VBIOS is executed);
- the NVIDIA K2100M VBIOS, handed to the operating system through the ACPI
  `_ROM` method of the GPU, with runtime power management (the GPU is switched
  off when idle), its power state kept across suspend, and the Optimus key
  the Windows NVIDIA driver asks for (`blobs/opvk.inc`);
- CPU microcode from coreboot's `intel-microcode`, also referenced by the FIT;
- a setup menu laid out like System Preferences (Esc at power-on): a grid
  of icons on a dark desktop, one per category, then the boot entries; each
  category opens a window of settings grouped on cards, with switches and
  drop-down menus. The arrow keys, the TrackPoint and the touchpad move
  around:

  | Category | Settings |
  |---|---|
  | *General* | model, processor, memory and firmware versions; beeps and volume; NMI |
  | *Energy Saver* | battery charge thresholds (OS controlled, 100%, 80%, 60%), Intel SpeedStep, Turbo Boost, C-states, CPU PL1/PL2 limits, cooling policy (active or passive), USB always on, power-on after power failure |
  | *Security* | Intel VT-x, VT-d, Intel ME, supervisor password (asked for before the setup menu opens) |
  | *Keyboard* | Fn/Ctrl swap, F1-F12 or special keys, sticky Fn, keyboard backlight, TrackPoint, touchpad |
  | *Hardware* | NVIDIA GPU, graphics stolen memory (native RAM init only: `mrc.bin` reserves a fixed 32 MiB) and aperture, ExpressCard and Thunderbolt ports, Wi-Fi, Bluetooth, WWAN |

  Settings live in UEFI variables in the `SMMSTORE` flash region and apply
  at the next boot. XMP memory profiles are not supported: the memory runs
  at its JEDEC timings;
- an event log in the `RW_ELOG` flash region (boots, wake sources, entries
  into S3 and S5, power failures, watchdog resets), read with
  `tools/elog.py` or coreboot's `elogtool`, and POST codes kept in CMOS, so
  that the boot after a hang logs where it stopped
  ([docs/flashing.md](docs/flashing.md#diagnosing-a-hang));
- fixes for wake from suspend (lid, Fn), the Fn hotkeys, Bluetooth and WWAN
  state on resume, xHCI ports, USB over-current mapping, PCIe interrupts, the
  backlight, HDMI/DisplayPort audio clocks, the battery `_UID` and the AES-NI
  lock, plus three fixes to the native RAM init used by the `nri` variant.
  [patches/](patches/) lists them all.

> [!NOTE]
> The NVIDIA GPU is **disabled by default**, as in upstream coreboot. Turn it
> on in the setup menu: *Hardware* → *NVIDIA discrete GPU*.

> [!WARNING]
> Suspend to RAM (S3): with the `mrc` images up to v1.0.5 the laptop sleeps
> and does not come back, because that `mrc.bin` build hangs while
> restoring the memory on resume (POST code `0x3a`, ten lines into its
> log). From v1.0.6 the `mrc` image carries the `mrc.bin` of the 24.08
> image that resumed on this laptop. If a resume still stops,
> [docs/flashing.md](docs/flashing.md#diagnosing-a-hang) shows how to find
> where, and suspend-to-idle (`mem_sleep_default=s2idle`) works meanwhile.

## Flashing

On a W541 that already runs coreboot with an unlocked flash (as the 4.22 image
in `legacy/`), from Linux:

```sh
sha256sum -c SHA256SUMS --ignore-missing
sudo flashrom -p internal -r backup-$(date +%F).rom      # whole 12 MiB: keep it off the laptop
ls -l backup-*.rom                                        # must be 12582912 bytes
sudo flashrom -p internal --ifd -i bios -w w541-coreboot-v1.0.2-mrc.rom
```

The UEFI settings and boot entries start from scratch after flashing. Before
you reboot, read [docs/flashing.md](docs/flashing.md): boot loader fallback
path, serial number in VPD, external flashing and recovery.

## Building

Debian or Ubuntu:

```sh
sudo apt install build-essential bison flex gnat libncurses-dev zlib1g-dev m4 \
    curl git python3 uuid-dev imagemagick
git clone https://github.com/debianita22/w541-coreboot-env.git
cd w541-coreboot-env
tools/build.sh                      # both variants, into dist/
```

`tools/build.sh` fetches coreboot at the pinned commit into `work/coreboot`,
applies `patches/series`, copies the blobs (checked against their
`SHA256SUMS`), fetches MrChromebox's EDK2 at the commit of the defconfigs and
applies `patches/edk2` and `patches/lvglpkg`, builds the coreboot cross
toolchain the first time (30-60 minutes, then reused), builds each variant
and runs `tools/verify-rom.sh` on it. Useful options:

```sh
tools/build.sh --variant mrc                  # one variant
tools/build.sh --version v1.0.0-local         # version string and file names
tools/build.sh --with test-peg-afe roms       # plus a patch from patches/optional/
tools/build.sh --help
```

## Continuous integration and releases

| Workflow | When | What |
|---|---|---|
| [`check.yml`](.github/workflows/check.yml) | every push and pull request | `tools/ci-check.sh` (shellcheck, actionlint, blob checksums, patch series, defconfigs); the series applied to the pinned coreboot and EDK2, and both configurations checked |
| [`build.yml`](.github/workflows/build.yml) | tag `vX.Y.Z`, *Run workflow*, push to `ci-test/**` | both ROMs with the coreboot toolchain (cached), verified, published as `vX.Y.Z-mrc` and `vX.Y.Z-nri` |
| [`upstream.yml`](.github/workflows/upstream.yml) | every Monday | opens an issue when the series no longer applies on coreboot `main`, a patch was merged upstream, a coreboot release is out, or MrChromebox's EDK2 branch moved |

A release is made by pushing a version tag (`git tag v1.0.0 && git push origin
v1.0.0`) or with *Run workflow* and a version. Versions with a hyphen
(`v1.1.0-rc1`) or the *prerelease* box give two pre-releases. A pre-release
that works on the laptop becomes a release without rebuilding it:

```sh
gh release edit v1.0.0-mrc --repo debianita22/w541-coreboot-env --prerelease=false --latest
```

## Repository layout

| Path | Contents |
|---|---|
| `blobs/` | IFD, GbE, ME, `mrc.bin` and VBIOS images ([blobs/README.md](blobs/README.md)) |
| `assets/` | the EDK2 boot splash |
| `configs/` | `w541-mrc.defconfig` and `w541-nri.defconfig` |
| `patches/` | the series applied to coreboot, optional patches, and the patches to EDK2 and its LvglPkg ([patches/README.md](patches/README.md)) |
| `tools/` | build, verification, CI and upstream-check scripts, and `elog.py`, which reads the event log from a flash dump |
| `legacy/coreboot-4.22/` | the coreboot 4.22 image and configuration this project started from, built from the same blobs |
| `docs/` | [flashing and recovery](docs/flashing.md) |

## Binary blobs

The images contain proprietary binaries that are not covered by the GPL: the
Intel flash descriptor, ME firmware and `mrc.bin`, the GbE configuration and
the NVIDIA VBIOS and the NVIDIA Optimus key (`blobs/opvk.inc`, from the
Lenovo DSDT). They are published here by the owner of the laptop they come
from; their redistribution terms are those of Intel, Lenovo and NVIDIA.

## License

The scripts, configurations and patches are GPL-2.0, like coreboot
([LICENSE](LICENSE)). The binary blobs keep their own licenses (see above).
