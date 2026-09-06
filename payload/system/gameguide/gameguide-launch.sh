#!/bin/sh
# KNULLI Game Guides -- hotkey entry point.
#
# Invoked by evmapy (see /userdata/system/configs/evmapy/any.keys) when the
# guide hotkey combo is pressed during a game.  Everything here exists to make
# sure the emulator is *never* left suspended, whatever happens to the viewer.
#
# SPDX-License-Identifier: MIT

BASE_DIR="$(dirname "$(readlink -f "$0")")"
VIEWER="${BASE_DIR}/gameguide.py"
LOG_FILE="${BASE_DIR}/gameguide.log"
LOCK_FILE="/var/run/gameguide.lock"
PYTHON="$(command -v python3 || echo /usr/bin/python3)"

# Log the invocation itself. If the hotkey ever seems dead, this line tells
# you whether evmapy fired at all or whether the viewer is the problem.
echo "$(date '+%Y-%m-%d %H:%M:%S') launch: hotkey fired" >>"${LOG_FILE}" 2>/dev/null

# Single instance: if a guide is already on screen, do nothing.  flock -n
# fails immediately rather than queueing a second viewer behind the first.
exec 9>"${LOCK_FILE}" 2>/dev/null || exit 0
flock -n 9 || exit 0

# Whatever happens next -- clean exit, crash, kill -9 of the viewer -- put the
# emulator back into the running state before we go away.
cleanup() {
    "${PYTHON}" "${VIEWER}" --resume >/dev/null 2>&1
}
trap 'cleanup' EXIT HUP INT TERM

"${PYTHON}" "${VIEWER}" --run "$@"
RC=$?

exit "${RC}"
