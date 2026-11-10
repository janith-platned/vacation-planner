"""Run the test suite against a throwaway embedded PostgreSQL.

Needs `pip install -r requirements-local.txt`. Use it when no PostgreSQL is installed;
the concurrency test is skipped on SQLite, so it must run against Postgres.

    python scripts/run_tests_local_pg.py [pytest args]
"""
import os
import subprocess
import sys
import tempfile
from urllib.parse import parse_qs, urlparse

import pgserver


def main() -> int:
    with tempfile.TemporaryDirectory() as data_dir:
        server = pgserver.get_server(data_dir)
        try:
            url = urlparse(server.get_uri())
            query = parse_qs(url.query)
            env = os.environ.copy()
            env.update(
                POSTGRES_DB=url.path.lstrip("/") or "postgres",
                POSTGRES_USER=url.username or "postgres",
                POSTGRES_PASSWORD=url.password or "",
                POSTGRES_HOST=query.get("host", [url.hostname or ""])[0],
                POSTGRES_PORT=str(url.port or ""),
            )
            return subprocess.call([sys.executable, "-m", "pytest", *sys.argv[1:]], env=env)
        finally:
            server.cleanup()


if __name__ == "__main__":
    sys.exit(main())
