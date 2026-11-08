#!/bin/sh
# Write and validate a complete archive before making it visible as a backup.
set -eu
umask 077
backup_dir="${BACKUP_DIR:-/backups}"
mkdir -p "$backup_dir"
temporary=$(mktemp "$backup_dir/.incomplete.XXXXXX")
trap 'rm -f "$temporary"' EXIT HUP INT TERM
pg_dump --format=custom --file="$temporary"
pg_restore --list "$temporary" >/dev/null
name="backup_$(date -u +%Y-%m-%d_%H-%M-%S)_${temporary##*.}"
mv "$temporary" "$backup_dir/$name.dump"
trap - EXIT HUP INT TERM
printf '%s\n' "$name.dump"
