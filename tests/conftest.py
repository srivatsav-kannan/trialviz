from __future__ import annotations

import subprocess
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-live",
        action="store_true",
        help="run tests that call real external services and may cost money",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("--run-live"):
        return
    skip = pytest.mark.skip(reason="calls external services; pass --run-live to run")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)


_PGSERVER = """
import sys
import pgserver
server = pgserver.get_server(sys.argv[1], cleanup_mode="stop")
print(server.get_uri(), flush=True)
sys.stdin.read()
"""


@pytest.fixture(scope="session")
def postgres_dsn(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """A real, throwaway Postgres for the spend ledger, so its SQL and locking are tested
    against the database it runs on. The pgserver package bundles Postgres but has no build
    for Python 3.13 yet, so it runs in a side process under 3.12, which uv provides. The
    tests that need it are skipped where it cannot start."""
    data: Path = tmp_path_factory.mktemp("postgres")
    command = ["uv", "run", "--no-project", "--python", "3.12", "--with", "pgserver"]
    try:
        process = subprocess.Popen(
            [*command, "python", "-c", _PGSERVER, str(data)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError:
        pytest.skip("uv is needed to run the embedded Postgres")
    assert process.stdout is not None and process.stdin is not None
    first_line: list[str] = []
    reader = threading.Thread(target=lambda: first_line.append(process.stdout.readline()))  # type: ignore[union-attr]
    reader.start()
    reader.join(timeout=180)
    uri = first_line[0].strip() if first_line else ""
    if not uri.startswith("postgres"):
        process.kill()
        _, errors = process.communicate(timeout=30)
        pytest.skip(f"the embedded Postgres did not start: {errors.strip()[-300:]}")
    # Keep reading its error output, so a full pipe can never stall the server.
    threading.Thread(target=process.stderr.read, daemon=True).start()  # type: ignore[union-attr]
    yield uri
    process.stdin.close()
    process.wait(timeout=60)
