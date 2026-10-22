"""Show or change each profile's plan (there is no checkout: set it after someone pays).

    python -m scripts.plans list                        # every profile with its plan
    python -m scripts.plans set Asha individual         # by profile name, Google email or profile id
    python -m scripts.plans set asha@example.com team
    python -m scripts.plans set <profile-id> free       # back to the daily question limit

Uses APP_DATA_DIR (and the master key, to read Google emails) like the app. A name that
more than one profile uses (possible with Google sign-in) must be given as an email or id.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from app import accounts, plans
from app.db import connect_db, initialize_db


def _profiles() -> list[dict[str, Any]]:
    with connect_db() as connection:
        rows = connection.execute(
            "SELECT id, display_name, plan FROM users ORDER BY display_name COLLATE NOCASE",
        ).fetchall()
    profiles = []
    for row in rows:
        account = accounts.account_for_user(row["id"])
        profiles.append({
            "id": row["id"], "name": row["display_name"], "plan": row["plan"],
            "email": account["email"] if account else None,
        })
    return profiles


def find_profiles(who: str) -> list[dict[str, Any]]:
    """Profiles matching an id, a Google email or a display name (case-insensitive), in that order."""
    profiles = _profiles()
    wanted = who.strip().casefold()
    for field in ("id", "email", "name"):
        matches = [profile for profile in profiles if (profile[field] or "").casefold() == wanted]
        if matches:
            return matches
    return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="every profile with its plan")
    change = commands.add_parser("set", help="change one profile's plan")
    change.add_argument("profile", help="profile name, Google email or profile id")
    change.add_argument("plan", choices=plans.PLAN_IDS)
    args = parser.parse_args(argv)
    initialize_db()

    if args.command == "list":
        profiles = _profiles()
        if not profiles:
            print("No profiles yet.")
        for profile in profiles:
            print(f"{profile['plan']:<11} {profile['name']:<24} {profile['email'] or '-':<32} {profile['id']}")
        return 0

    matches = find_profiles(args.profile)
    if not matches:
        print(f"No profile called {args.profile!r}.", file=sys.stderr)
        return 1
    if len(matches) > 1:
        print(f"{len(matches)} profiles are called {args.profile!r}; give an email or id instead:", file=sys.stderr)
        for profile in matches:
            print(f"  {profile['email'] or '-':<32} {profile['id']}", file=sys.stderr)
        return 1
    profile = matches[0]
    plans.set_plan(profile["id"], args.plan)
    print(f"{profile['name']} is now on the {plans.PLANS[args.plan].name} plan.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
