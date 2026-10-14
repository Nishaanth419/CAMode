"""Create, list, verify and restore CA Mode database backups.

    python -m scripts.backup now              # back up now
    python -m scripts.backup list             # list backups, newest first
    python -m scripts.backup verify FILE      # integrity check and row counts
    python -m scripts.backup restore FILE     # stop the app first; the current database is kept aside

Uses BACKUP_DIR (default ~/ca-mode-backups) and APP_DATA_DIR like the app.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app import backup, db


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("now")
    commands.add_parser("list")
    for name in ("verify", "restore"):
        commands.add_parser(name).add_argument("file", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "now":
            print("Backed up to", backup.create_backup())
        elif args.command == "list":
            for path in backup.list_backups():
                print(f"{backup.backup_time(path):%Y-%m-%d %H:%M}  {path}")
            print(f"({backup.status()['count']} kept in {backup.backup_dir()})")
        elif args.command == "verify":
            counts = backup.verify_backup(args.file)
            print("OK:", ", ".join(f"{count} {table}" for table, count in counts.items()))
        else:
            aside = backup.restore_backup(args.file)
            print(f"Restored {args.file.name} to {db.DATABASE_PATH}. The previous database is at {aside}.")
            print("Start the app again; each profile unlocks with the PIN it had when the backup was made.")
    except backup.BackupError as exc:
        print("Error:", exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
