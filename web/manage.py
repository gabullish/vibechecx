#!/usr/bin/env python3
"""web/manage.py — CLI admin tool for VibeChecx.

Usage:
  python3 web/manage.py create-user <username> <password>
  python3 web/manage.py make-admin <username>
  python3 web/manage.py list-users
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vibechecx_config import DB_CONFIG
from vibechecx_auth import register

import psycopg2
from psycopg2.extras import RealDictCursor


def _conn():
    return psycopg2.connect(**DB_CONFIG, cursor_factory=RealDictCursor)


def cmd_create_user(username, password):
    ok, err = register(username, password)
    if ok:
        print(f"Created user: {username}")
    else:
        print(f"Error: {err}", file=sys.stderr)
        sys.exit(1)


def cmd_make_admin(username):
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE users SET is_admin=TRUE WHERE username=%s RETURNING id",
            (username,),
        )
        row = cur.fetchone()
    if row:
        print(f"{username} is now an admin (id={row['id']})")
    else:
        print(f"User not found: {username}", file=sys.stderr)
        sys.exit(1)


def cmd_list_users():
    with _conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, username, is_admin, created_at FROM users ORDER BY id"
        )
        rows = cur.fetchall()
    if not rows:
        print("No users.")
        return
    for r in rows:
        admin = " [admin]" if r["is_admin"] else ""
        print(f"  {r['id']:4d}  {r['username']:<24} {r['created_at'].strftime('%Y-%m-%d')}{admin}")


COMMANDS = {
    "create-user": (cmd_create_user, 2, "<username> <password>"),
    "make-admin":  (cmd_make_admin,  1, "<username>"),
    "list-users":  (cmd_list_users,  0, ""),
}

if __name__ == "__main__":
    args = sys.argv[1:]
    if not args or args[0] not in COMMANDS:
        print("Usage:")
        for name, (_, n, sig) in COMMANDS.items():
            print(f"  python3 web/manage.py {name} {sig}")
        sys.exit(1)
    cmd, (fn, n_args, _) = args[0], COMMANDS[args[0]]
    rest = args[1:]
    if len(rest) != n_args:
        _, _, sig = COMMANDS[cmd]
        print(f"Usage: python3 web/manage.py {cmd} {sig}", file=sys.stderr)
        sys.exit(1)
    fn(*rest)
