# Implementation notes

For anyone maintaining or porting this. End-user documentation is in
[README.md](README.md).

## Layout

| Path | |
|---|---|
| `payload/system/gameguide/gameguide.py` | all of it: game discovery, emulator suspend, both renderers, input, hotkey binding, diagnostics |
| `payload/system/gameguide/gameguide-launch.sh` | hotkey entry point; `flock` single-instance and an `EXIT` trap that always un-suspends |
| `payload/system/configs/evmapy/any.keys` | the evmapy binding, merged by KNULLI into every emulator's key map |
| `install.sh` | copies into userdata, then binds the hotkey for the detected controller |

`payload/` mirrors `/userdata`, so installing is a copy.

## Why not the ROCKNIX approach

ROCKNIX opens a second fullscreen SDL window over the emulator. That needs a
compositor. KNULLI's Allwinner builds have none — SDL2 there has exactly two
video drivers, `mali` and `dummy`, and `mali` is a bare fbdev/EGL backend, so
two EGL surfaces would fight over one framebuffer.

Instead: find the game, suspend it, take the pad, paint into `/dev/fb0`, put
everything back.

1. **Find the game.** `emulatorlauncher` lives for the whole session and its
   `/proc/<pid>/cmdline` carries `-system` and `-rom`. No hooks, no state
   files. Matched on the executable basename so a stray `grep emulatorlauncher`
   cannot win on PID order.
2. **Suspend it.** `SIGSTOP` to every descendant, then poll `/proc/<pid>/stat`
   for state `T` — `os.kill` returns when the signal is queued, not when the
   process has stopped, and step 4 depends on it having actually stopped.
3. **Take the pad**, see below.
4. **Paint.** `pygame.font` renders glyphs into an ordinary in-memory Surface,
   which needs no SDL video display at all, and the page is copied into the
   mmapped framebuffer. No EGL context, so nothing to contend with.
5. **Restore.** Previous framebuffer contents are snapshotted first, so the
   game reappears instantly instead of after its next frame.

## Which buffer slot

These framebuffers are much taller than the screen — 1024x16384 on a Brick,
twenty-one screen-sized slots the emulator pans between. The visible slot is
resolved in `Framebuffer.acquire()`, **after** the suspend; reading it earlier
gives an answer already stale by the time we draw, which is why an early
version rendered correctly once and then went blank.

`FBIOPAN_DISPLAY` then makes our slot the visible one rather than guessing at
it, and the original offset is handed back on exit. `fb_all_buffers` (paint
every slot) and `--probe` exist for a driver that ignores panning.

The call order in `main()` is load-bearing — `pad.open` → `freeze` →
`wait_until_idle` → `grab` → `acquire` — so `present()` and `save()` raise if
`acquire()` has not run.

## Input grab timing

Grabbing matters: without it every scroll press sits in the suspended
emulator's event queue and replays into the game on resume.

But a grab must only be taken while nothing is held. The guide opens with the
hotkey combo still pressed; grabbing immediately would swallow those key-up
events and the emulator would resume believing the buttons were still down —
with a `hotkey+…` combo that blocks all input until MENU is tapped again. So
the pad is opened *ungrabbed*, the emulator is suspended, and `EVIOCGRAB` is
taken only once nothing is pressed. Those releases queue for the suspended
emulator and arrive intact. On the way out the grab is dropped *before*
`SIGCONT`, for the same reason.

If the combo is still held after two seconds it grabs anyway. evmapy then also
misses those releases and still believes the combo is half-held, so a later
lone SELECT press can reopen the guide. One MENU tap clears it and it lasts
only for that game session.

## Text

Monospaced on purpose: GameFAQs guides are ASCII tables and maps that only
line up at fixed pitch. It also makes wrapping pure integer arithmetic —
prefix sums plus `bisect` — so a two-megabyte guide opens instantly instead of
measuring fifty thousand strings.

`font_size = auto` targets `target_columns` (80) across, which is the width
guides are written for: 13pt on a 640x480 RG35XX, 21pt on a Brick, 40pt at
1080p. A saved per-guide size overrides it.

## Rotation

A panel mounted sideways presents a portrait framebuffer and KNULLI's SDL2
turns everything drawn into it. `rotate = auto` reproduces that driver's own
rule and honours the same override:

```c
/* If the device seems to be portrait mode, set default as rotated. */
data->rotation = (vinfo.xres < vinfo.yres) ? 1 : 0;
rotation = SDL_GetHint("SDL_ROTATION");
```

so the guide lands the same way up as the emulator. `rotate = 90|180|270`
forces it.

## Hotkey selection

`--set-hotkey auto` walks a preference list and takes the first the controller
can express:

| | Combo | |
|---|---|---|
| 1 | MENU + SELECT, instant | RetroArch never binds it, and the game never sees it |
| 2 | L2 + R2, held ½ s | not a RetroArch combo, absent as game input on pre-PSX systems |
| 3 | L1 + R1, held ¾ s | last resort; the game does see these |

