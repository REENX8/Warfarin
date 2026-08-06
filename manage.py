#!/usr/bin/env python3
"""Operations CLI: migrations, admin accounts and manual job runs.

Usage:
    python manage.py migrate
    python manage.py status
    python manage.py createadmin <username> [--role admin]
    python manage.py passwd <username>
    python manage.py runjob <job-name>
    python manage.py jobs
"""
from __future__ import annotations

import argparse
import getpass
import sys

from warfarin import scheduler, staff
from warfarin.app_factory import configure_logging
from warfarin.config import get_settings
from warfarin.db import db, read_db
from warfarin.migrations import LATEST_VERSION, run_migrations, schema_version
from warfarin.patients import backfill_access_tokens
from warfarin.security import ROLES, hash_password, validate_password
from warfarin.time_utils import now


def cmd_migrate(_args) -> int:
    applied = run_migrations()
    if applied:
        print("Applied:", ", ".join(applied))
    else:
        print("Database already up to date.")
    with db() as conn:
        filled = backfill_access_tokens(conn)
    if filled:
        print(f"Generated portal tokens for {filled} patients.")
    print(f"Schema version: {schema_version()}/{LATEST_VERSION}")
    return 0


def cmd_status(_args) -> int:
    settings = get_settings()
    print(f"Environment      : {settings.env}")
    print(f"Database         : {settings.db_path}")
    print(f"Base URL         : {settings.base_url}")
    print(f"Schema version   : {schema_version()}/{LATEST_VERSION}")
    print(f"LINE push        : {'on' if settings.line_push_enabled else 'off'}")
    print(f"LINE webhook     : {'on' if settings.line_webhook_enabled else 'off'}")
    print(f"Scheduler        : {'on' if settings.enable_scheduler else 'off'}")
    with read_db() as conn:
        for table in ("patients", "medication_plan", "lab_results", "staff"):
            try:
                count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                print(f"{table:<17}: {count}")
            except Exception as exc:
                print(f"{table:<17}: unavailable ({exc})")
    return 0


def _prompt_password() -> str | None:
    password = getpass.getpass("New password: ")
    if password != getpass.getpass("Confirm password: "):
        print("Passwords do not match.", file=sys.stderr)
        return None
    error = validate_password(password)
    if error:
        print(error, file=sys.stderr)
        return None
    return password


def cmd_createadmin(args) -> int:
    run_migrations()
    password = _prompt_password()
    if password is None:
        return 1
    try:
        with db() as conn:
            staff.create_staff(
                conn, args.username, password, args.full_name or args.username,
                args.role, "cli",
            )
    except staff.StaffError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(f"Created {args.role} account '{args.username}'.")
    return 0


def cmd_passwd(args) -> int:
    password = _prompt_password()
    if password is None:
        return 1
    with db() as conn:
        account = staff.get_by_username(conn, args.username)
        if account is None:
            print(f"No such user: {args.username}", file=sys.stderr)
            return 1
        conn.execute(
            "UPDATE staff SET password_hash=?, must_change_password=0, updated_at=? "
            "WHERE staff_id=?",
            (hash_password(password), now(), account["staff_id"]),
        )
    from warfarin.security import destroy_sessions_for_user

    destroy_sessions_for_user(args.username)
    print(f"Password updated for '{args.username}'. Existing sessions were revoked.")
    return 0


def cmd_runjob(args) -> int:
    try:
        detail = scheduler.run_job(args.job)
    except KeyError:
        print(f"Unknown job '{args.job}'. Available: {', '.join(scheduler.JOBS)}",
              file=sys.stderr)
        return 1
    print(f"{args.job}: {detail}")
    return 0


def cmd_jobs(_args) -> int:
    for name in scheduler.JOBS:
        print(name)
    return 0


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("migrate", help="apply pending schema migrations")
    sub.add_parser("status", help="show configuration and row counts")
    sub.add_parser("jobs", help="list scheduled job names")

    create = sub.add_parser("createadmin", help="create a staff account")
    create.add_argument("username")
    create.add_argument("--role", default="admin", choices=sorted(ROLES))
    create.add_argument("--full-name", dest="full_name", default="")

    passwd = sub.add_parser("passwd", help="set a staff password")
    passwd.add_argument("username")

    runjob = sub.add_parser("runjob", help="run a scheduled job now")
    runjob.add_argument("job")

    args = parser.parse_args(argv)
    handlers = {
        "migrate": cmd_migrate,
        "status": cmd_status,
        "createadmin": cmd_createadmin,
        "passwd": cmd_passwd,
        "runjob": cmd_runjob,
        "jobs": cmd_jobs,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
