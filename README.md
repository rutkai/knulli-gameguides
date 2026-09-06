# KNULLI Game Guides — for the TrimUI Brick

A port of the ROCKNIX [Game Guides](https://rocknix.org/configure/gameguides/)
feature to KNULLI. Press **MENU + SELECT** during a game and a GameFAQs-style
text walkthrough covers the screen; press **A** or **B** and you are back in
the game, at the same frame, with your place in the guide remembered.

Everything installs into the **SHARE / userdata partition only**, so a KNULLI
update cannot remove it and **no boot overlay is needed**. See
[Why no overlay](#why-no-overlay) below.

```
.
├── install.sh                  run this
├── README.md                   you are here
├── LICENSE                     MIT
└── payload/                    mirrors the layout of /userdata
    ├── guides/README.txt
    └── system/
        ├── configs/evmapy/any.keys       the hotkey binding
        └── gameguide/
            ├── gameguide.py              the viewer
            ├── gameguide-launch.sh       hotkey entry point
            ├── gameguide.conf            settings
            └── README.md                 full technical notes
```

## Install

With the SD card in this PC:

```sh
./install.sh /run/media/<you>/SHARE
```

`install.sh` also accepts no argument and defaults to `/userdata`, for running
it on the Brick itself — but that means copying this whole folder there first,
since neither the installer nor `payload/` is part of what gets installed.
Driving it from the PC over SMB or a card reader is the normal path.

Then launch any game — the hotkey is picked up the next time a game starts,
no reboot required.

## Use

Drop a plain-text guide next to the ROM, or into `/userdata/guides/<system>/`,
named after the game:

```
/userdata/guides/gba/Metroid Fusion (USA).txt
/userdata/roms/gba/Metroid Fusion (USA).gba
```

Then, in game:

| Input | Action |
|---|---|
| **MENU + SELECT** | open the guide |
| D-pad up / down | scroll |
| L1 / R1 | page up / page down |
| D-pad left / right | text size − / + |
| L2 / R2 | jump to start / end |
| SELECT | show the control list |
| A or B | close |

## Choosing the hotkey

**MENU + SELECT** is the default, and on this device it is the only two-button
combination that nothing else uses. That is not a guess — it falls out of
three independent facts:

- `libretroControllers.py` builds RetroArch's hotkey table from a fixed
  dictionary: `x y a b start up down left right pageup pagedown l2 r2`.
  `select` is not in it, so RetroArch never binds MENU + SELECT.
- The *Retroarch Hotkeys* remap menu offers that same list plus `l3`/`r3`.
  `select` is not offered, so no future remap can take it either.
- No `evmapy` `.keys` file anywhere in the KNULLI tree uses
  `["hotkey", "select"]`.

To re-check those claims against upstream:

| Claim | Source |
|---|---|
| RetroArch hotkey table | [`knulli-linux`](https://github.com/knulli-cfw/knulli-linux) → `package/system/knulli-configgen/configgen/configgen/generators/libretro/libretroControllers.py` (`default_specials`) |
| Remap menu choices | same repo → `package/emulationstation/knulli-es-system/es_features.yml` (`hotkey_*`) |
| evmapy merge order and `exec` actions | on-device `/usr/lib/python3.12/site-packages/configgen/utils/evmapy.py` |
| The feature this ports | [`ROCKNIX/distribution`](https://github.com/ROCKNIX/distribution) → `projects/ROCKNIX/packages/apps/sdl2text/` |

And because RetroArch blocks core input while the hotkey-enable button is
held, the game never sees the SELECT press.

Per-system `hotkey_*` remaps in `knulli.conf` make the rest of the MENU family
*more* crowded, not less. A worked example — a `gb` section carrying
`hotkey_left=l2`, `hotkey_right=r2`, `hotkey_l1=none`, `hotkey_r1=none`,
`hotkey_l2=pageup`, `hotkey_r2=pagedown`, run through
`libretroControllers.py`, resolves to:

| Combo | Effective action |
|---|---|
| MENU + X / Y / A / B | load state / save state / reset / RetroArch menu |
| MENU + START | exit emulator |
| MENU + UP / DOWN | state slot ± |
| MENU + L1 / R1 | shader prev / next |
| MENU + L2 | rewind |
| MENU + R2 | fast forward |
| **MENU + SELECT** | **nothing** |

MENU + LEFT and MENU + RIGHT are free *in that example*, because rewind and
fast-forward moved to the triggers — but on every other system they are still
rewind and fast-forward, and `any.keys` applies to all systems, so they are not
safe to bind here. MENU + SELECT is free either way.

### Changing it

One command, no JSON editing:

```sh
python3 /userdata/system/gameguide/gameguide.py --set-hotkey "select+l2+r2" --hold 0.3
```

Names: `a b x y start select hotkey pageup pagedown l2 r2 up down left right`,
with `l1`/`r1`/`menu` as aliases. `--hold` defaults to 0 (instant), matching
the shipped binding. Relaunch the game afterwards — evmapy reads the binding
when a game starts.

If MENU + SELECT ever feels wrong, the next-best options are three-button
combos that no emulator and no RetroArch hotkey can claim:

| Combo | Command |
|---|---|
| SELECT + L2 + R2 | `--set-hotkey "select+l2+r2" --hold 0.3` |
| START + L2 + R2 | `--set-hotkey "start+l2+r2" --hold 0.3` |

Avoid plain **L2 + R2** if you have remapped rewind and fast-forward onto the
triggers, as the example above does: it is not a RetroArch hotkey, but holding
both at once then becomes something you do during normal play.

## Why no overlay

The KNULLI docs suggest `knulli-save-overlay` for changes that must survive an
update, and that is the right advice for anything under `/usr`. It turns out
nothing here needs to live there:

- **The hotkey** goes in `/userdata/system/configs/evmapy/any.keys`. KNULLI's
  configgen merges that file into *every* emulator's key map — it is a
  first-class user extension point, read straight from userdata.
- **The viewer** is Python. KNULLI already ships everything it needs:
  python 3.12, pygame 2.5.2 (SDL2 2.32.8 + SDL2_ttf), python-evdev 1.7.1 and
  the DejaVu fonts. So it is just a script in `/userdata/system/gameguide/`.
- **The guides** are your own text files.

That is strictly better than an overlay: overlays have to be removed before
every KNULLI update and rebuilt afterwards, and they pin you to the system
version they were built against. This survives updates untouched, and a
factory reset is recovered by re-running `install.sh`.

If you ever *do* want it in the system image — say, to have `gameguide` on
`$PATH` — copy `gameguide.py` to `/usr/bin/`, run `knulli-save-overlay`, and
remember to delete the overlay before updating.

## How it renders over a running emulator

This is the part that could not simply be copied from ROCKNIX. ROCKNIX runs a
Wayland compositor, so its `sdl2text` viewer just opens a second fullscreen
window on top of the emulator. KNULLI on the Brick has no compositor at all —
its SDL2 build contains exactly two video drivers, `mali` and `dummy`, and
`mali` is a bare fbdev/EGL backend. Two EGL surfaces would fight over the same
framebuffer.

So instead the viewer suspends the emulator (`SIGSTOP` on the whole
`emulatorlauncher` process tree), renders glyphs with `pygame.font` into an
ordinary in-memory surface — which needs no SDL video display — and copies the
finished page straight into the mmapped `/dev/fb0`. No EGL context is created,
so there is nothing to contend with. The old framebuffer contents are saved
first and restored on exit, so the game reappears instantly rather than after
the emulator's next frame.

Full details, including the input-grab timing that stops the emulator from
seeing a phantom stuck hotkey, are in
[`payload/system/gameguide/README.md`](payload/system/gameguide/README.md).

## Before you ship a change

`python -m py_compile` is not enough here. Most of this file is branches that
only run on the device — the 16 bpp blit path, the SDL renderer, the
pan-refused fallback — so a name that does not exist parses perfectly and only
raises when that branch finally executes. That is exactly how a missing
`FBIOPAN_DISPLAY` constant reached the hardware.

```sh
pyflakes payload/system/gameguide/gameguide.py     # or: ruff check --select F
```

Both flag undefined names at the exact line, plus unused imports and dead
locals. Run one of them before copying anything to the device.

## Verify / troubleshoot

Over SSH on the Brick, with a game running:

```sh
python3 /userdata/system/gameguide/gameguide.py --diag            # environment report
python3 /userdata/system/gameguide/gameguide.py --test            # built-in test page
python3 /userdata/system/gameguide/gameguide.py --test --renderer sdl
python3 /userdata/system/gameguide/gameguide.py --probe           # which slot reaches the panel?
cat /userdata/system/gameguide/gameguide.log                      # what happened last
```

**If the emulator freezes but nothing appears**, that is the display path, not
the hotkey. `--probe` settles it: it suspends the emulator and paints a big
labelled page into each framebuffer slot in turn, then once through SDL. Note
which label you saw and set it in `gameguide.conf`:

| What you saw | Setting |
|---|---|
| `SLOT 0 + PAN` | `renderer = fb` (the default — already correct) |
| `SLOT n` only | `renderer = fb` and `fb_all_buffers = 4` |
| `SDL` only | `renderer = sdl` |
| nothing at all | neither path reaches the panel — say so |

**If nothing happens at all on the hotkey**, check the log: the launcher now
writes a `launch: hotkey fired` line before it does anything else, and
`--diag` prints whether evmapy installed the action into
`/var/run/evmapy/<pad>.json`. No log line and no live action means evmapy
never got the binding; no log line but the action is present means the combo
is not matching; a log line but no guide means the viewer failed.

## Status on hardware

Confirmed working on the Brick, KNULLI *scarab*:

- pygame 2.5.2 / SDL 2.32.8 / Python 3.12.8 present; pad map read from
  `es_input.cfg`; guide lookup resolves against real ROMs.
- Suspend/resume of the emulator is clean (`freeze: stopped [pid]` …
  `thaw: resumed [pid]`).
- The framebuffer path rendered correctly over a running Game Boy session.

Then it stopped rendering — same freeze, blank screen. Cause: the visible
buffer slot was being resolved *before* the emulator was suspended, while it
was still page-flipping across a 21-slot virtual framebuffer, so the guide was
often painted into a slot that was not being scanned out. Fixed by resolving
the slot after the freeze and using `FBIOPAN_DISPLAY` to make our slot the
visible one. `--probe` exists to settle it empirically if the driver turns out
to ignore panning.

Still unverified: how a *standalone* (non-libretro) emulator reacts to being
`SIGSTOP`ped mid-frame. RetroArch, which covers nearly everything on the
Brick, carries on without complaint.

## Licence

MIT — see [`LICENSE`](LICENSE).

This is an independent implementation, not a fork. It was written after reading
ROCKNIX's `sdl2text` and KNULLI's configgen to understand the feature and the
platform, but shares no code with either: the viewer is original Python, and the
display approach is different by necessity (ROCKNIX stacks an SDL window over a
Wayland compositor; this suspends the emulator and writes to `/dev/fb0`, because
KNULLI on this hardware has no compositor). Both of those projects are GPL-2.0;
nothing of theirs is redistributed here.