Two collisions are worth knowing about. Only the second rules a combo out.

**MENU + SELECT is not free on handhelds without analog sticks.** RetroArch
never binds it — `select` is absent from `default_specials` in
`libretroControllers.py` and from the *Retroarch Hotkeys* remap menu — but
KNULLI uses it for the D-pad / virtual-joystick toggle in
`/usr/bin/dpad-toggle`, which opens with:

```sh
if knulli-board-capability "analogstick"; then exit 1; fi
```

That binding is invisible to RetroArch's config and to every `evmapy` key map,
which is what makes it easy to miss. It is reported as a note, not treated as
a blocker: both actions simply fire.

**`hotkey` and `select` can be the same physical button.** On pads with no
dedicated MENU button they share a code; KNULLI's configgen aliases them and
evmapy then rejects the *entire* key map with `duplicate event(s) in action
trigger`, taking any other actions down with it. Combos that would collapse
like that are refused outright.

Per-system `hotkey_*` entries in `knulli.conf` also move RetroArch's specials
around, which can free some MENU combos and consume others. Nothing here
depends on them.

## Why no boot overlay

Nothing needs to live outside `/userdata`:

- The hotkey goes in `/userdata/system/configs/evmapy/any.keys`, which
  configgen merges into every emulator's key map — a first-class user
  extension point. There is no `any.keys` upstream, so nothing to conflict
  with.
- The viewer is Python, and KNULLI already ships python 3.12, pygame 2.5.2
  (SDL2 + SDL2_ttf), python-evdev 1.7.1 and the DejaVu fonts.

Overlays have to be removed before every KNULLI update and rebuilt after. This
survives updates untouched, and a factory reset is recovered by re-running
`install.sh`.

## Platform support

What decides it is the display stack, not the screen size:

- **A133** and **H700** patch SDL2 with `add-video-malifb-driver.patch` —
  fbdev, so there is a scanned-out `/dev/fb0`.
- **RK3566** patches are all `KMSDRM-*`, and **Snapdragon 865** has no SDL
  patch at all, so stock KMSDRM. No scanned-out `/dev/fb0` on either, and
  `open_output()` refuses with an explanation rather than half-working.

There is deliberately no automatic fallback to `SdlOutput`: that path opens a
second EGL surface against a GPU the emulator still owns, and has never run on
any hardware. It must be asked for with `renderer = sdl`.

## Before you ship a change

`python -m py_compile` is not enough. Most of this file is branches that only
run on the device — the 16 bpp blit, the SDL renderer, the pan-refused
fallback — so a name that does not exist parses fine and only raises when that
branch finally executes. That is how a missing `FBIOPAN_DISPLAY` constant
reached hardware once already.

```sh
pyflakes payload/system/gameguide/gameguide.py     # or: ruff check --select F
```

## Status

Verified on a TrimUI Brick, KNULLI *scarab*: framebuffer geometry and pixel
format, pygame/SDL/evdev present, pad map from `es_input.cfg`, guide lookup
against real ROMs, suspend and resume of a live RetroArch session, and the
guide rendering over a running game.

Unverified: every non-A133 device; the 16 bpp blit path; `SdlOutput` on any
hardware; how a *standalone* (non-libretro) emulator reacts to `SIGSTOP`
mid-frame — RetroArch, which covers nearly everything on these handhelds,
carries on without complaint.

## Known weaknesses

- One 1,900-line module. Deliberate — it has to be copyable to a device with
  no package management — but `main()` is long enough to hide an ordering bug.
- **No committed test suite.** Everything was verified with throwaway
  harnesses, including a fake-ioctl framebuffer that is what finally exercised
  `acquire()`. That should be a file, not a memory. Highest-value follow-up.
- `probe_display()` reaches into `Framebuffer` privates.
- `LAUNCH_CMD` hardcodes the device-side path, which is correct because it is
  executed there, but it means the installer cannot target a non-standard
  location.

## Upstream sources

| Claim | Where |
|---|---|
| RetroArch hotkey table | [`knulli-linux`](https://github.com/knulli-cfw/knulli-linux) → `package/system/knulli-configgen/configgen/configgen/generators/libretro/libretroControllers.py` (`default_specials`) |
| Remap menu choices | same repo → `package/emulationstation/knulli-es-system/es_features.yml` |
| MENU+SELECT D-pad toggle | same repo → `board/allwinner/a133/fsoverlay/usr/bin/dpad-toggle`, and the [hotkey shortcuts](https://knulli.org/play/hotkey-shortcuts/) page |
| SDL rotation rule | same repo → `board/allwinner/h700/patches/sdl2/0002-mali-fbdev-*rotation.patch` |
| evmapy merge order, aliasing, `exec` actions | on-device `/usr/lib/python3.12/site-packages/configgen/utils/evmapy.py` |
| The feature this ports | [`ROCKNIX/distribution`](https://github.com/ROCKNIX/distribution) → `projects/ROCKNIX/packages/apps/sdl2text/` |
