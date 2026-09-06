# KNULLI Game Guides

An in-game text guide viewer for KNULLI, modelled on the ROCKNIX
[Game Guides](https://rocknix.org/configure/gameguides/) feature.
Press a hotkey during a game and a GameFAQs-style plain-text walkthrough
appears over it; press A or B and you are back in the game exactly where you
left off.

Built and tested against KNULLI *scarab* on a **TrimUI Brick** (Allwinner
A133 / PowerVR GE8300, 1024x768), but nothing in it is Brick specific.

## Files

| Path | What it is |
|---|---|
| `gameguide.py` | the whole thing: guide lookup, emulator suspend, renderer, input |
| `gameguide-launch.sh` | hotkey entry point; guarantees the emulator is un-suspended |
| `gameguide.conf` | user settings (font, size, colours, scroll speed, renderer) |
| `positions.conf` | per-guide reading position and text size (written automatically) |
| `gameguide.log` | last few runs; the first place to look if something misbehaves |
| `../configs/evmapy/any.keys` | the hotkey binding, merged into every emulator by KNULLI |

## Hotkey

**MENU + SELECT** while a game is running.

It is the only two-button combination on this device that nothing else claims:
`select` is absent from the `default_specials` table in
`libretroControllers.py`, absent from the choices in the *Retroarch Hotkeys*
remap menu, and unused by every shipped `evmapy` `.keys` file. RetroArch also
blocks core input while the hotkey-enable button is held, so the game never
sees the press.

Change it with one command:

```sh
python3 /userdata/system/gameguide/gameguide.py --set-hotkey "select+l2+r2" --hold 0.3
```

Names: `a b x y start select hotkey pageup pagedown l2 r2 l3 r3 up down left
right`, plus the aliases `l1`→`pageup`, `r1`→`pagedown`, `menu`→`hotkey`.
`--hold` defaults to 0 — instant, matching the shipped binding — and a
single-button combo is refused unless you give it a hold time. Relaunch the
game afterwards; evmapy reads the binding when a game starts.

Good alternatives, if MENU + SELECT does not suit: `select+l2+r2` or
`start+l2+r2`, both held ~0.3 s. Three-button combos are not RetroArch
hotkeys and are effectively impossible to hit by accident.

Avoid plain `l2+r2` if you have remapped rewind/fast-forward onto the
triggers (`hotkey_left=l2`, `hotkey_right=r2`) — you would then be holding
both of them during normal play.

## Controls inside the guide

| Input | Action |
|---|---|
| D-pad up / down | scroll |
| L1 / R1 | page up / page down |
| D-pad left / right | text size smaller / larger |
| L2 / R2 | jump to start / end |
| Analog stick (if fitted) | scroll |
| SELECT | toggle the on-screen control list |
| A or B | close and return to the game |

## Where guides go

Named after the ROM, without its extension. First match wins:

1. `<rom folder>/<game>.txt` — the ROCKNIX-compatible location
2. `<rom folder>/<game>.<ext>.txt`
3. `<rom folder>/guides/<game>.txt`
4. `/userdata/guides/<system>/<game>.txt`  ← recommended
5. `/userdata/guides/<game>.txt`

Option 4 keeps `.txt` files out of your ROM folders, so scrapers and
`gamelist.xml` stay clean.

Plain text only — UTF-8, CP1252 or Latin-1 are all decoded. Use the
"plain text" version of a GameFAQs guide, not the HTML page.

## How it works, and why it works that way

KNULLI on the Brick has no compositor. Its SDL2 build ships exactly two video
drivers — `mali` and `dummy` — and `mali` is a straight fbdev/EGL backend.
There is no window manager to stack a second surface on top of a running
emulator, so the ROCKNIX approach (a second fullscreen SDL window over a
Wayland compositor) is not available.

Instead:

1. **Find the game.** The `emulatorlauncher` process lives for the whole game
   session; its `/proc/<pid>/cmdline` carries `-system` and `-rom`. No hook
   scripts, no state files, nothing to get out of sync.
2. **Suspend the emulator.** Every descendant of `emulatorlauncher` gets
   `SIGSTOP`, so it stops flipping framebuffers. The PIDs are written to
   `/var/run/gameguide.stopped` *before* the signal, so a crash can still be
   cleaned up (`gameguide.py --resume`, which the launcher's `EXIT` trap always
   runs).
3. **Take the pad — but not too early.** The guide opens with the hotkey combo
   still held. Grabbing the device immediately would swallow those key-up
   events and the emulator would resume believing the buttons were still down
   — with a `hotkey+…` combo that blocks all input until you tap MENU again.
   So the pad is opened *without* a grab, the emulator is suspended, and only
   once every button is physically released is `EVIOCGRAB` taken. Those
   releases queue up for the suspended emulator and reach it intact when it
   resumes. On the way out the grab is dropped *before* `SIGCONT` for the same
   reason.
4. **Draw straight into `/dev/fb0`.** `pygame.font` (SDL2_ttf) renders glyphs
   into an ordinary in-memory `Surface` — that needs no SDL video display at
   all — and the finished page is `memcpy`'d into the mmapped framebuffer. No
   EGL context, so nothing can contend with the emulator's. The previous
   contents are snapshotted first and put back on exit, so the game reappears
   instantly.

   **Which buffer slot.** This panel's framebuffer is 1024x16384: twenty-one
   screen-sized slots that the emulator pans between. The visible slot is
   therefore resolved in `Framebuffer.acquire()`, *after* the emulator is
   suspended — reading it earlier gives an answer that is already stale by the
   time we draw, which is why an earlier version rendered correctly once and
   then went blank. `FBIOPAN_DISPLAY` is then used to make our slot the
   visible one rather than merely guessing at it, and the original pan offset
   is handed back on exit. `fb_all_buffers` (paint every slot) and
   `gameguide.py --probe` (paint a labelled page into each slot in turn) are
   there in case a driver ignores panning.
5. **Monospaced text.** GameFAQs guides are ASCII art: tables, maps and
   column-aligned item lists. A proportional font mangles them. Using a fixed
   pitch also makes wrapping pure integer arithmetic, so a two-megabyte guide
   opens instantly instead of measuring fifty thousand strings.

If the direct framebuffer path ever shows nothing on some other device, set
`renderer = sdl` in `gameguide.conf` to go through SDL2 instead. That path
creates a second EGL surface, which is only safe *because* the emulator is
suspended.

## Checking an install

Over SSH:

```sh
python3 /userdata/system/gameguide/gameguide.py --diag
```

reports the framebuffer geometry, the pygame/SDL_ttf versions, the button map
read from `es_input.cfg`, every evdev node, the guide it would open for the
running game, and — most useful when a hotkey misbehaves — whether evmapy is
running and whether it actually installed our action into
`/var/run/evmapy/<pad>.json`.

```sh
python3 /userdata/system/gameguide/gameguide.py --test
python3 /userdata/system/gameguide/gameguide.py --test --renderer sdl
python3 /userdata/system/gameguide/gameguide.py --probe
```

`--test` suspends the running game and paints a built-in page, so you can
check the display and every control without needing a guide file. `--probe`
goes further: it paints a big labelled page into each framebuffer slot in
turn and then once through SDL, so if the screen stays blank you can see
which path actually reaches the panel and set `renderer` / `fb_all_buffers`
accordingly.

## Known limitations

- The emulator is genuinely paused, so netplay and RetroAchievements sessions
  will notice. Audio may click once on resume as ALSA re-primes.
- A combo without MENU is visible to the game, because RetroArch only blocks
  core input while the hotkey-enable button is held. That is why the default
  is a MENU combo, and why the suggested alternatives use a hold time.
- Creating `/userdata/system/configs/evmapy/any.keys` makes KNULLI start
  `evmapy` for *every* system, including ones that previously had no key map.
  It runs ungrabbed with a single action, so nothing else changes.
- If you hold the hotkey combo for more than two seconds, `wait_until_idle()`
  gives up and grabs the pad anyway. evmapy then misses those key releases and
  still believes the combo is half-held, so a later lone SELECT press can
  reopen the guide. Tapping MENU once clears it, and it lasts only for the
  current game session. Releasing the combo normally avoids it entirely.
