# KNULLI Game Guides

Read a GameFAQs walkthrough without leaving your game. Press the hotkey and a
plain-text guide covers the screen; press A or B and you are back in the game
at the same frame, with your place in the guide remembered — even after you
quit and come back days later.

A port of the ROCKNIX [Game Guides](https://rocknix.org/configure/gameguides/)
feature to KNULLI. Everything installs into the userdata partition, so KNULLI
updates cannot remove it and no boot overlay is needed.

## Supported devices

| Platform | Devices | |
|---|---|---|
| Allwinner **A133** | TrimUI Brick / Smart Pro, Powkiddy V20 / V90s, MagicX Zero 28 / 40, XU20 | works (tested on Brick) |
| Allwinner **H700** | Anbernic RG28XX, RG34XX, RG35XX (Plus / H / SP / Pro / 2024), RG40XX H / V, RGCubeXX | should work, untested |
| Rockchip **RK3566** | Powkiddy RGB30 / X55, Miyoo Flip, Anbernic RG Arc S | not supported |
| **Snapdragon 865** | Retroid Pocket 5 / Mini / Flip 2 | not supported |

The last two draw the screen a different way; the tool says so and does nothing
rather than half-working. Screen size, controller layout and hotkey are all
detected, so there is nothing to configure per device.

## Install

With the SD card in a PC:

```sh
./install.sh /run/media/<you>/SHARE
```

Or copy this folder onto the device and run it there with no argument — it
finds the userdata root from wherever you are:

```sh
cd /userdata/gameguides && ./install.sh
```

Launch any game afterwards. No reboot needed.

## Add a guide

Save the **plain text** version of a guide (not the web page) named after the
ROM, in either place:

```
/userdata/guides/gba/Metroid Fusion (USA).txt      <- recommended
/userdata/roms/gba/Metroid Fusion (USA).txt        <- also works
```

The first keeps `.txt` files out of your ROM folders so scrapers and gamelists
stay clean. `<system>` is the ROM folder's name — `gba`, `snes`, `psx` and so
on. UTF-8, CP1252 and Latin-1 are all read correctly.

## Controls

| Input | |
|---|---|
| **MENU + SELECT** | open the guide |
| D-pad up / down | scroll |
| L1 / R1 | page up / down |
| D-pad left / right | text size − / + |
| L2 / R2 | jump to start / end |
| SELECT | show the controls on screen |
| A or B | close |

The game is paused while the guide is open.

## Changing the hotkey

```sh
python3 /userdata/system/gameguide/gameguide.py --set-hotkey "select+r2"
python3 /userdata/system/gameguide/gameguide.py --set-hotkey auto   # let it choose
```

Button names: `a b x y start select hotkey pageup pagedown l2 r2 up down left
right`, with `l1`, `r1` and `menu` accepted as aliases. Add `--hold 0.5` to
require the combo be held, worth doing for combos the game can also see.
Relaunch the game afterwards.

MENU + SELECT is the default because RetroArch never binds it and the game
never sees it. Two things to know:

- On handhelds **without analog sticks**, KNULLI also uses MENU + SELECT to
  toggle its virtual joystick, so both happen at once. Pick another combo if
  that bothers you.
- On handhelds with **no dedicated MENU button**, MENU and SELECT are the same
  physical button and the combo is impossible. `--set-hotkey auto` detects that
  and picks L2 + R2 instead.

## Troubleshooting

Over SSH, with a game running:

```sh
python3 /userdata/system/gameguide/gameguide.py --diag    # what it detected
python3 /userdata/system/gameguide/gameguide.py --test    # show a test page
cat /userdata/system/logs/gameguide.log                   # what happened last
```

**Nothing happens when I press the hotkey.** Look for a `launch: hotkey fired`
line in the log. If it is missing the binding never reached evmapy — run
`--set-hotkey auto` and relaunch the game. `--diag` also prints whether evmapy
picked the action up.

**The game freezes but no guide appears.** That is the display, not the hotkey.
Run `--probe`: it paints a labelled page into each screen buffer in turn, then
once through SDL. Set the winner in `/userdata/system/gameguide/gameguide.conf`:

| What you saw | Setting |
|---|---|
| `SLOT 0 + PAN` | nothing to change |
| `SLOT n` for some n | `fb_all_buffers = 4` |
| `SDL` only | `renderer = sdl` |

**The guide is sideways.** Set `rotate = 90`, `180` or `270` in
`gameguide.conf`, or use `renderer = sdl`.

**Grey background, text as black bars.** The screen layer is honouring the
alpha channel. `force_opaque = 1` in `gameguide.conf` fixes it and is the
default; check it has not been switched off.

**The screen dims, or the handheld sleeps, while I am reading.** Fixed by
default. KNULLI decides you are idle by watching the buttons, and the guide
takes those over while it is open, so it now reports your presses back. If it
still happens, check `idle_keepalive` in `gameguide.conf` is not `off`; set it
to `always` to stay awake for as long as the guide is up, even untouched.

**"No guide file was found".** The message lists every path it tried. The name
must match the ROM exactly, minus the extension.

**Text too small or too large.** D-pad left/right while reading, remembered per
guide. Or set `font_size` in `gameguide.conf`; `auto` targets the ~80 columns
guides are written for.

## Settings

`/userdata/system/gameguide/gameguide.conf` — font, colours, scroll speed,
guide folders, renderer. Every option is commented in the file.

---

Implementation notes are in [INTERNALS.md](INTERNALS.md).
