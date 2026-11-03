"""Build the company-profile snapshot for the edinet-company-profiles skill.

    python tools/build_profiles.py all
    python tools/build_profiles.py cohort
    python tools/build_profiles.py filings --from 2025-05-01 --to 2026-11-02
    python tools/build_profiles.py profiles
    python tools/build_profiles.py peers
    python tools/build_profiles.py finish

Steps run in that order: the profiles need each company's annual-report document
ID, which only the filings scan provides. Every download is cached under
`--work`, so an interrupted run resumes where it stopped and a rebuild makes no
repeat calls.

The API key is `EDINET_API_KEY`, from the environment or from `.env` in the
repository root. It is never printed.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from edinet_util_mcp_server import api
from snapshot import steps
from snapshot.cache import Cache
from snapshot.schema import connect

REPO = Path(__file__).resolve().parents[1]
DEFAULT_DB = REPO / "skill" / "edinet-company-profiles" / "data" / "edinet_profiles.sqlite"
DEFAULT_WORK = REPO / "build" / "edinet-cache"
JST = timezone(timedelta(hours=9), "JST")
# A year of filings, plus 3 months for the filing deadline and late filers, plus a
# 3-month safety buffer.
WINDOW_MONTHS = 18
STEPS = ("cohort", "filings", "profiles", "peers", "finish")


def main(argv: list[str] | None = None) -> None:
    # EDINET's calendar is Japan time.
    today = datetime.now(JST).date()
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("step", choices=(*STEPS, "all"))
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--work", type=Path, default=DEFAULT_WORK)
    parser.add_argument("--env-file", type=Path, default=REPO / ".env")
    parser.add_argument("--from", dest="start", type=date.fromisoformat,
                        default=_months_back(today, WINDOW_MONTHS),
                        help=f"first daily list (default: {WINDOW_MONTHS} months back, so "
                             "every company's latest annual report falls inside)")
    parser.add_argument("--to", dest="end", type=date.fromisoformat,
                        default=today - timedelta(days=1))
    parser.add_argument("--refresh-lists", action="store_true",
                        help="fetch the daily lists again even when cached")
    parser.add_argument("--offline", action="store_true",
                        help="use cached downloads only; make no API calls")
    parser.add_argument("--top-n", type=int, default=10)
    parser.add_argument("--provider", default="GotoAI",
                        help="the publisher named in the processing notice")
    args = parser.parse_args(argv)

    cache = Cache(args.work)
    db = connect(args.db)
    client = None if args.offline else api.EdinetClient(api.load_api_key(args.env_file))
    started = time.monotonic()

    def log(message: str) -> None:
        elapsed = time.monotonic() - started
        print(f"[{datetime.now(JST):%H:%M:%S} +{elapsed:5.0f}s] {message}", flush=True)

    try:
        for step in STEPS if args.step == "all" else (args.step,):
            if step == "cohort":
                steps.cohort(db, cache, today=today, log=log)
            elif step == "filings":
                steps.filings(db, cache, client, start=args.start, end=args.end,
                              refresh=args.refresh_lists, log=log)
            elif step == "profiles":
                steps.profiles(db, cache, client, log=log)
            elif step == "peers":
                steps.peers(db, cache, top_n=args.top_n, log=log)
            elif step == "finish":
                steps.finish(db, provider=args.provider, today=today, log=log)
    finally:
        db.close()
        if client:
            log(f"done; {client.requests} API calls; database {args.db}")
            client.close()


def _months_back(day: date, months: int) -> date:
    month = day.month - months
    year = day.year + (month - 1) // 12
    return date(year, (month - 1) % 12 + 1, 1)


if __name__ == "__main__":
    main()
