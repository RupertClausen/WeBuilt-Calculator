"""Manage team accounts from the server's terminal.

    python manage.py add-user USERNAME [--admin] [--name "Display name"]
    python manage.py reset-password USERNAME
    python manage.py set-role USERNAME admin|member
    python manage.py deactivate USERNAME
    python manage.py activate USERNAME
    python manage.py list-users

Passwords are asked for interactively and never shown, so they don't end up
in the shell history. Use this to create the first admin; after that, admins
can manage everyone from the Admin page.
"""

from __future__ import annotations

import argparse
import getpass
import sys

import auth


def _ask_password() -> str:
    while True:
        a = getpass.getpass("New password: ")
        b = getpass.getpass("Again: ")
        if a != b:
            print("They don't match; try again.")
            continue
        try:
            auth.check_new_password(a)
        except auth.AuthError as exc:
            print(exc)
            continue
        return a


def _find(username: str) -> dict:
    for u in auth.list_users():
        if u["username"] == username.strip().lower():
            return u
    raise SystemExit(f"No user called {username}.")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Manage WeBuilt Quote team accounts.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add-user")
    a.add_argument("username")
    a.add_argument("--admin", action="store_true")
    a.add_argument("--name", help="display name")
    sub.add_parser("reset-password").add_argument("username")
    r = sub.add_parser("set-role")
    r.add_argument("username")
    r.add_argument("role", choices=auth.ROLES)
    sub.add_parser("deactivate").add_argument("username")
    sub.add_parser("activate").add_argument("username")
    sub.add_parser("list-users")
    args = ap.parse_args(argv)

    try:
        if args.cmd == "add-user":
            u = auth.create_user(args.username, _ask_password(),
                                 "admin" if args.admin else "member", args.name)
            print(f"Created {u['username']} ({u['role']}).")
        elif args.cmd == "reset-password":
            u = _find(args.username)
            auth.update_user(u["id"], password=_ask_password())
            print(f"Password changed for {u['username']}; their sessions were signed out.")
        elif args.cmd == "set-role":
            u = auth.update_user(_find(args.username)["id"], role=args.role)
            print(f"{u['username']} is now {u['role']}.")
        elif args.cmd in ("deactivate", "activate"):
            u = auth.update_user(_find(args.username)["id"], active=args.cmd == "activate")
            print(f"{u['username']} is now {'active' if u['active'] else 'deactivated'}.")
        elif args.cmd == "list-users":
            users = auth.list_users()
            if not users:
                print("No users yet. Create the first admin with:\n"
                      "  python manage.py add-user YOURNAME --admin")
            for u in users:
                print(f"{u['username']:20} {u['role']:7} "
                      f"{'active' if u['active'] else 'OFF':7} {u['display_name'] or ''}")
    except auth.AuthError as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
