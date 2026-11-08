#!/bin/sh
# Opt-in scheduler. Never rotates the existing one-shot backup directory.
set -eu
umask 077
root="${BACKUP_DIR:-/backups}/scheduled-v1"
if [ ! -d "$root" ]; then
    mkdir -p "$root"
    printf '%s\n' 'podzemelye-scheduled-v1' > "$root/.managed"
fi
if [ -L "$root" ] || [ "$(cat "$root/.managed" 2>/dev/null)" != 'podzemelye-scheduled-v1' ]; then
    echo 'Unknown scheduler directory; refusing backup and rotation.' >&2
    exit 1
fi
# Never clear a stale lock automatically after an unclean shutdown.
mkdir "$root/.lock" || { echo 'Scheduler already running or stale lock needs operator review.' >&2; exit 1; }
trap 'rmdir "$root/.lock"' EXIT
trap 'exit 1' HUP INT TERM
while :; do
    BACKUP_DIR="$root" sh "${BACKUP_SCRIPT:-/usr/local/bin/project-backup.sh}"
    if [ "${BACKUP_ROTATION_ENABLED:-0}" = 1 ]; then
        # Fixed generated basenames only, in the scheduler's own marked directory.
        # Failed dumps never reach this point. Original /backups/*.dump stay intact.
        find "$root" -maxdepth 1 -type f -name 'backup_????-??-??_??-??-??_??????.dump' \
            | LC_ALL=C sort -r | sed '1,7d' | while IFS= read -r old; do rm -- "$old"; done
    fi
    [ "${BACKUP_RUN_ONCE:-0}" = 1 ] && break
    sleep 86400 &
    wait "$!"
done
