"""Pytest configuration and fixtures for SKEIN tests."""

import os
import sys
import tempfile
import shutil
from pathlib import Path
from typing import Generator

import pytest

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from skein.storage import save_project_registry  # noqa: E402  (needs sys.path above)


def pytest_addoption(parser):
    # Registered at rootdir-level so the option is recognized regardless of
    # which test path is selected. The @pytest.mark.interactive consumer lives
    # under tests/test_signing/conftest.py but `pytest tests/conformance
    # --run-interactive` must still parse the flag without erroring.
    parser.addoption(
        "--run-interactive",
        action="store_true",
        default=False,
        help="Run tests marked @pytest.mark.interactive (require a human at the "
        "terminal to complete an OIDC handshake — never run in CI).",
    )


def pytest_configure(config):
    """Redirect SKEIN_HOME to a throwaway temp dir and seed the test-project
    registry THERE, so the suite never reads or writes the real
    ~/.skein/projects.json (issue-20260709-zl71: the old merge-write clobbered
    the live registry, deregistering all 50 projects). The redirect is an
    environment variable, not a monkeypatch, so a server spawned as a subprocess
    inherits it; it is set here, before any fixture runs, so every test — in
    process or subprocess — resolves the sandbox home.
    """
    test_home = Path(tempfile.mkdtemp(prefix="skein_home_test_"))
    os.environ["SKEIN_HOME"] = str(test_home)
    config._skein_test_home = test_home  # for pytest_unconfigure cleanup

    # Test project data dir (a fixed path other tests reference).
    test_project_dir = Path("/tmp/skein-test/.skein/data")
    test_project_dir.mkdir(parents=True, exist_ok=True)

    # Seed the registry in the sandbox home via the atomic writer.
    save_project_registry(
        {
            "projects": {
                "test-project": {
                    "data_dir": str(test_project_dir),
                    "name": "test-project",
                }
            }
        }
    )


def pytest_unconfigure(config):
    """Remove the throwaway SKEIN_HOME created in pytest_configure."""
    test_home = getattr(config, "_skein_test_home", None)
    if test_home is not None:
        shutil.rmtree(test_home, ignore_errors=True)
        os.environ.pop("SKEIN_HOME", None)


@pytest.fixture
def temp_project() -> Generator[Path, None, None]:
    """Create a temporary project directory with .skein folder.

    This fixture creates a mock project environment for testing
    SKEIN operations that require project-specific storage.
    """
    temp_dir = tempfile.mkdtemp(prefix="skein_test_")
    skein_dir = Path(temp_dir) / ".skein"
    skein_dir.mkdir()

    # Create basic project structure
    (skein_dir / "data").mkdir()

    yield Path(temp_dir)

    # Cleanup
    shutil.rmtree(temp_dir)


@pytest.fixture
def test_agent_id() -> str:
    """Return a consistent test agent ID."""
    return "test-agent-001"


@pytest.fixture
def test_project_id() -> str:
    """Return a consistent test project ID."""
    return "test-project"


@pytest.fixture
def test_headers(test_project_id: str, test_agent_id: str) -> dict:
    """Return standard test headers with project and agent IDs."""
    return {"X-Project-Id": test_project_id, "X-Agent-Id": test_agent_id}


@pytest.fixture
def base_url() -> str:
    """Return the base URL for the SKEIN API."""
    port = os.getenv("SKEIN_TEST_PORT", "8001")
    return f"http://localhost:{port}/skein"
