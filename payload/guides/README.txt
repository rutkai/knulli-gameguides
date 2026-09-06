KNULLI Game Guides -- where to put your guide files
===================================================

Guides are plain UTF-8 (or Latin-1 / CP1252) .txt files, exactly the format
you get from the "Download this guide" / plain-text link on GameFAQs.
Images, HTML and emoji are not supported.

The file must be named after the ROM, without the ROM's extension.

Two places are searched, in this order.

1. Next to the ROM  (this is what ROCKNIX does)

       /userdata/roms/gba/Metroid Fusion (USA).gba
       /userdata/roms/gba/Metroid Fusion (USA).txt

   A "guides" sub-folder next to the ROM also works, and keeps the .txt out
   of EmulationStation's way:

       /userdata/roms/gba/guides/Metroid Fusion (USA).txt

2. In this folder, optionally under a per-system sub-folder

       /userdata/guides/gba/Metroid Fusion (USA).txt
       /userdata/guides/Metroid Fusion (USA).txt

   The system name is the ROM folder's name: gba, snes, psx, megadrive ...

Recommended: use option 2. Nothing extra ends up in your ROM folders, so
scrapers and gamelist.xml stay clean.


Opening a guide
---------------

While a game is running, press  MENU + SELECT.

(To use a different combination:
     python3 /userdata/system/gameguide/gameguide.py --set-hotkey "select+l2+r2"
 then relaunch the game.)

    D-PAD UP / DOWN     scroll
    L1 / R1             page up / page down
    D-PAD LEFT / RIGHT  text size smaller / larger
    L2 / R2             jump to start / end
    SELECT              show the on-screen control list
    A or B              close the guide and return to the game

The emulator is paused while the guide is on screen, and your reading
position and text size are remembered per guide -- even after you quit the
game and come back to it days later.
