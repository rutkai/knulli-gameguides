#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
KNULLI Game Guides -- an in-game GameFAQs-style text guide viewer.

A re-implementation of the ROCKNIX "Game Guides" feature for KNULLI
(Batocera-like) devices, written for the TrimUI Brick but device agnostic.

How it works
------------
KNULLI on the Brick has no compositor: SDL2 only ships the "mali" (fbdev/EGL)
video driver, so a second GPU surface cannot be stacked on top of a running
emulator.  Instead this tool:

  1. locates the running game via /proc (the `emulatorlauncher` command line),
  2. finds the matching .txt guide,
  3. SIGSTOPs the emulator process tree so it stops flipping the framebuffer,
  4. takes an exclusive grab of the gamepad (EVIOCGRAB) so no stray input
     leaks into the paused game,
  5. paints the guide straight into /dev/fb0 (no EGL, no GPU contention),
  6. on exit restores the framebuffer contents and SIGCONTs the emulator.

Text is rendered with pygame.font (SDL2_ttf), which works without ever
initialising an SDL video display.  A monospaced face is used on purpose:
GameFAQs guides are ASCII-art formatted and only line up in a fixed pitch.

Usage
-----
  gameguide.py --run                 open the guide for the running game
  gameguide.py --show FILE.txt       open an arbitrary text file
  gameguide.py --test                show a built-in test page
  gameguide.py --set-hotkey "..."    change the combo that opens the guide
  gameguide.py --probe               find out what actually reaches the panel
  gameguide.py --resume              SIGCONT anything a crashed run left stopped
  gameguide.py --diag                print environment diagnostics and exit

SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import argparse
import bisect
import fcntl
import mmap
import os
import re
import select
import shutil
import signal
import struct
import sys
import time
import xml.etree.ElementTree as ET

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONF_FILE = os.path.join(BASE_DIR, "gameguide.conf")
POS_FILE = os.path.join(BASE_DIR, "positions.conf")
LOG_FILE = os.path.join(BASE_DIR, "gameguide.log")
STOPPED_FILE = "/var/run/gameguide.stopped"

ES_INPUT_CFG = "/userdata/system/configs/emulationstation/es_input.cfg"

# ---------------------------------------------------------------------------
# logging
# ---------------------------------------------------------------------------

_LOG_FH = None


def log(msg: str) -> None:
    global _LOG_FH
    line = "%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        if _LOG_FH is None:
            # keep the log from growing without bound
            try:
                if os.path.getsize(LOG_FILE) > 256 * 1024:
                    os.replace(LOG_FILE, LOG_FILE + ".1")
            except OSError:
                pass
            _LOG_FH = open(LOG_FILE, "a")
        _LOG_FH.write(line)
        _LOG_FH.flush()
    except Exception:
        pass
    sys.stderr.write(line)


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------

DEFAULTS = {
    "font": "/usr/share/fonts/dejavu/DejaVuSansMono.ttf",
    "font_fallbacks": ":".join([
        "/usr/share/fonts/dejavu/DejaVuSansMono.ttf",
        "/usr/share/fonts/dejavu/DejaVuSansCondensed.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans.ttf",
        "/usr/share/libretro/assets/ozone/regular.ttf",
        "/usr/share/emulationstation/resources/ubuntu_condensed.ttf",
    ]),
    "font_size": "20",
    "font_size_min": "10",
    "font_size_max": "48",
    "fg": "#e8e8e8",
    "bg": "#101010",
    "accent": "#7fb2ff",
    "margin_x": "12",
    "margin_y": "8",
    "scroll_lines": "1",          # lines per repeat tick when holding up/down
    "page_lines": "0",            # 0 = one screen minus 2 lines
    "repeat_delay": "0.35",       # seconds before auto-repeat starts
    "repeat_rate": "0.035",       # seconds between auto-repeat ticks
    "max_fps": "30",
    "guide_dirs": "/userdata/guides",
    "renderer": "fb",
    "fbdev": "/dev/fb0",
    "fb_force_pan": "1",
    "fb_all_buffers": "0",
    "restore_framebuffer": "1",
    "stop_emulator": "1",
    "show_line_counter": "1",
    "help_on_open": "0",
}


