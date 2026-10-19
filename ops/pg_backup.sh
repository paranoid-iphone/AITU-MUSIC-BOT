#!/bin/sh
# Periodic pg_dump of the club database with rotation. Runs in the postgres image (compose service `backup`).
#
#   pg_backup.sh        dump now, then every BACKUP_INTERVAL_HOURS (default 24)
#   pg_backup.sh once   one dump and exit (a manual backup before something risky)
#
# Connection comes from the usual PGHOST/PGUSER/PGPASSWORD/PGDATABASE variables. Dumps are custom-format
# (`pg_restore`) files in BACKUP_DIR; the ones older than BACKUP_KEEP_DAYS (default 14) are removed, but only after a
# new dump succeeded, so a failing database never eats the backups that are left.
set -u

BACKUP_DIR=${BACKUP_DIR:-/backups}
INTERVAL_HOURS=${BACKUP_INTERVAL_HOURS:-24}
KEEP_DAYS=${BACKUP_KEEP_DAYS:-14}

case "$INTERVAL_HOURS$KEEP_DAYS" in
  *[!0-9]* | "") echo "BACKUP_INTERVAL_HOURS and BACKUP_KEEP_DAYS must be whole numbers" >&2; exit 2 ;;
esac

backup_once() {
  name="${PGDATABASE}-$(date -u +%Y%m%d-%H%M%SZ).dump"
  partial="$BACKUP_DIR/.$name.partial"
  rm -f "$BACKUP_DIR"/.*.partial  # left behind by a dump that was killed half way
  if ! pg_dump -Fc -f "$partial"; then
    rm -f "$partial"
    echo "backup failed: pg_dump exited with an error" >&2
    return 1
  fi
  mv "$partial" "$BACKUP_DIR/$name"
  echo "backup written: $name ($(wc -c < "$BACKUP_DIR/$name") bytes)"

  removed=0
  for old in $(find "$BACKUP_DIR" -maxdepth 1 -name '*.dump' -mtime +"$KEEP_DAYS"); do
    rm -f "$old" && removed=$((removed + 1))
  done
  echo "rotation: removed $removed dump(s) older than $KEEP_DAYS day(s)"
}

if [ "${1:-}" = "once" ]; then
  backup_once
  exit $?
fi

while true; do
  backup_once || true  # a failed dump is logged and retried at the next interval
  sleep $((INTERVAL_HOURS * 3600))
done
