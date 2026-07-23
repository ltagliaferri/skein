"""Packaging regressions for the public `interskein` wheel.

The wheel is the whole public install path: no checkout, no Makefile, no
systemd unit. Anything the CLI needs at runtime has to be *inside* it. These
tests pin the three things that have to survive a build — the console scripts,
the documentation `skein info` serves, and the importable service module — so a
packaging change cannot quietly ship a wheel that installs but does not work.
"""

import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGE_ROOT = REPO_ROOT / "skein"


def _read_pyproject() -> dict:
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.10 has no tomllib
        tomllib = pytest.importorskip("tomli", reason="no TOML parser on this Python")

    with open(REPO_ROOT / "pyproject.toml", "rb") as f:
        return tomllib.load(f)


class TestConsoleScripts:
    """The install has to hand the user runnable commands."""

    def test_declares_cli_and_service_entrypoints(self):
        scripts = _read_pyproject()["project"]["scripts"]
        assert scripts["skein"] == "client.cli:main"
        assert scripts["mesh"] == "skein.mesh.cli:main"
        # Without this, a wheel user has no way to run the API service: the
        # source-tree launcher (skein_server.py) is not in the distribution.
        assert scripts["skein-server"] == "skein.server:main"

    def test_service_entrypoint_is_importable_and_callable(self):
        from skein import server

        assert callable(server.main)

    def test_service_module_runs_as_a_module(self):
        """`skein service start` launches `python -m skein.server`."""
        result = subprocess.run(
            [sys.executable, "-m", "skein.server", "--version"],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            timeout=120,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip()


class TestSupervisorUnit:
    """The systemd unit is how a service stays up. It broke the wheel case twice:
    first by hardcoding a checkout path (the defect that made someone write a
    process supervisor in Python instead of fixing one line of ini), then by
    living in systemd/ — not package data — so a wheel user had no copy to render.
    It now ships under skein/units/."""

    UNIT = PACKAGE_ROOT / "units" / "skein.service"

    def test_unit_is_under_the_package(self):
        # If it is not under skein/, it is not in the wheel, and a wheel user is
        # back to "copy it from a repository you do not have".
        assert self.UNIT.exists(), f"{self.UNIT} is missing"

    def test_unit_invokes_the_console_script(self):
        assert "ExecStart=__SKEIN_SERVER__" in self.UNIT.read_text()

    def test_unit_does_not_depend_on_a_checkout(self):
        body = self.UNIT.read_text()
        assert "WorkingDirectory" not in body
        assert "skein_server.py" not in body
        assert "__PYTHON__" not in body

    def test_print_unit_resolves_the_placeholder_to_an_absolute_execstart(self):
        # `skein-server --print-unit` is how the unit gets rendered — by the
        # service itself, so it works for an isolated uv-tool/pipx install a
        # bare `python -c "import skein"` could not import. A bare `skein-server`
        # ExecStart would not resolve under a systemd user unit's PATH on older
        # systemd, so the rendered path must be absolute.
        import re

        from skein.server import render_unit

        rendered = render_unit()
        assert "__SKEIN_SERVER__" not in rendered
        exec_line = next(ln for ln in rendered.splitlines() if ln.startswith("ExecStart="))
        # Either an absolute console-script path, or the `<python> -m` fallback,
        # which is also absolute.
        assert re.match(r"^ExecStart=(/|\S+/python)", exec_line), exec_line

    def test_print_unit_cli_prints_the_rendered_unit(self):
        result = subprocess.run(
            [sys.executable, "-m", "skein.server", "--print-unit"],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            timeout=120,
        )
        assert result.returncode == 0, result.stderr
        assert "ExecStart=" in result.stdout
        assert "__SKEIN_SERVER__" not in result.stdout

    def test_package_data_declares_the_unit(self):
        package_data = _read_pyproject()["tool"]["setuptools"]["package-data"]["skein"]
        assert "units/*" in package_data

    def test_makefile_renders_via_print_unit(self):
        makefile = (REPO_ROOT / "Makefile").read_text()
        assert "skein-server --print-unit" in makefile


class TestPackagedDocs:
    """`skein info` reads from the package, not from a checkout."""

    def test_every_info_topic_resolves_inside_the_package(self):
        from client.cli import INFO_TOPICS, packaged_docs_dir, resolve_doc

        for topic in INFO_TOPICS:
            resolved = resolve_doc(topic)
            assert resolved is not None, f"no document for topic '{topic}'"
            assert (
                resolved.parent == packaged_docs_dir()
            ), f"topic '{topic}' resolved to {resolved}, outside the package"
            assert resolved.read_text().strip(), f"'{topic}' document is empty"

    def test_unknown_topic_resolves_to_nothing(self):
        from client.cli import resolve_doc

        assert resolve_doc("no-such-topic") is None

    def test_package_data_declares_the_docs(self):
        package_data = _read_pyproject()["tool"]["setuptools"]["package-data"]["skein"]
        assert "docs/*.md" in package_data

    def test_docs_resolve_to_documents_not_link_targets(self):
        """skein/docs holds links, so the packaged copy cannot drift from docs/.

        Asserting non-emptiness is not enough: a checkout made without symlink
        support (`git clone -c core.symlinks=false`, some Windows and archive
        checkouts) turns each of these into a ~30-byte regular file holding the
        link target's path, and a wheel built there packages that string
        verbatim. Only the size and shape tell them apart.
        """
        from client.cli import MIN_DOC_BYTES

        for name in ("SKEIN_QUICK_START.md", "SKEIN_AGENT_GUIDE.md", "ARCHITECTURE.md"):
            packaged = PACKAGE_ROOT / "docs" / name
            assert packaged.exists(), f"{packaged} is missing"
            content = packaged.read_text()
            assert (
                len(content) >= MIN_DOC_BYTES
            ), f"{packaged} is {len(content)} bytes — a link target, not a document"
            assert content.lstrip().startswith("#"), f"{packaged} is not a markdown document"


@pytest.mark.slow_build
class TestBuiltWheel:
    """Build the wheel and look inside it.

    The docs are symlinks in the source tree; a build that packaged the link
    instead of its content would install a wheel whose `skein info quickstart`
    prints a relative path. That failure is invisible from the source tree, so
    it is only catchable here.
    """

    @pytest.fixture(scope="class")
    def wheel(self, tmp_path_factory):
        # An isolated build, because the declared backend (setuptools>=77) is
        # newer than what a dev environment necessarily has installed — an older
        # setuptools rejects this pyproject's PEP 639 license field outright.
        outdir = tmp_path_factory.mktemp("wheel")
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "build",
                "--wheel",
                "--outdir",
                str(outdir),
            ],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            timeout=900,
        )
        if result.returncode != 0:
            pytest.fail(f"wheel build failed:\n{result.stdout}\n{result.stderr}")
        wheels = list(outdir.glob("interskein-*.whl"))
        assert len(wheels) == 1, f"expected one wheel, got {wheels}"
        return wheels[0]

    def test_wheel_contains_the_service_module(self, wheel):
        """Without this the wheel installs a CLI with no server to talk to."""
        with zipfile.ZipFile(wheel) as zf:
            assert "skein/server.py" in zf.namelist()
            assert "skein/version.py" in zf.namelist()

    def test_wheel_contains_doc_content_not_symlinks(self, wheel):
        with zipfile.ZipFile(wheel) as zf:
            names = zf.namelist()
            for name in (
                "skein/docs/SKEIN_QUICK_START.md",
                "skein/docs/SKEIN_AGENT_GUIDE.md",
                "skein/docs/ARCHITECTURE.md",
            ):
                assert name in names, f"{name} is not in the wheel"
                content = zf.read(name).decode()
                # A packaged symlink would be a one-line relative path.
                assert len(content) > 500, f"{name} looks like a link, not a document"  # noqa: E501
                assert content.lstrip().startswith("#")

    def test_wheel_declares_the_console_scripts(self, wheel):
        with zipfile.ZipFile(wheel) as zf:
            entry_points = next(
                zf.read(n).decode() for n in zf.namelist() if n.endswith("entry_points.txt")
            )
        assert "skein = client.cli:main" in entry_points
        assert "skein-server = skein.server:main" in entry_points

    def test_wheel_contains_the_systemd_unit(self, wheel):
        """A wheel user has no repo to copy the unit from, so it has to be here —
        with its placeholder intact, since rendering happens at install."""
        with zipfile.ZipFile(wheel) as zf:
            assert "skein/units/skein.service" in zf.namelist()
            unit = zf.read("skein/units/skein.service").decode()
        assert "ExecStart=__SKEIN_SERVER__" in unit
