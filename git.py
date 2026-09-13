#!/usr/bin/env python3
"""Plant backdated empty commits to fill the GitHub contribution graph.

GitHub counts a commit toward the graph when it is authored by a verified
email and lands on the repository's default branch. Each run writes one
commit per slot for the requested lookback window, backdated with both the
author and committer timestamps so the square renders on the right day.

Usage:
    python git.py                       # 366 days x 4 commits into this folder
    python git.py --days 90 --per-day 6
    python git.py --repo "D:/git green" --branch main --dry-run
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path

DEFAULT_DAYS = 365
DEFAULT_PER_DAY = 4
DEFAULT_BRANCH = "main"
DEFAULT_MESSAGE = "chore: update"
MAX_COMMITS = 5000  # guard against an accidental multi-hour run

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[33m"
DIM = "\033[2m"
RESET = "\033[0m"


class GitError(RuntimeError):
    """A git invocation failed or a precondition was not met."""


def enable_ansi() -> None:
    """Make the console accept ANSI colors and non-ASCII glyphs.

    Windows consoles default to a legacy code page (cp1252 here) which
    crashes on characters like the arrows and emoji in our output, so both
    streams are reconfigured to UTF-8 with a lossy fallback.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass  # not a real text stream (e.g. captured pipe on old Python)
    if os.name == "nt":
        os.system("")  # nudges conhost into ANSI mode; output is discarded


def paint(color: str, text: str) -> str:
    """Wrap text in a color, or return it untouched when stdout is not a tty."""
    if not sys.stdout.isatty():
        return text
    return f"{color}{text}{RESET}"


def run_git(args: list[str], cwd: Path, env: dict[str, str] | None = None) -> str:
    """Run git with *args*, returning stdout. Raises GitError on failure."""
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise GitError(f"`git {' '.join(args)}` failed: {detail or 'unknown error'}")
    return result.stdout.strip()


def ensure_repo(cwd: Path, branch: str, force: bool = False) -> None:
    """Verify *cwd* is a git repository, creating one when it is empty.

    A non-empty directory that is not already a repo is refused unless
    *force* is set, so the script cannot silently init inside a real project.
    """
    probe = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    if probe.returncode == 0:
        return
    if not cwd.is_dir():
        raise GitError(f"{cwd} is not a directory")
    if any(cwd.iterdir()) and not force:
        raise GitError(
            f"{cwd} is not a git repository and is not empty.\n"
            "  Re-run with --force to init anyway (a dedicated commits-only folder),\n"
            "  or point --repo at a real repository."
        )
    run_git(["init", f"--initial-branch={branch}"], cwd)


def resolve_identity(cwd: Path) -> tuple[str, str]:
    """Return the effective (name, email), or explain what is missing."""
    try:
        name = run_git(["config", "user.name"], cwd)
        email = run_git(["config", "user.email"], cwd)
    except GitError as exc:
        raise GitError(
            "git has no user.name / user.email configured. Set them with:\n"
            '  git config --global user.name "Your Name"\n'
            "  git config --global user.email you@example.com\n"
            f"(original error: {exc})"
        ) from exc
    if not name or not email:
        raise GitError("git user.name or user.email is empty; commits would be unattributed")
    return name, email


def iso_with_offset(moment: datetime) -> str:
    """Format *moment* as an ISO 8601 timestamp with an explicit UTC offset.

    Passing a naive timestamp makes git fall back to the machine zone, which
    can shift a commit onto the neighbouring day. A fixed 12:00 local slot
    keeps every commit inside the middle of its target day.
    """
    aware = moment.astimezone()
    return aware.strftime("%Y-%m-%dT%H:%M:%S%z")


def commit_slots(days: int, per_day: int, clock: datetime) -> Iterator[tuple[str, str]]:
    """Yield (day_label, timestamp) pairs, oldest first, stopping at now.

    Future days are skipped: GitHub drops backdates that point past today, so
    a slot on a not-yet-reached day is wasted work.
    """
    now = clock.astimezone()
    for offset in range(days, -1, -1):
        day = (now - timedelta(days=offset)).replace(
            hour=12, minute=0, second=0, microsecond=0
        )
        if day > now:
            continue
        label = day.strftime("%Y-%m-%d")
        for _ in range(per_day):
            yield label, iso_with_offset(day)


def plant(cwd: Path, slots: Iterator[tuple[str, str]], message: str, dry_run: bool) -> int:
    """Create one empty backdated commit per slot. Returns the commit count."""
    written = 0
    for label, timestamp in slots:
        if written >= MAX_COMMITS:
            print(paint(YELLOW, f"⚠ Stopped at the {MAX_COMMITS}-commit safety cap"))
            break
        if dry_run:
            written += 1
            continue
        env = {
            **os.environ,
            "GIT_AUTHOR_DATE": timestamp,
            "GIT_COMMITTER_DATE": timestamp,
        }
        run_git(["commit", "--allow-empty", "--quiet", "-m", message], cwd, env)
        written += 1
        if written % 100 == 0:
            print(paint(DIM, f"  {written} commits · {label}"))
    return written


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parent,
                        help="repository directory (default: alongside this script)")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS,
                        help=f"lookback window in days (default: {DEFAULT_DAYS})")
    parser.add_argument("--per-day", type=int, default=DEFAULT_PER_DAY,
                        help=f"commits per day (default: {DEFAULT_PER_DAY})")
    parser.add_argument("--branch", default=DEFAULT_BRANCH,
                        help=f"branch to commit on (default: {DEFAULT_BRANCH})")
    parser.add_argument("-m", "--message", default=DEFAULT_MESSAGE,
                        help=f"commit message (default: {DEFAULT_MESSAGE!r})")
    parser.add_argument("--dry-run", action="store_true",
                        help="count the commits that would be created, write nothing")
    parser.add_argument("--force", action="store_true",
                        help="git-init a non-empty directory if it is not already a repo")
    args = parser.parse_args(argv)

    if args.days < 1:
        parser.error("--days must be at least 1")
    if args.per_day < 1:
        parser.error("--per-day must be at least 1")
    return args


def main(argv: list[str] | None = None) -> int:
    enable_ansi()
    args = parse_args(argv)
    cwd: Path = args.repo.resolve()

    try:
        ensure_repo(cwd, args.branch, force=args.force)
        name, email = resolve_identity(cwd)
        try:
            run_git(["checkout", "-B", args.branch], cwd)
        except GitError:
            # Older git refuses checkout on an unborn HEAD; point it directly.
            run_git(["symbolic-ref", "HEAD", f"refs/heads/{args.branch}"], cwd)

        planned = sum(1 for _ in commit_slots(args.days, args.per_day, datetime.now()))
        label = "would plant" if args.dry_run else "planting"
        print(paint(GREEN, f"→ {label} {planned} commits in {cwd} "
                          f"as {name} <{email}> on '{args.branch}'"))

        written = plant(cwd, commit_slots(args.days, args.per_day, datetime.now()),
                        args.message, args.dry_run)
    except GitError as exc:
        print(paint(RED, f"✗ {exc}"), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(paint(RED, "\n✗ Interrupted - commits already written are kept"),
              file=sys.stderr)
        return 130

    verb = "Would have written" if args.dry_run else "Wrote"
    print(paint(GREEN, f"✅ {verb} {written} commits."))
    if not args.dry_run:
        print(paint(DIM, f"   Next: git push -u origin {args.branch} --force"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
