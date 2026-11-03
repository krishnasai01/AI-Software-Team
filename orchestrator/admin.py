"""Operator commands.

python -m orchestrator.admin add-product <name> <owner/repo> <sandbox-image> [--branch main] [--board <node id>]
python -m orchestrator.admin set-cap <role> <usd>
python -m orchestrator.admin enable|disable <role>
"""

import argparse

import psycopg

from core.settings import get_settings


def main() -> None:
    p = argparse.ArgumentParser(prog="orchestrator.admin")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add-product")
    a.add_argument("name")
    a.add_argument("repo")
    a.add_argument("image")
    a.add_argument("--branch", default="main")
    a.add_argument("--board", default=None, help="GitHub Projects v2 node id to mirror ticket state on")
    c = sub.add_parser("set-cap")
    c.add_argument("role")
    c.add_argument("usd", type=float)
    for name in ("enable", "disable"):
        sub.add_parser(name).add_argument("role")
    args = p.parse_args()

    with psycopg.connect(get_settings().database_url, autocommit=True) as conn:
        if args.cmd == "add-product":
            conn.execute(
                """INSERT INTO products (name, repo, sandbox_image, default_branch, project_board_id)
                   VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (name) DO UPDATE SET repo = EXCLUDED.repo, sandbox_image = EXCLUDED.sandbox_image,
                     default_branch = EXCLUDED.default_branch, project_board_id = EXCLUDED.project_board_id""",
                (args.name, args.repo, args.image, args.branch, args.board),
            )
        elif args.cmd == "set-cap":
            conn.execute("UPDATE agents SET daily_cap_usd = %s WHERE role = %s", (args.usd, args.role))
        else:
            conn.execute("UPDATE agents SET enabled = %s WHERE role = %s", (args.cmd == "enable", args.role))
    print("ok")


if __name__ == "__main__":
    main()
