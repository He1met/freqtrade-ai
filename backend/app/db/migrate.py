"""CLI entry point for the local PostgreSQL schema migration contract."""

from __future__ import annotations

import argparse
import sys

from app.db.migrations import upgrade_database, verify_schema
from app.db.session import create_database_engine


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate or verify the local PostgreSQL schema.")
    parser.add_argument("command", choices=("upgrade", "verify"))
    parser.add_argument("--database-url", required=True, help="SQLAlchemy PostgreSQL URL")
    args = parser.parse_args()
    engine = create_database_engine(args.database_url)

    if args.command == "upgrade":
        print(f"schema_version={upgrade_database(engine)}")
        return 0

    readiness = verify_schema(engine)
    print(f"database={readiness.database_identity}")
    print(f"schema_version={readiness.schema_version or '<missing>'}")
    for problem in readiness.problems:
        print(f"problem={problem}")
    return 0 if readiness.ready else 2


if __name__ == "__main__":
    sys.exit(main())