def load_conf() -> dict:
    conf = dict(DEFAULTS)
    try:
        with open(CONF_FILE, "r", encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw or raw.startswith("#") or "=" not in raw:
                    continue
                key, _, val = raw.partition("=")
                conf[key.strip()] = val.strip()
    except OSError:
        pass
    return conf


def conf_int(conf: dict, key: str, default: int) -> int:
    try:
        return int(str(conf.get(key, default)).strip())
    except (TypeError, ValueError):
        return default


def conf_float(conf: dict, key: str, default: float) -> float:
    try:
        return float(str(conf.get(key, default)).strip())
    except (TypeError, ValueError):
        return default


def conf_bool(conf: dict, key: str, default: bool) -> bool:
    val = str(conf.get(key, "1" if default else "0")).strip().lower()
    return val in ("1", "true", "yes", "on")


def parse_colour(text: str, fallback=(255, 255, 255)):
    text = str(text).strip()
    m = re.fullmatch(r"#?([0-9a-fA-F]{6})", text)
    if m:
        v = int(m.group(1), 16)
        return ((v >> 16) & 0xFF, (v >> 8) & 0xFF, v & 0xFF)
    parts = [p for p in re.split(r"[,\s]+", text) if p]
    if len(parts) == 3:
        try:
            return tuple(max(0, min(255, int(p))) for p in parts)
        except ValueError:
            pass
    return fallback


def pick_font(conf: dict) -> str:
    """First readable font from the config, then anything TTF on the system."""
    candidates = [conf.get("font", "")]
    candidates += conf.get("font_fallbacks", "").split(":")
    for path in candidates:
        path = path.strip()
        if path and os.path.isfile(path):
            return path
    for root in ("/usr/share/fonts", "/usr/local/share/fonts"):
        for dirpath, _dirs, files in os.walk(root):
            for name in sorted(files):
                if name.lower().endswith((".ttf", ".otf")):
                    return os.path.join(dirpath, name)
    raise RuntimeError("no usable TTF font found on this system")


# ---------------------------------------------------------------------------
# /proc helpers -- find the running game and its emulator processes
# ---------------------------------------------------------------------------

def _pids():
    for name in os.listdir("/proc"):
        if name.isdigit():
            yield int(name)


def read_cmdline(pid: int) -> list[str]:
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as fh:
            data = fh.read()
    except OSError:
        return []
    return [p.decode("utf-8", "replace") for p in data.split(b"\0") if p]


def process_state(pid: int) -> str | None:
    """One-letter process state from /proc/<pid>/stat, or None if it is gone."""
    try:
        with open("/proc/%d/stat" % pid, "r") as fh:
            data = fh.read()
    except OSError:
        return None
    idx = data.rfind(")")
    if idx < 0:
        return None
    fields = data[idx + 2:].split()
    return fields[0] if fields else None


def read_ppid(pid: int) -> int:
    try:
        with open("/proc/%d/stat" % pid, "r") as fh:
            data = fh.read()
    except OSError:
        return 0
    # comm may contain spaces and parentheses -- split after the last ')'
    idx = data.rfind(")")
    if idx < 0:
        return 0
    fields = data[idx + 2:].split()
    try:
        return int(fields[1])
    except (IndexError, ValueError):
        return 0


def is_launcher_argv(argv: list[str]) -> bool:
    """
    True only if argv actually *runs* emulatorlauncher.

    Matching the bare word anywhere in the command line also matches, say, a
    `grep emulatorlauncher` in an SSH session -- which, because we prefer the
    highest PID, would then win.
    """
    if not argv:
        return False
    exe = os.path.basename(argv[0])
    if exe == "emulatorlauncher":
        return True
    # emulatorlauncher is a Python script; it may be invoked via the interpreter
    return (exe.startswith("python")
            and len(argv) > 1
            and os.path.basename(argv[1]) == "emulatorlauncher")


def find_launcher_pid() -> int | None:
    """The `emulatorlauncher` process stays alive for the whole game session."""
    best = None
    for pid in _pids():
        if is_launcher_argv(read_cmdline(pid)):
            # prefer the newest one if several linger
            if best is None or pid > best:
                best = pid
    return best


def parse_launcher_args(argv: list[str]) -> dict:
    """Pull -rom / -system / -systemname out of the emulatorlauncher argv."""
    out = {}
    keys = {"-rom": "rom", "-system": "system", "-systemname": "systemname",
            "-gameinfoxml": "gameinfoxml", "-emulator": "emulator", "-core": "core"}
    i = 0
    while i < len(argv):
        key = keys.get(argv[i])
        if key is not None and i + 1 < len(argv):
            out[key] = argv[i + 1]
            i += 2
        else:
            i += 1
    return out


def ancestors_of(pid: int) -> set[int]:
    seen = set()
    cur = pid
    while cur > 1 and cur not in seen:
        seen.add(cur)
        cur = read_ppid(cur)
    return seen


def descendants_of(root: int, exclude: set[int]) -> list[int]:
    """All live descendants of `root`, deepest first."""
    children: dict[int, list[int]] = {}
    for pid in _pids():
        ppid = read_ppid(pid)
        if ppid:
            children.setdefault(ppid, []).append(pid)

    ordered: list[int] = []

    def walk(pid: int, depth: int) -> None:
        if depth > 32:
            return
        for child in children.get(pid, []):
            if child in exclude:
                continue
            walk(child, depth + 1)
            ordered.append(child)

    walk(root, 0)
    return ordered


def game_name_from_xml(path: str) -> str | None:
    try:
        root = ET.parse(path).getroot()
    except Exception:
        return None
    for node in root.iter("name"):
        if node.text:
            return node.text.strip()
    return None


# ---------------------------------------------------------------------------
# emulator suspend / resume
# ---------------------------------------------------------------------------

class EmulatorFreezer:
    """SIGSTOP the emulator process tree, and always be able to undo it."""

    def __init__(self, launcher_pid: int | None, enabled: bool = True):
        self.launcher_pid = launcher_pid
        self.enabled = enabled
        self.stopped: list[int] = []

    def freeze(self) -> None:
        if not self.enabled or not self.launcher_pid:
            return
        exclude = ancestors_of(os.getpid())
        exclude.update(descendants_of(os.getpid(), set()))
        targets = descendants_of(self.launcher_pid, exclude)
        if not targets:
            log("freeze: no emulator children under pid %d" % self.launcher_pid)
            return
        # Record before stopping: if we die mid-way, --resume can clean up.
        try:
            with open(STOPPED_FILE, "w") as fh:
                fh.write("\n".join(str(p) for p in targets))
        except OSError:
            pass
        for pid in targets:
            try:
                os.kill(pid, signal.SIGSTOP)
                self.stopped.append(pid)
            except OSError as exc:
                log("freeze: cannot stop %d: %s" % (pid, exc))
        log("freeze: stopped %s" % self.stopped)
        # os.kill() returns as soon as the signal is queued, not when the target
        # is actually stopped. acquire() reads the live pan offset and needs the
        # emulator to have genuinely stopped flipping, so wait for state T
        # instead of guessing at a sleep.
        self._await_stopped()

    def _await_stopped(self, timeout: float = 1.0) -> None:
        deadline = time.time() + timeout
        pending = list(self.stopped)
        while pending and time.time() < deadline:
            pending = [pid for pid in pending if process_state(pid) not in ("T", None)]
            if pending:
                time.sleep(0.01)
        if pending:
            log("freeze: %s still not stopped after %.1fs" % (pending, timeout))

    def thaw(self) -> None:
        for pid in reversed(self.stopped):
            try:
                os.kill(pid, signal.SIGCONT)
            except OSError:
                pass
        if self.stopped:
            log("thaw: resumed %s" % self.stopped)
        self.stopped = []
        try:
            os.unlink(STOPPED_FILE)
        except OSError:
            pass


def resume_leftovers() -> int:
    """Safety net: SIGCONT anything a previous, crashed run left frozen."""
    try:
        with open(STOPPED_FILE, "r") as fh:
            pids = [int(x) for x in fh.read().split() if x.strip().isdigit()]
    except OSError:
        return 0
    count = 0
    for pid in reversed(pids):
        try:
            os.kill(pid, signal.SIGCONT)
            count += 1
        except OSError:
            pass
    try:
        os.unlink(STOPPED_FILE)
    except OSError:
        pass
    if count:
        log("resume: SIGCONT sent to %d process(es)" % count)
    return count


# ---------------------------------------------------------------------------
# framebuffer
# ---------------------------------------------------------------------------

FBIOGET_VSCREENINFO = 0x4600
FBIOPUT_VSCREENINFO = 0x4601
FBIOGET_FSCREENINFO = 0x4602
FBIOPAN_DISPLAY = 0x4606

_VAR_FMT_LEN = 160        # sizeof(struct fb_var_screeninfo) == 40 * __u32
_FIX_FMT_LEN = 80         # sizeof(struct fb_fix_screeninfo) on 64-bit


class Framebuffer:
    """Direct /dev/fb0 access -- no EGL, so it cannot fight the GPU driver."""

    def __init__(self, device: str = "/dev/fb0", force_pan: bool = True,
                 all_buffers: int = 0):
        self.path = device
        self.fd = os.open(device, os.O_RDWR)
        self.force_pan = force_pan
        self.all_buffers = all_buffers

        var = bytearray(_VAR_FMT_LEN)
        fcntl.ioctl(self.fd, FBIOGET_VSCREENINFO, var, True)
        self._var_raw = bytes(var)
        v = struct.unpack("40I", bytes(var))
        self.xres, self.yres = v[0], v[1]
        self.xres_virtual, self.yres_virtual = v[2], v[3]
        self.xoffset, self.yoffset = v[4], v[5]
        self.bpp = v[6]
        self.red = (v[8], v[9])
        self.green = (v[11], v[12])
        self.blue = (v[14], v[15])
        self.transp = (v[17], v[18])

        fix = bytearray(_FIX_FMT_LEN)
        fcntl.ioctl(self.fd, FBIOGET_FSCREENINFO, fix, True)
        self.smem_len = struct.unpack_from("I", bytes(fix), 24)[0]
        self.ypanstep = struct.unpack_from("H", bytes(fix), 42)[0]
        self.line_length = struct.unpack_from("I", bytes(fix), 48)[0]

        if self.bpp not in (16, 32):
            raise RuntimeError("unsupported framebuffer depth: %d bpp" % self.bpp)
        self.bytes_pp = self.bpp // 8
        if not self.line_length:
            self.line_length = self.xres_virtual * self.bytes_pp
        if not self.smem_len:
            self.smem_len = self.line_length * self.yres_virtual

        self.mm = mmap.mmap(self.fd, self.smem_len, mmap.MAP_SHARED,
                            mmap.PROT_READ | mmap.PROT_WRITE)

        self.frame_bytes = self.yres * self.line_length
        # How many screen-sized slots the virtual framebuffer can hold. On the
        # Brick this is 21 (1024x16384), and the emulator pans freely between
        # them -- which is why the visible slot must be resolved in acquire(),
        # after the emulator has been suspended, not here.
        self.slot_count = max(1, min(self.yres_virtual // max(1, self.yres),
                                     self.smem_len // max(1, self.frame_bytes)))
        self.visible_offset = self._clamp_offset(self.yoffset)
        self.entry_yoffset = self.yoffset

        # The call order in main() -- pad.open, freeze, wait_until_idle, grab,
        # acquire -- is load-bearing, and until now it lived only in a comment.
        # Painting before acquire() means painting into whichever slot happened
        # to be current when this object was constructed, which is exactly the
        # bug that made the guide render intermittently.
        self._acquired = False
        self._saved: dict[int, bytes] | None = None

    def _require_acquired(self, what: str) -> None:
        if not self._acquired:
            raise RuntimeError(
                "Framebuffer.%s() called before acquire(); the visible buffer "
                "slot is only knowable once the emulator is suspended" % what)

    def _clamp_offset(self, yoffset: int) -> int:
        offset = yoffset * self.line_length
        if offset < 0 or offset + self.frame_bytes > self.smem_len:
            return 0
        return offset

    def _read_yoffset(self) -> int:
        var = bytearray(self._var_raw)
        fcntl.ioctl(self.fd, FBIOGET_VSCREENINFO, var, True)
        self._var_raw = bytes(var)
        return struct.unpack_from("I", bytes(var), 5 * 4)[0]

    def _pan_to(self, yoffset: int) -> bool:
        var = bytearray(self._var_raw)
        struct.pack_into("I", var, 4 * 4, 0)          # xoffset
        struct.pack_into("I", var, 5 * 4, yoffset)    # yoffset
        struct.pack_into("I", var, 21 * 4, 0)         # activate = FB_ACTIVATE_NOW
        try:
            fcntl.ioctl(self.fd, FBIOPAN_DISPLAY, var, True)
            return True
        except OSError as exc:
            log("fb: pan to y=%d failed (%s)" % (yoffset, exc))
            return False

    # -- taking over the screen ---------------------------------------------

    def acquire(self) -> None:
        """
        Decide which buffer slot to paint into.

        MUST be called *after* the emulator is suspended. Before that the
        emulator is still page-flipping, so any offset we read is stale by the
        time we draw -- the original cause of "it freezes the game but nothing
        appears", intermittently, depending on which slot the emulator
        happened to stop on.
        """
        self.entry_yoffset = self._read_yoffset()
        target = self.entry_yoffset

        # Panning to a slot we choose is better than guessing which one is
        # live: FBIOPAN_DISPLAY *makes* our slot the visible one.
        if self.force_pan and self.ypanstep:
            if self._pan_to(0) and self._read_yoffset() == 0:
                target = 0
            else:
                target = self._read_yoffset()

        self.visible_offset = self._clamp_offset(target)
        self._acquired = True
        log("fb: acquire -- entry y=%d, drawing at y=%d (slot %d of %d)"
            % (self.entry_yoffset, target,
               self.visible_offset // max(1, self.frame_bytes),
               self.slot_count))

    def release(self) -> None:
        """Hand the pan offset back to the emulator."""
        if self.force_pan and self.ypanstep and \
                self.entry_yoffset != self.visible_offset // self.line_length:
            self._pan_to(self.entry_yoffset)

    # -- pixel format -------------------------------------------------------

    def masks(self):
        def mask(field):
            offset, length = field
            return ((1 << length) - 1) << offset if length else 0
        return (mask(self.red), mask(self.green), mask(self.blue),
                mask(self.transp))

    def surface_width(self) -> int:
        """Width that makes a pygame surface pitch match line_length exactly."""
        return self.line_length // self.bytes_pp

    def page_surface(self):
        """The Surface the viewer draws into, in this display's pixel format."""
        import pygame
        return pygame.Surface((self.surface_width(), self.yres), 0, self.bpp,
                              self.masks())

    def present(self, surface) -> None:
        self._require_acquired("present")
        self.blit(surface.get_buffer(), surface.get_pitch())

    # -- content ------------------------------------------------------------

    def save(self) -> None:
        self._require_acquired("save")
        self._saved = {off: self.mm[off:off + self.frame_bytes]
                       for off in self.target_offsets()}

    def restore(self) -> None:
        for off, data in (self._saved or {}).items():
            self.mm[off:off + self.frame_bytes] = data

    def target_offsets(self) -> list[int]:
        """Byte offsets to paint. Normally one; more if all_buffers is set."""
        if self.all_buffers <= 1:
            return [self.visible_offset]
        count = min(self.all_buffers, self.slot_count)
        return [n * self.frame_bytes for n in range(count)]

    def blit(self, buf, pitch: int | None = None) -> None:
        """Copy a full frame in. Fast path when the source pitch matches ours."""
        view = memoryview(buf).cast("B")
        for base in self.target_offsets():
            if pitch is None or pitch == self.line_length:
                chunk = view[:self.frame_bytes]
                self.mm[base:base + len(chunk)] = chunk
                continue
            # Pitches differ (16bpp surfaces get padded by SDL): row by row.
            row = min(pitch, self.line_length)
            for y in range(self.yres):
                src = y * pitch
                self.mm[base + y * self.line_length:
                        base + y * self.line_length + row] = view[src:src + row]

    def close(self) -> None:
        try:
            self.mm.close()
        except Exception:
            pass
        try:
            os.close(self.fd)
        except OSError:
            pass

    def describe(self) -> str:
        return ("%s %dx%d virt %dx%d @%d,%d %dbpp stride=%d smem=%d "
                "R%s G%s B%s A%s") % (
            self.path, self.xres, self.yres, self.xres_virtual,
            self.yres_virtual, self.xoffset, self.yoffset, self.bpp,
            self.line_length, self.smem_len, self.red, self.green,
            self.blue, self.transp)


class SdlOutput:
    """
    Fallback display path: draw through SDL2 instead of /dev/fb0.

    KNULLI's SDL2 on the Brick only has the "mali" (fbdev/EGL) video driver,
    so this creates a second fullscreen EGL surface. That is only safe because
    the emulator is suspended and therefore not flipping buffers itself. Use
    it by setting `renderer = sdl` in gameguide.conf if the direct
    framebuffer path shows nothing on your device.
    """

    def __init__(self):
        import pygame
        self.pygame = pygame
        os.environ.setdefault("SDL_NOMOUSE", "1")
        pygame.display.init()
        info = pygame.display.Info()
        self.screen = pygame.display.set_mode(
            (info.current_w, info.current_h), pygame.FULLSCREEN)
        pygame.mouse.set_visible(False)
        self.xres, self.yres = self.screen.get_size()
        self.bpp = self.screen.get_bitsize()
        self.driver = pygame.display.get_driver()

    def masks(self):
        return self.screen.get_masks()

    def surface_width(self) -> int:
        return self.xres

    def page_surface(self):
        return self.screen

    def present(self, surface) -> None:
        self.pygame.display.flip()

    def acquire(self) -> None:
        pass

    def release(self) -> None:
        pass

    def save(self) -> None:
        pass

    def restore(self) -> None:
        pass

    def close(self) -> None:
        try:
            self.pygame.display.quit()
        except Exception:
            pass

    def describe(self) -> str:
        return "SDL2 driver=%s %dx%d %dbpp" % (self.driver, self.xres,
                                               self.yres, self.bpp)


def open_output(conf: dict, override: str | None = None):
    """Pick the display backend named in the config, with a sane fallback."""
    wanted = str(override or conf.get("renderer", "fb")).strip().lower()
    if wanted == "sdl":
        return SdlOutput()
    # Deliberately no automatic fallback to SdlOutput. That path opens a second
    # EGL surface against a GPU the emulator still owns and has never run on any
    # hardware; quietly switching to it on an unrelated failure would turn a
    # clear error into an unpredictable one. Ask for it explicitly instead.
    return Framebuffer(conf.get("fbdev", "/dev/fb0"),
                       force_pan=conf_bool(conf, "fb_force_pan", True),
                       all_buffers=conf_int(conf, "fb_all_buffers", 0))


# ---------------------------------------------------------------------------
# input -- evdev, mapped through EmulationStation's own controller config
# ---------------------------------------------------------------------------

EVIOCGRAB = 0x40044590

# Linux input event codes we care about, so the viewer still works if
# es_input.cfg is missing or describes a different pad.
FALLBACK_BUTTONS = {
    304: "b",        # BTN_SOUTH / BTN_A
    305: "a",        # BTN_EAST  / BTN_B
    307: "y",        # BTN_WEST
    308: "x",        # BTN_NORTH
    310: "pageup",   # BTN_TL   (L1)
    311: "pagedown",  # BTN_TR   (R1)
    312: "l2",       # BTN_TL2
    313: "r2",       # BTN_TR2
    314: "select",   # BTN_SELECT
    315: "start",    # BTN_START
    316: "hotkey",   # BTN_MODE
    544: "up", 545: "down", 546: "left", 547: "right",  # BTN_DPAD_*
}

ABS_X, ABS_Y = 0x00, 0x01
ABS_RX, ABS_RY = 0x03, 0x04
ABS_HAT0X, ABS_HAT0Y = 0x10, 0x11
EV_KEY, EV_ABS = 0x01, 0x03


class PadMapping:
    """name -> how to recognise it, learned from es_input.cfg when possible."""

    def __init__(self):
        self.buttons: dict[int, str] = dict(FALLBACK_BUTTONS)
        self.hat_axes: dict[int, tuple[str, str]] = {
            ABS_HAT0X: ("left", "right"),
            ABS_HAT0Y: ("up", "down"),
        }
        self.stick_axes: dict[int, str] = {}     # abs code -> "vertical"
        self.device_names: list[str] = []
        self.source = "built-in defaults"

    @classmethod
    def load(cls, path: str = ES_INPUT_CFG) -> "PadMapping":
        mapping = cls()
        try:
            root = ET.parse(path).getroot()
        except Exception as exc:
            log("input: falling back to default codes (%s)" % exc)
            return mapping

        for cfg in root.findall("inputConfig"):
            if cfg.get("type") != "joystick":
                continue
            name = cfg.get("deviceName") or ""
            if name:
                mapping.device_names.append(name)
            for node in cfg.findall("input"):
                iname = node.get("name") or ""
                itype = node.get("type") or ""
                code = node.get("code")
                if itype == "button" and code and code.isdigit():
                    mapping.buttons[int(code)] = iname
                elif itype == "axis" and code and code.isdigit():
                    if iname in ("joystick1up", "joystick2up", "up"):
                        mapping.stick_axes[int(code)] = "vertical"
        if mapping.device_names:
            mapping.source = path
        # A d-pad reported as an axis is handled by the hat path, which gives
        # proper key repeat; don't also treat it as an analog stick.
        for code in list(mapping.stick_axes):
            if code in mapping.hat_axes:
                del mapping.stick_axes[code]
        return mapping


class PadReader:
    """Reads (and exclusively grabs) every gamepad-ish evdev node."""

    def __init__(self, mapping: PadMapping):
        self.mapping = mapping
        self.devices = []
        self.grabbed = []
        self.stale_keys: set[int] = set()
        # abs code -> (centre, span, deadzone) for analog scrolling
        self.axis_info: dict[int, tuple[float, float, float]] = {}

    def open(self) -> None:
        try:
            import evdev
        except ImportError as exc:
            raise RuntimeError("python-evdev is not available: %s" % exc)

        for path in sorted(evdev.list_devices()):
            try:
                dev = evdev.InputDevice(path)
            except OSError:
                continue
            caps = dev.capabilities(absinfo=True)
            keys = set(caps.get(EV_KEY, []))
            abs_entries = caps.get(EV_ABS, [])
            absi = {code for code, _ in abs_entries}
            looks_like_pad = bool(keys & set(self.mapping.buttons)) or \
                bool(absi & {ABS_HAT0X, ABS_HAT0Y})
            if not looks_like_pad:
                dev.close()
                continue
            for code, info in abs_entries:
                if code in self.mapping.stick_axes:
                    centre = (info.max + info.min) / 2.0
                    span = max(1.0, (info.max - info.min) / 2.0)
                    dead = max(info.flat or 0, span * 0.25)
                    self.axis_info[code] = (centre, span, dead)
            self.devices.append(dev)
        if not self.devices:
            raise RuntimeError("no input devices found")
        log("input: using %s" % [d.name for d in self.devices])

    # -- exclusive access ---------------------------------------------------
    #
    # Grabbing matters: without it every scroll press would sit in the frozen
    # emulator's event queue and be replayed into the game on resume.
    #
    # But a grab must only be taken while *nothing* is held down. The guide is
    # opened with MENU+SELECT still pressed; if we grabbed straight away we
    # would swallow both key-up events and the emulator would come back
    # believing the hotkey is still held, blocking all input until the player
    # tapped MENU again. So we wait for the pad to go idle first -- the
    # emulator is already suspended by then, so those releases simply queue up
    # for it and arrive correctly when it resumes.

    def pressed_keys(self) -> set[int]:
        active: set[int] = set()
        for dev in list(self.devices):
            try:
                active.update(dev.active_keys())
            except OSError:
                self._forget(dev)
        return {code for code in active if code in self.mapping.buttons}

    def wait_until_idle(self, timeout: float = 2.0) -> set[int]:
        """Block until no mapped button is held. Returns what was still held."""
        deadline = time.time() + timeout
        while True:
            self.poll(0.02)          # keep our own queue from going stale
            held = self.pressed_keys()
            if not held or time.time() >= deadline:
                return held

    def grab(self) -> None:
        self.stale_keys = self.pressed_keys()
        if self.stale_keys:
            log("input: grabbing with %s still held" % sorted(self.stale_keys))
        for dev in list(self.devices):
            try:
                fcntl.ioctl(dev.fd, EVIOCGRAB, 1)
                self.grabbed.append(dev)
            except OSError as exc:
                log("input: could not grab %s (%s) -- %s"
                    % (dev.path, dev.name, exc))
        self.poll(0.0)               # drop anything buffered before the grab

    def ungrab(self, settle: float = 1.5) -> None:
        for dev in self.grabbed:
            try:
                fcntl.ioctl(dev.fd, EVIOCGRAB, 0)
            except OSError:
                pass
        self.grabbed = []
        # If we had to grab over a held button, give its release a chance to
        # reach the (still suspended) emulator before we let it run again.
        if getattr(self, "stale_keys", None):
            deadline = time.time() + settle
            while self.pressed_keys() & self.stale_keys and \
                    time.time() < deadline:
                time.sleep(0.02)
            self.stale_keys = set()

    def axis_fraction(self, code: int, value: int) -> float:
        """Stick position as -1.0 .. 1.0, or 0.0 inside the dead zone."""
        info = self.axis_info.get(code)
        if not info:
            return 0.0
        centre, span, dead = info
        delta = value - centre
        if abs(delta) <= dead:
            return 0.0
        magnitude = (abs(delta) - dead) / max(1.0, span - dead)
        return min(1.0, magnitude) * (1.0 if delta > 0 else -1.0)

    def close(self) -> None:
        self.ungrab(settle=0.0)
        for dev in self.devices:
            try:
                dev.close()
            except Exception:
                pass
        self.devices = []

    def poll(self, timeout: float):
        """Return the raw evdev events that arrived within `timeout`."""
        if not self.devices:
            time.sleep(timeout)
            return []
        fds = {dev.fd: dev for dev in self.devices}
        try:
            ready, _, _ = select.select(list(fds), [], [], timeout)
        except (OSError, ValueError):
            # A controller was unplugged: drop whatever no longer works.
            self._drop_dead_devices()
            return []
        events = []
        for fd in ready:
            dev = fds[fd]
            try:
                events.extend(dev.read())
            except BlockingIOError:
                continue
            except OSError:
                self._forget(dev)
        return events

    def _forget(self, dev) -> None:
        log("input: lost %s" % getattr(dev, "path", "?"))
        for collection in (self.devices, self.grabbed):
            if dev in collection:
                collection.remove(dev)
        try:
            dev.close()
        except Exception:
            pass

    def _drop_dead_devices(self) -> None:
        for dev in list(self.devices):
            try:
                os.fstat(dev.fd)
            except OSError:
                self._forget(dev)


# ---------------------------------------------------------------------------
# guide discovery
# ---------------------------------------------------------------------------

def guide_candidates(rom: str, system: str | None, extra_dirs: list[str]):
    rom_dir = os.path.dirname(rom)
    base = os.path.basename(rom)
    stem = os.path.splitext(base)[0]

    seen = set()
    for path in (
        # ROCKNIX-compatible: guide next to the ROM, same name, .txt
        os.path.join(rom_dir, stem + ".txt"),
        os.path.join(rom_dir, base + ".txt"),
        # tidier: keep guides out of the gamelist's way
        os.path.join(rom_dir, "guides", stem + ".txt"),
        os.path.join(rom_dir, "guides", base + ".txt"),
    ):
        if path not in seen:
            seen.add(path)
            yield path

    for root in extra_dirs:
        root = root.strip()
        if not root:
            continue
        subdirs = [system, ""] if system else [""]
        for sub in subdirs:
            for leaf in (stem + ".txt", base + ".txt"):
                path = os.path.join(root, sub, leaf) if sub else \
                    os.path.join(root, leaf)
                path = os.path.normpath(path)
                if path not in seen:
                    seen.add(path)
                    yield path


def find_guide(rom: str, system: str | None, extra_dirs: list[str]):
    tried = []
    for path in guide_candidates(rom, system, extra_dirs):
        tried.append(path)
        if os.path.isfile(path):
            return path, tried
    return None, tried


def read_guide(path: str) -> list[str]:
    with open(path, "rb") as fh:
        raw = fh.read()
    for encoding in ("utf-8", "cp1252", "latin-1"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("latin-1", "replace")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    return [ln.expandtabs(8) for ln in lines]


# ---------------------------------------------------------------------------
# saved reading positions
# ---------------------------------------------------------------------------

def load_positions() -> dict:
    out = {}
    try:
        with open(POS_FILE, "r", encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                raw = raw.rstrip("\n")
                if "=" not in raw:
                    continue
                # The key is a full guide path, which may itself contain '='.
                # The value never does, so split from the right.
                key, _, val = raw.rpartition("=")
                parts = val.split(",")
                try:
                    scroll = int(parts[0])
                    size = int(parts[1]) if len(parts) > 1 else 0
                except ValueError:
                    continue
                out[key] = (max(0, scroll), size)
    except OSError:
        pass
    return out


def save_positions(positions: dict) -> None:
    tmp = POS_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            for key in sorted(positions):
                scroll, size = positions[key]
                fh.write("%s=%d,%d\n" % (key, scroll, size))
        os.replace(tmp, POS_FILE)
    except OSError as exc:
        log("positions: cannot save (%s)" % exc)


# ---------------------------------------------------------------------------
# the viewer
# ---------------------------------------------------------------------------

HELP_ROWS = [
    ("D-PAD UP/DOWN", "Scroll"),
    ("L1 / R1", "Page up / down"),
    ("D-PAD LEFT/RIGHT", "Text size - / +"),
    ("L2 / R2", "Jump to start / end"),
    ("SELECT", "Toggle this help"),
    ("A or B", "Close the guide"),
]

STICK_HELP_ROW = ("STICK UP/DOWN", "Scroll")


class Viewer:
    def __init__(self, conf: dict, out, lines: list[str],
                 title: str, position_key: str, positions: dict):
        import pygame

        self.pygame = pygame
        self.conf = conf
        self.out = out
        self.raw_lines = lines
        self.title = title
        self.position_key = position_key
        self.positions = positions

        self.fg = parse_colour(conf.get("fg"), (232, 232, 232))
        self.bg = parse_colour(conf.get("bg"), (16, 16, 16))
        self.accent = parse_colour(conf.get("accent"), (127, 178, 255))
        self.margin_x = conf_int(conf, "margin_x", 12)
        self.margin_y = conf_int(conf, "margin_y", 8)
        self.size_min = conf_int(conf, "font_size_min", 10)
        self.size_max = conf_int(conf, "font_size_max", 48)
        self.max_fps = max(5, conf_int(conf, "max_fps", 30))

        pygame.font.init()
        self.font_path = self._pick_font()

        saved_scroll, saved_size = positions.get(position_key, (0, 0))
        self.font_size = saved_size or conf_int(conf, "font_size", 20)
        self.font_size = max(self.size_min, min(self.size_max, self.font_size))

        self.surface = out.page_surface()
        self.width = out.xres
        self.height = out.yres

        self.font = None
        self.line_height = 1
        self.char_width = 1
        self.cols = 1
        self.rows = 1
        self.offsets: list[int] = []
        self.total_wrapped = 0
        self._cache: dict[int, object] = {}

        self._apply_font_size(self.font_size)

        self.scroll = min(saved_scroll, max(0, self.total_wrapped - 1))
        self.show_help = conf_bool(conf, "help_on_open", False)
        self.show_counter = conf_bool(conf, "show_line_counter", True)
        self.help_rows = list(HELP_ROWS)
        self.running = True
        self.dirty = True

    # -- font / layout ------------------------------------------------------

    def _pick_font(self) -> str:
        return pick_font(self.conf)

    def _apply_font_size(self, size: int) -> None:
        pygame = self.pygame
        self.font_size = max(self.size_min, min(self.size_max, size))
        self.font = pygame.font.Font(self.font_path, self.font_size)
        self.line_height = max(1, self.font.get_linesize())
        # Monospace assumption: measure a representative glyph run.
        probe = self.font.size("M" * 20)[0]
        self.char_width = max(1, probe // 20)

        usable_w = max(1, self.width - 2 * self.margin_x)
        usable_h = max(1, self.height - 2 * self.margin_y)
        self.cols = max(8, usable_w // self.char_width)
        self.rows = max(1, usable_h // self.line_height)

        self._rewrap()
        self._cache.clear()

    def _rewrap(self) -> None:
        """Character-pitch wrapping: O(n) integer maths, no font measuring."""
        cols = self.cols
        offsets = [0]
        total = 0
        for line in self.raw_lines:
            n = len(line)
            count = 1 if n == 0 else (n + cols - 1) // cols
            total += count
            offsets.append(total)
        self.offsets = offsets
        self.total_wrapped = total

    def _wrapped_line(self, index: int) -> str:
        """Map a wrapped-line index back to the right slice of a raw line."""
        if index < 0 or index >= self.total_wrapped:
            return ""
        raw_idx = bisect.bisect_right(self.offsets, index) - 1
        sub = index - self.offsets[raw_idx]
        line = self.raw_lines[raw_idx]
        start = sub * self.cols
        return line[start:start + self.cols]

    # -- rendering ----------------------------------------------------------

    def _line_surface(self, index: int):
        cached = self._cache.get(index)
        if cached is not None:
            return cached
        text = self._wrapped_line(index)
        surf = self.font.render(text, True, self.fg) if text else None
        if len(self._cache) > 512:
            self._cache.clear()
        self._cache[index] = surf
        return surf

    def _draw_help(self) -> None:
        pygame = self.pygame
        rows = self.help_rows
        pad = max(10, self.font_size // 2)
        line_h = self.line_height
        box_h = 2 * pad + (len(rows) + 2) * line_h
        left_w = max(self.font.size(k)[0] for k, _ in rows)
        right_w = max(self.font.size(v)[0] for _, v in rows)
        box_w = min(self.width - 2 * self.margin_x,
                    left_w + right_w + 3 * pad)
        box_x = (self.width - box_w) // 2
        box_y = max(self.margin_y, (self.height - box_h) // 2)

        panel = pygame.Rect(box_x, box_y, box_w, box_h)
        self.surface.fill((0, 0, 0), panel)
        pygame.draw.rect(self.surface, self.accent, panel, 2)

        y = box_y + pad
        header = self.font.render("GAME GUIDE  --  CONTROLS", True, self.accent)
        self.surface.blit(header, (box_x + pad, y))
        y += 2 * line_h
        for key, value in rows:
            self.surface.blit(self.font.render(key, True, self.accent),
                              (box_x + pad, y))
            self.surface.blit(self.font.render(value, True, self.fg),
                              (box_x + 2 * pad + left_w, y))
            y += line_h

    def _draw_counter(self) -> None:
        pygame = self.pygame
        first = min(self.scroll + 1, max(1, self.total_wrapped))
        text = "%d / %d" % (first, self.total_wrapped)
        surf = self.font.render(text, True, self.fg)
        x = self.width - surf.get_width() - self.margin_x - 6
        y = self.height - surf.get_height() - self.margin_y
        box = pygame.Rect(x - 6, y - 3, surf.get_width() + 12,
                          surf.get_height() + 6)
        self.surface.fill((0, 0, 0), box)
        self.surface.blit(surf, (x, y))

    def draw(self) -> None:
        self.surface.fill(self.bg)
        y = self.margin_y
        index = self.scroll
        for _ in range(self.rows):
            if index >= self.total_wrapped:
                break
            surf = self._line_surface(index)
            if surf is not None:
                self.surface.blit(surf, (self.margin_x, y))
            y += self.line_height
            index += 1
        if self.show_counter:
            self._draw_counter()
        if self.show_help:
            self._draw_help()
        self.out.present(self.surface)

    # -- navigation ---------------------------------------------------------

    def max_scroll(self) -> int:
        return max(0, self.total_wrapped - self.rows)

    def scroll_by(self, delta: int) -> None:
        new = max(0, min(self.max_scroll(), self.scroll + delta))
        if new != self.scroll:
            self.scroll = new
            self.dirty = True

    def scroll_to(self, value: int) -> None:
        new = max(0, min(self.max_scroll(), value))
        if new != self.scroll:
            self.scroll = new
            self.dirty = True

    def page_size(self) -> int:
        configured = conf_int(self.conf, "page_lines", 0)
        return configured if configured > 0 else max(1, self.rows - 2)

    def change_font_size(self, delta: int) -> None:
        old = self.font_size
        fraction = (self.scroll / self.total_wrapped) if self.total_wrapped else 0.0
        self._apply_font_size(self.font_size + delta)
        if self.font_size == old:
            return
        self.scroll = max(0, min(self.max_scroll(),
                                 int(fraction * self.total_wrapped)))
        self.dirty = True

    def save_position(self) -> None:
        self.positions[self.position_key] = (self.scroll, self.font_size)
        save_positions(self.positions)


# ---------------------------------------------------------------------------
# main loop
# ---------------------------------------------------------------------------

def run_viewer(conf: dict, out, pad: PadReader, lines: list[str],
               title: str, position_key: str) -> None:
    positions = load_positions()
    viewer = Viewer(conf, out, lines, title, position_key, positions)

    repeat_delay = conf_float(conf, "repeat_delay", 0.35)
    repeat_rate = conf_float(conf, "repeat_rate", 0.035)
    scroll_step = max(1, conf_int(conf, "scroll_lines", 1))
    frame_time = 1.0 / max(5, conf_int(conf, "max_fps", 30))

    if pad.axis_info:
        viewer.help_rows.insert(4, STICK_HELP_ROW)

    held: dict[str, float] = {}      # action -> next repeat timestamp
    stick: dict[int, float] = {}     # abs code -> normalised -1.0 .. 1.0
    stick_accumulator = 0.0

    def press(action: str) -> None:
        now = time.time()
        if action in ("up", "down", "pageup", "pagedown"):
            held[action] = now + repeat_delay
            _apply(action)
        elif action == "left":
            viewer.change_font_size(-2)
        elif action == "right":
            viewer.change_font_size(+2)
        elif action == "l2":
            viewer.scroll_to(0)
        elif action == "r2":
            viewer.scroll_to(viewer.max_scroll())
        elif action == "select":
            viewer.show_help = not viewer.show_help
            viewer.dirty = True
        elif action in ("a", "b"):
            viewer.running = False

    def release(action: str) -> None:
        held.pop(action, None)

    def _apply(action: str) -> None:
        if action == "up":
            viewer.scroll_by(-scroll_step)
        elif action == "down":
            viewer.scroll_by(+scroll_step)
        elif action == "pageup":
            viewer.scroll_by(-viewer.page_size())
        elif action == "pagedown":
            viewer.scroll_by(+viewer.page_size())

    mapping = pad.mapping
    hat_state: dict[int, int] = {}

    while viewer.running:
        if viewer.dirty:
            viewer.draw()
            viewer.dirty = False

        for ev in pad.poll(frame_time):
            if ev.type == EV_KEY:
                name = mapping.buttons.get(ev.code)
                if not name:
                    continue
                if ev.value == 1:
                    press(name)
                elif ev.value == 0:
                    release(name)
            elif ev.type == EV_ABS:
                if ev.code in mapping.hat_axes:
                    neg, pos = mapping.hat_axes[ev.code]
                    previous = hat_state.get(ev.code, 0)
                    value = -1 if ev.value < 0 else (1 if ev.value > 0 else 0)
                    if value == previous:
                        continue
                    hat_state[ev.code] = value
                    if previous < 0:
                        release(neg)
                    elif previous > 0:
                        release(pos)
                    if value < 0:
                        press(neg)
                    elif value > 0:
                        press(pos)
                elif ev.code in mapping.stick_axes:
                    stick[ev.code] = pad.axis_fraction(ev.code, ev.value)

        now = time.time()
        for action in list(held):
            if now >= held[action]:
                held[action] = now + repeat_rate
                _apply(action)

        # Analog scrolling: accumulate fractional lines so slow pushes still
        # move, and hard pushes run at about one screen per second.
        tilt = max(stick.values(), key=abs, default=0.0)
        if tilt:
            stick_accumulator += tilt * viewer.rows * frame_time
            whole = int(stick_accumulator)
            if whole:
                stick_accumulator -= whole
                viewer.scroll_by(whole)
        else:
            stick_accumulator = 0.0

    viewer.save_position()


def show_message(conf: dict, out, pad: PadReader, title: str,
                 body: list[str], timeout: float = 8.0) -> None:
    """A small modal used when there is no guide to show."""
    lines = [""] + [title, ""] + body
    positions = {}
    viewer = Viewer(conf, out, lines, title, "__message__", positions)
    viewer.show_counter = False
    viewer.draw()
    deadline = time.time() + timeout
    while time.time() < deadline:
        for ev in pad.poll(0.1):
            if ev.type == EV_KEY and ev.value == 1:
                name = pad.mapping.buttons.get(ev.code)
                if name in ("a", "b", "start", "select"):
                    return


# ---------------------------------------------------------------------------
# entry points
# ---------------------------------------------------------------------------

def diagnostics(conf: dict) -> int:
    print("== KNULLI Game Guides diagnostics ==")
    print("base dir       : %s" % BASE_DIR)
    print("config file    : %s (%s)"
          % (CONF_FILE, "present" if os.path.isfile(CONF_FILE) else "defaults"))
    print("renderer       : %s" % conf.get("renderer", "fb"))
    try:
        fb = Framebuffer(conf.get("fbdev", "/dev/fb0"))
        print("framebuffer    : %s" % fb.describe())
        fb.close()
    except Exception as exc:
        print("framebuffer    : FAILED -- %s" % exc)

    try:
        import pygame
        print("pygame         : %s" % pygame.version.ver)
        pygame.font.init()
        if hasattr(pygame.font, "get_sdl_ttf_version"):
            # returns a tuple, so it must be wrapped before %-formatting
            ttf = ".".join(str(n) for n in pygame.font.get_sdl_ttf_version())
        else:
            ttf = "n/a"
        print("SDL_ttf        : %s" % ttf)
    except Exception as exc:
        print("pygame         : FAILED -- %s" % exc)

    mapping = PadMapping.load()
    print("pad mapping    : from %s" % mapping.source)
    if mapping.device_names:
        print("pad devices    : %s" % ", ".join(mapping.device_names))
    named = sorted((code, name) for code, name in mapping.buttons.items())
    print("buttons        : %s"
          % ", ".join("%s=%d" % (name, code) for code, name in named))

    try:
        import evdev
        print("evdev nodes    :")
        for path in sorted(evdev.list_devices()):
            try:
                dev = evdev.InputDevice(path)
                print("                 %s  %s" % (path, dev.name))
                dev.close()
            except OSError:
                pass
    except Exception as exc:
        print("evdev          : FAILED -- %s" % exc)

    launcher = find_launcher_pid()
    if launcher:
        info = parse_launcher_args(read_cmdline(launcher))
        print("running game   : pid=%d system=%s rom=%s"
              % (launcher, info.get("system"), info.get("rom")))
        rom = info.get("rom")
        if rom:
            guide, tried = find_guide(rom, info.get("system"),
                                      conf.get("guide_dirs", "").split(":"))
            print("guide          : %s" % (guide or "NOT FOUND"))
            if not guide:
                for path in tried:
                    print("                 tried %s" % path)
    else:
        print("running game   : none (no emulatorlauncher process)")

    print()
    describe_evmapy_state()
    return 0


# ---------------------------------------------------------------------------
# hotkey binding (evmapy)
# ---------------------------------------------------------------------------

ANY_KEYS = "/userdata/system/configs/evmapy/any.keys"
LAUNCH_CMD = ("setsid /userdata/system/gameguide/gameguide-launch.sh "
              "</dev/null >/dev/null 2>&1 &")

# The names KNULLI's configgen understands; they come from es_input.cfg, which
# is why "l1"/"r1" are spelled pageup/pagedown.
VALID_TRIGGERS = {
    "a", "b", "x", "y", "start", "select", "hotkey",
    "pageup", "pagedown", "l2", "r2", "l3", "r3",
    "up", "down", "left", "right",
    "joystick1up", "joystick1down", "joystick2up", "joystick2down",
}
TRIGGER_ALIASES = {"l1": "pageup", "r1": "pagedown", "menu": "hotkey",
                   "north": "x", "south": "b", "east": "a", "west": "y"}


def parse_combo(text: str) -> list[str]:
    names = [t.strip().lower() for t in re.split(r"[+,\s]+", text) if t.strip()]
    resolved = [TRIGGER_ALIASES.get(n, n) for n in names]
    bad = [n for n in resolved if n not in VALID_TRIGGERS]
    if bad:
        raise ValueError("unknown button name(s): %s\nvalid names: %s"
                         % (", ".join(bad), ", ".join(sorted(VALID_TRIGGERS))))
    if not resolved:
        raise ValueError("no buttons given")
    if len(set(resolved)) != len(resolved):
        raise ValueError("the same button is listed twice")
    return resolved


def set_hotkey(combo: str, hold: float) -> int:
    """Rewrite our action in any.keys, leaving any other actions alone."""
    import json

    try:
        trigger = parse_combo(combo)
    except ValueError as exc:
        print("error: %s" % exc)
        return 1

    if len(trigger) == 1 and hold <= 0:
        print("refusing a single-button hotkey with no hold time: it would\n"
              "fire during normal play. Add --hold 1.0 or use two buttons.")
        return 1

    action = {
        "trigger": trigger,
        "type": "exec",
        "target": LAUNCH_CMD,
        "description": "Open the text guide for the running game",
    }
    if hold > 0:
        action["hold"] = hold

    data = {}
    if os.path.isfile(ANY_KEYS):
        try:
            with open(ANY_KEYS) as fh:
                data = json.load(fh)
        except Exception as exc:
            print("error: %s is not valid JSON (%s)" % (ANY_KEYS, exc))
            return 1
        # Copy rather than rename: renaming first leaves no any.keys at all if
        # anything goes wrong before the replacement is in place.
        try:
            shutil.copyfile(ANY_KEYS, ANY_KEYS + ".bak")
        except OSError:
            pass

    actions = [a for a in data.get("actions_player1", [])
               if a.get("target") != LAUNCH_CMD]
    actions.append(action)
    data["actions_player1"] = actions

    os.makedirs(os.path.dirname(ANY_KEYS), exist_ok=True)
    tmp = ANY_KEYS + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, ANY_KEYS)

    print("hotkey set to: %s%s"
          % (" + ".join(t.upper() for t in trigger),
             "  (hold %.2gs)" % hold if hold > 0 else "  (instant)"))
    print("written to   : %s" % ANY_KEYS)
    print("Relaunch the game -- the binding is applied when a game starts.")
    return 0


def describe_evmapy_state() -> None:
    """Show whether evmapy actually installed our action for this pad."""
    import json

    running = any("evmapy" in " ".join(read_cmdline(pid)) for pid in _pids())
    print("evmapy running : %s" % ("yes" if running else "NO"))

    if os.path.isfile(ANY_KEYS):
        try:
            with open(ANY_KEYS) as fh:
                data = json.load(fh)
            triggers = [a.get("trigger") for a in data.get("actions_player1", [])
                        if a.get("target") == LAUNCH_CMD]
            print("any.keys       : %s -> %s"
                  % (ANY_KEYS, triggers or "OUR ACTION IS MISSING"))
        except Exception as exc:
            print("any.keys       : INVALID JSON -- %s" % exc)
    else:
        print("any.keys       : MISSING (%s)" % ANY_KEYS)

    live = "/var/run/evmapy"
    if not os.path.isdir(live):
        print("live config    : %s does not exist (no game running?)" % live)
        return
    for name in sorted(os.listdir(live)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(live, name)
        try:
            with open(path) as fh:
                cfg = json.load(fh)
        except Exception as exc:
            print("live config    : %s unreadable -- %s" % (path, exc))
            continue
        acts = cfg.get("actions", [])
        print("live config    : %s (%d action(s), grab=%s)"
              % (path, len(acts), cfg.get("grab")))
        for a in acts:
            mark = " <-- guide" if a.get("target") == LAUNCH_CMD else ""
            print("                 %s %s hold=%s%s"
                  % (a.get("trigger"), a.get("type"), a.get("hold", 0), mark))


def probe_display(conf: dict, launcher_pid: int | None, seconds: float) -> int:
    """
    Work out empirically what actually reaches the panel.

    Suspends the emulator, then paints a big numbered page into each
    framebuffer slot in turn, and finally through SDL. Watch the screen and
    note which one you saw; the answer goes straight into gameguide.conf.
    """
    import pygame

    freezer = EmulatorFreezer(launcher_pid, conf_bool(conf, "stop_emulator", True))
    pygame.font.init()
    font_path = pick_font(conf)
    print("font: %s" % font_path)

    def paint(fb, slot, label, colour):
        surface = pygame.Surface((fb.surface_width(), fb.yres), 0, fb.bpp,
                                 fb.masks())
        surface.fill(colour)
        big = pygame.font.Font(font_path, max(28, fb.yres // 12))
        small = pygame.font.Font(font_path, max(16, fb.yres // 28))
        rows = [label, "", "if you can read this,", "write it down"]
        y = fb.yres // 5
        for i, text in enumerate(rows):
            font = big if i == 0 else small
            img = font.render(text, True, (0, 0, 0))
            surface.blit(img, ((fb.xres - img.get_width()) // 2, y))
            y += font.get_linesize() + 6
        base = slot * fb.frame_bytes
        fb.mm[base:base + fb.frame_bytes] = \
            memoryview(surface.get_buffer()).cast("B")[:fb.frame_bytes]

    print("Watch the device screen. Each step lasts %.1fs.\n" % seconds)
    try:
        freezer.freeze()
        fb = Framebuffer(conf.get("fbdev", "/dev/fb0"), force_pan=False)
        print("framebuffer: %s" % fb.describe())
        print("slots       : %d of %dx%d\n" % (fb.slot_count, fb.xres, fb.yres))

        slots = min(fb.slot_count, conf_int(conf, "probe_slots", 4))
        saved = {n: bytes(fb.mm[n * fb.frame_bytes:(n + 1) * fb.frame_bytes])
                 for n in range(slots)}
        palette = [(240, 90, 90), (90, 210, 120), (110, 150, 250), (240, 200, 90)]

        # 1. each slot, without touching the pan offset
        for n in range(slots):
            print("  showing: SLOT %d (no pan)" % n)
            paint(fb, n, "SLOT %d" % n, palette[n % len(palette)])
            time.sleep(seconds)

        # 2. slot 0 again, this time panning the display to it
        if fb.ypanstep:
            print("  showing: SLOT 0 + PAN")
            entry = fb._read_yoffset()
            fb._pan_to(0)
            paint(fb, 0, "SLOT 0 + PAN", (200, 120, 240))
            time.sleep(seconds)
            fb._pan_to(entry)
        else:
            print("  (driver reports ypanstep=0, skipping the pan test)")

        for n, data in saved.items():
            fb.mm[n * fb.frame_bytes:(n + 1) * fb.frame_bytes] = data
        fb.close()

        # 3. the SDL path
        print("  showing: SDL")
        sdl = SdlOutput()
        surface = sdl.page_surface()
        surface.fill((250, 250, 250))
        font = pygame.font.Font(font_path, max(28, sdl.yres // 12))
        img = font.render("SDL", True, (0, 0, 0))
        surface.blit(img, ((sdl.xres - img.get_width()) // 2, sdl.yres // 3))
        sdl.present(surface)
        time.sleep(seconds)
        sdl.close()
    except Exception as exc:
        import traceback
        log("probe: %s\n%s" % (exc, traceback.format_exc()))
        return 1
    finally:
        freezer.thaw()

    print("""
Now set the winner in /userdata/system/gameguide/gameguide.conf:

  saw "SLOT 0 + PAN"      -> renderer = fb     (the default; nothing to change)
  saw "SLOT n" for some n -> renderer = fb  and  fb_all_buffers = %d
                             (n+1 would do; %d covers every slot just shown)
  saw only "SDL"          -> renderer = sdl
  saw nothing at all      -> renderer = sdl, and tell me -- neither path
                             reaches the panel while an emulator holds it
""" % (slots, slots))
    return 0


def build_test_page(info: dict) -> list[str]:
    """A guide-shaped page used by --test to prove the whole path works."""
    ruler = "".join(str(i % 10) for i in range(1, 101))
    lines = [
        "=" * 78,
        "  KNULLI GAME GUIDES -- SELF TEST",
        "=" * 78,
        "",
        "If you can read this, the display path, the font and the gamepad",
        "are all working. Press A or B to close and return to the game.",
        "",
        "Detected right now:",
        "  system   : %s" % info.get("system", "(no game running)"),
        "  rom      : %s" % info.get("rom", "-"),
        "",
        "Column ruler -- with a monospaced font these digits line up exactly:",
        ruler,
        "|" + "-" * 76 + "|",
        "",
        "Try every control:",
        "  D-pad up/down     scroll this page",
        "  L1 / R1           jump a page at a time",
        "  D-pad left/right  make the text smaller / larger",
        "  L2 / R2           jump to the start / the end",
        "  Select            show the on-screen control list",
        "",
        "A box-drawing sample, as GameFAQs maps tend to look:",
        "",
        "    +--------+--------+--------+",
        "    | Item   | Where  | Notes  |",
        "    +--------+--------+--------+",
        "    | Bomb   | Cave 2 | hidden |",
        "    | Key    | Tower  | boss   |",
        "    +--------+--------+--------+",
        "",
    ]
    lines += ["Filler line %03d -- scroll down to check smooth movement." % n
              for n in range(1, 121)]
    lines += ["", "=== END OF TEST PAGE ===", ""]
    return lines


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="KNULLI in-game text guide viewer")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--run", action="store_true",
                       help="show the guide for the currently running game")
    group.add_argument("--show", metavar="FILE",
                       help="show an arbitrary text file")
    group.add_argument("--test", action="store_true",
                       help="show a built-in test page (checks the whole path "
                            "without needing a guide file)")
    group.add_argument("--resume", action="store_true",
                       help="SIGCONT anything a previous run left frozen")
    group.add_argument("--diag", action="store_true",
                       help="print environment diagnostics")
    group.add_argument("--set-hotkey", metavar="COMBO",
                       help='change the opening combo, e.g. '
                            '--set-hotkey "select+l2+r2"')
    group.add_argument("--probe", action="store_true",
                       help="paint a labelled page into every framebuffer slot "
                            "and through SDL, to find what reaches the panel")
    parser.add_argument("--hold", type=float, default=0.0,
                        help="seconds the combo must be held; default 0 "
                             "(instant), matching the shipped binding. Only "
                             "used with --set-hotkey")
    parser.add_argument("--renderer", choices=("fb", "sdl"),
                        help="override the configured display backend for "
                             "this run only")
    parser.add_argument("--probe-seconds", type=float, default=3.0,
                        help="how long each --probe step stays on screen")
    args = parser.parse_args(argv)

    conf = load_conf()

    if args.resume:
        resume_leftovers()
        return 0
    if args.diag:
        return diagnostics(conf)
    if args.set_hotkey:
        return set_hotkey(args.set_hotkey, args.hold)

    guide_dirs = [d for d in conf.get("guide_dirs", "").split(":") if d.strip()]

    launcher_pid = find_launcher_pid()
    info = parse_launcher_args(read_cmdline(launcher_pid)) if launcher_pid else {}

    if args.probe:
        return probe_display(conf, launcher_pid, args.probe_seconds)

    test_lines = None
    guide_path = None
    tried: list[str] = []

    if args.test:
        title = "Game Guide self-test"
        test_lines = build_test_page(info)
    elif args.show:
        guide_path = os.path.abspath(args.show)
        if not os.path.isfile(guide_path):
            log("show: no such file: %s" % guide_path)
            return 1
        title = os.path.basename(guide_path)
    else:
        rom = info.get("rom")
        if not rom:
            log("run: no running game found, nothing to do")
            return 0
        title = game_name_from_xml(info.get("gameinfoxml", "")) or \
            os.path.splitext(os.path.basename(rom))[0]
        guide_path, tried = find_guide(rom, info.get("system"), guide_dirs)

    freezer = EmulatorFreezer(launcher_pid,
                              conf_bool(conf, "stop_emulator", True))
    out = None
    pad = None
    exit_code = 0

    def emergency(signum, _frame):
        raise KeyboardInterrupt("signal %d" % signum)

    signal.signal(signal.SIGTERM, emergency)
    signal.signal(signal.SIGINT, emergency)
    signal.signal(signal.SIGHUP, emergency)

    try:
        pad = PadReader(PadMapping.load())
        out = open_output(conf, args.renderer)
        log("display: %s" % out.describe())

        # Order matters. Open the pad first (no grab), then suspend the
        # emulator, then wait for the launch combo to be released -- those
        # releases queue up for the suspended emulator and reach it intact.
        # Only then take the exclusive grab.
        pad.open()
        freezer.freeze()
        still_held = pad.wait_until_idle(timeout=2.0)
        if still_held:
            log("input: pad not idle after 2s, held=%s" % sorted(still_held))
        pad.grab()

        # Only now is it safe to work out which buffer slot is on screen: the
        # emulator has stopped flipping, so the answer will stay true.
        out.acquire()
        if conf_bool(conf, "restore_framebuffer", True):
            out.save()

        if test_lines is not None:
            run_viewer(conf, out, pad, test_lines, title, "__selftest__")
        elif guide_path:
            lines = read_guide(guide_path)
            log("open: %s (%d lines)" % (guide_path, len(lines)))
            run_viewer(conf, out, pad, lines, title, guide_path)
        else:
            log("open: no guide for %s" % title)
            body = [
                "No guide file was found for:",
                "",
                "    " + title,
                "",
                "Put a plain-text guide at any of these paths:",
                "",
            ] + ["    " + p for p in tried[:8]] + [
                "",
                "Press A or B to return to the game.",
            ]
            show_message(conf, out, pad, "GAME GUIDE", body)
    except KeyboardInterrupt:
        log("interrupted")
    except Exception as exc:  # noqa: BLE001 -- never leave the emulator frozen
        import traceback
        log("error: %s\n%s" % (exc, traceback.format_exc()))
        exit_code = 1
    finally:
        # From here on nothing may raise us out of the cleanup path. The ungrab
        # settle below can sleep over a second, and a SIGTERM landing in that
        # window would otherwise escape before thaw() runs, leaving the
        # emulator suspended with only the shell wrapper's trap to save it.
        for _sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            try:
                signal.signal(_sig, signal.SIG_IGN)
            except (OSError, ValueError):
                pass
        if out is not None:
            try:
                if conf_bool(conf, "restore_framebuffer", True):
                    out.restore()
                out.release()
            except Exception:
                pass
            out.close()
        # Release input before resuming, so any button still down reaches the
        # emulator as a clean release rather than a phantom held key.
        if pad is not None:
            try:
                pad.ungrab()
            except Exception:
                pass
        freezer.thaw()
        if pad is not None:
            pad.close()
    return exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
