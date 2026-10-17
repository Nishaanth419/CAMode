"""Show the anonymous page-view counts kept on this computer.

    python -m scripts.page_views              # totals per page for the last 30 days
    python -m scripts.page_views --days 7     # last 7 days (at most 90)
    python -m scripts.page_views --daily      # one line per day and page

Uses APP_DATA_DIR like the app. Counts hold only a date, a page name and a number.
"""

from __future__ import annotations

import argparse
import sys

from app import page_views, snapshots


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=30, help="days to include, today counted (1-90)")
    parser.add_argument("--daily", action="store_true", help="list each day instead of totals")
    args = parser.parse_args()
    rows = page_views.counts(days=args.days, today=snapshots.local_now().date())
    if not rows:
        print("No page views counted in that period.")
        return 0
    if args.daily:
        for row in rows:
            print(f"{row['day']}  {row['route']:<12} {row['views']:>6}")
    else:
        totals = page_views.totals(rows)
        for route, views in totals.items():
            print(f"{route:<12} {views:>6}")
        print(f"{'total':<12} {sum(totals.values()):>6}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
