#!/bin/sh
# KNULLI Game Guides -- installer.
#
# Copies everything into the userdata (SHARE) partition. Nothing is written
# outside userdata, so a KNULLI update cannot remove it and no boot overlay is
# required.
#
# From a PC with the SD mounted:   ./install.sh /run/media/<you>/SHARE
# On the device:                   ./install.sh
#
# With no argument the destination is worked out from where you are standing:
# the current directory if it is a userdata root, else the nearest one above
# it, else /userdata. So copying this folder anywhere under userdata and
# running ./install.sh from it does the right thing.
#
# SPDX-License-Identifier: MIT

set -e

SRC="$(dirname "$(readlink -f "$0")")/payload"

# A userdata root has system/ and at least one of the things only userdata has.
is_userdata_root() {
    [ -d "${1}/system" ] || return 1
    [ -d "${1}/roms" ] || [ -f "${1}/system/knulli.conf" ] ||
        [ -f "${1}/system/batocera.conf" ] || return 1
    return 0
}

find_userdata_root() {
    dir="$(pwd -P)"
    while [ -n "${dir}" ] && [ "${dir}" != "/" ]; do
        if is_userdata_root "${dir}"; then
            printf '%s' "${dir}"
            return 0
        fi
        dir="$(dirname "${dir}")"
    done
    is_userdata_root "/userdata" && { printf '/userdata'; return 0; }
    return 1
}

if [ -n "${1}" ]; then
    DEST="${1}"
elif DEST="$(find_userdata_root)"; then
    echo ":: no path given -- detected userdata root at ${DEST}"
else
    echo "error: no path given and no KNULLI userdata root found here." >&2
    echo "       Run this from inside userdata on the device, or pass the" >&2
    echo "       path to the SHARE partition:" >&2
    echo "         ./install.sh /run/media/<you>/SHARE" >&2
    exit 1
fi

if [ ! -d "${SRC}" ]; then
    echo "error: payload directory not found at ${SRC}" >&2
    exit 1
fi
if [ ! -d "${DEST}" ]; then
    echo "error: destination ${DEST} does not exist" >&2
    exit 1
fi
if ! is_userdata_root "${DEST}"; then
    echo "error: ${DEST} does not look like a KNULLI userdata/SHARE partition" >&2
    echo "       (expected a 'system' folder plus roms/ or system/knulli.conf)" >&2
    exit 1
fi

PYTHON="$(command -v python3 || true)"

echo ":: installing to ${DEST}"

mkdir -p "${DEST}/system/gameguide" \
         "${DEST}/system/configs/evmapy" \
         "${DEST}/guides"

cp -f "${SRC}/system/gameguide/gameguide.py"         "${DEST}/system/gameguide/"
cp -f "${SRC}/system/gameguide/gameguide-launch.sh"  "${DEST}/system/gameguide/"
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
ES_INPUT="${DEST}/system/configs/emulationstation/es_input.cfg"

# Pick the combo from the controller this userdata actually belongs to. The
# shipped default (MENU+SELECT) is wrong on handhelds with no dedicated MENU
# button, where SELECT *is* the hotkey: KNULLI would merge the two into one
# event and evmapy would reject the whole key map.
if [ ! -f "${ANY_KEYS}" ] && [ -n "${PYTHON}" ] && [ -f "${ES_INPUT}" ]; then
    if "${PYTHON}" "${DEST}/system/gameguide/gameguide.py" \
            --userdata "${DEST}" --set-hotkey auto | sed 's/^/   /'; then
        echo ":: installed hotkey binding -> ${ANY_KEYS}"
    else
        cp -f "${NEW_KEYS}" "${ANY_KEYS}"
        echo ":: installed default hotkey binding -> ${ANY_KEYS}"
    fi
elif [ ! -f "${ANY_KEYS}" ]; then
    cp -f "${NEW_KEYS}" "${ANY_KEYS}"
    echo ":: installed default hotkey binding -> ${ANY_KEYS}"
    echo "   (no es_input.cfg here yet, so the controller is unknown. If"
    echo "    MENU+SELECT does nothing, run on the device:"
    echo "      python3 /userdata/system/gameguide/gameguide.py --set-hotkey auto)"
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
