#!/bin/sh
# KNULLI Game Guides -- installer.
#
# Copies everything into the userdata (SHARE) partition. Nothing is written
# outside userdata, so a KNULLI update cannot remove it and no boot overlay is
# required.
#
# On the device (over SSH):        ./install.sh
# From a PC with the SD mounted:   ./install.sh /run/media/<you>/SHARE
#
# SPDX-License-Identifier: MIT

set -e

SRC="$(dirname "$(readlink -f "$0")")/payload"
DEST="${1:-/userdata}"

if [ ! -d "${SRC}" ]; then
    echo "error: payload directory not found at ${SRC}" >&2
    exit 1
fi
if [ ! -d "${DEST}" ]; then
    echo "error: destination ${DEST} does not exist" >&2
    exit 1
fi
if [ ! -d "${DEST}/system" ]; then
    echo "error: ${DEST} does not look like a KNULLI userdata/SHARE partition" >&2
    echo "       (no 'system' folder inside it)" >&2
    exit 1
fi

PYTHON="$(command -v python3 || true)"

echo ":: installing to ${DEST}"

mkdir -p "${DEST}/system/gameguide" \
         "${DEST}/system/configs/evmapy" \
         "${DEST}/guides"

cp -f "${SRC}/system/gameguide/gameguide.py"         "${DEST}/system/gameguide/"
cp -f "${SRC}/system/gameguide/gameguide-launch.sh"  "${DEST}/system/gameguide/"
cp -f "${SRC}/system/gameguide/README.md"            "${DEST}/system/gameguide/" 2>/dev/null || true
cp -f "${SRC}/guides/README.txt"                     "${DEST}/guides/" 2>/dev/null || true

# Never clobber a config the user has already tuned.
if [ -f "${DEST}/system/gameguide/gameguide.conf" ]; then
    cp -f "${SRC}/system/gameguide/gameguide.conf" \
          "${DEST}/system/gameguide/gameguide.conf.new"
    echo ":: kept your gameguide.conf (new default saved as gameguide.conf.new)"
else
    cp -f "${SRC}/system/gameguide/gameguide.conf" "${DEST}/system/gameguide/"
fi

# The SHARE partition is exFAT, which has no permission bits -- KNULLI mounts
# it with fmask=0022 so everything is already 0755. The chmod is here for
# ext4-formatted userdata partitions, and must not abort the install if the
# filesystem rejects it.
chmod 0755 "${DEST}/system/gameguide/gameguide.py" \
           "${DEST}/system/gameguide/gameguide-launch.sh" 2>/dev/null || true

# ---------------------------------------------------------------------------
# evmapy hotkey binding
#
# any.keys is merged by KNULLI's configgen into every emulator's key map, so
# one file covers every system. If the user already has one, merge into it
# instead of overwriting.
# ---------------------------------------------------------------------------
ANY_KEYS="${DEST}/system/configs/evmapy/any.keys"
NEW_KEYS="${SRC}/system/configs/evmapy/any.keys"

if [ ! -f "${ANY_KEYS}" ]; then
    cp -f "${NEW_KEYS}" "${ANY_KEYS}"
    echo ":: installed hotkey binding -> ${ANY_KEYS}"
elif [ -n "${PYTHON}" ]; then
    "${PYTHON}" - "${ANY_KEYS}" "${NEW_KEYS}" <<'PYEOF'
import json, shutil, sys

existing_path, new_path = sys.argv[1], sys.argv[2]
with open(new_path) as fh:
    new = json.load(fh)
try:
    with open(existing_path) as fh:
        existing = json.load(fh)
except Exception as exc:
    print("   ! %s is not valid JSON (%s); leaving it alone." % (existing_path, exc))
    print("     Add this action to it by hand:")
    print(json.dumps(new, indent=2))
    sys.exit(0)

# Identify our action by its target, exactly as gameguide.py --set-hotkey
# does. Matching on trigger instead would miss an action whose combo the user
# has since changed with the tool, and append a second binding beside it.
def is_ours(action):
    return "gameguide-launch.sh" in str(action.get("target", ""))

changed = False
for group, actions in new.items():
    current = existing.setdefault(group, [])
    if any(is_ours(a) for a in current):
        continue                      # already bound, whatever combo they chose
    current.extend(actions)
    changed = True

if changed:
    shutil.copyfile(existing_path, existing_path + ".bak")
    with open(existing_path, "w") as fh:
        json.dump(existing, fh, indent=2)
        fh.write("\n")
    print("   merged the guide hotkey into your existing any.keys "
          "(backup: any.keys.bak)")
else:
    trig = [a.get("trigger") for a in existing.get("actions_player1", [])
            if is_ours(a)]
    print("   hotkey already bound in any.keys as %s, left alone"
          % (trig[0] if trig else "?"))
PYEOF
else
    echo "   ! ${ANY_KEYS} already exists and python3 is unavailable here."
    echo "     Merge the action from ${NEW_KEYS} into it by hand."
fi

echo
echo ":: done."
echo "   Hotkey        : MENU + SELECT while a game is running"
echo "   Guides folder : ${DEST}/guides/<system>/<game name>.txt"
echo "                   (or just drop <game name>.txt next to the ROM)"
echo
echo "   The hotkey is picked up the next time you launch a game."
echo "   Check the install with:  python3 ${DEST}/system/gameguide/gameguide.py --diag"
echo "   Change the combo with :  python3 ${DEST}/system/gameguide/gameguide.py --set-hotkey \"select+l2+r2\""
