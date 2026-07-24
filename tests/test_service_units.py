"""Tests for the rendered launchd plist (skein-server --print-plist).

The plist's CONTENT is fully testable here — plistlib parses the same XML
plist format launchd reads — but launchd itself only exists on macOS, so
loading the rendered plist is exercised on a real Mac, not by this suite
(notion-20260723-477x; the socket-activation half of that notion was closed
do-not-implement, so the systemd side ships only the always-on unit covered
in test_packaging).
"""

import plistlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from skein.server import render_plist, server_command


class TestPlist:
    def test_renders_a_parseable_plist_with_this_installs_argv(self):
        rendered = render_plist()
        assert "__SKEIN" not in rendered, "unresolved placeholder"
        parsed = plistlib.loads(rendered.encode())
        assert parsed["Label"] == "net.interskein.skein-server"
        assert parsed["ProgramArguments"] == server_command()
        assert parsed["RunAtLoad"] is True
        # Mirror of the systemd unit's Restart=on-failure.
        assert parsed["KeepAlive"] == {"SuccessfulExit": False}
        # launchd does not expand ~, so the log paths must come out absolute.
        assert Path(parsed["StandardOutPath"]).is_absolute()
        assert parsed["StandardOutPath"] == parsed["StandardErrorPath"]

    def test_xml_hostile_argv_tokens_round_trip(self, monkeypatch):
        """A path with XML metacharacters (an Application Support tree with an
        ampersand, a quoted segment) must arrive in launchd byte-identical."""
        hostile = [
            "/Users/pat/A & B <Support>/skein-server",
            '-m "quoted" \'apostrophe\'',
        ]
        monkeypatch.setattr("skein.server.server_command", lambda: hostile)
        parsed = plistlib.loads(render_plist().encode())
        assert parsed["ProgramArguments"] == hostile

    def test_print_plist_flag_prints_it_and_exits(self, capsys):
        from skein import server

        server.main(["--print-plist"])
        out = capsys.readouterr().out
        assert plistlib.loads(out.encode())["Label"] == "net.interskein.skein-server"
        assert out.endswith("\n")

    def test_print_plist_works_even_with_a_broken_skein_port(self, capsys, monkeypatch):
        """Rendering is exempt from the bind-time refusal guards, like
        --version and --print-unit."""
        from skein import server

        monkeypatch.setenv("SKEIN_PORT", "80o1")
        server.main(["--print-plist"])
        assert plistlib.loads(capsys.readouterr().out.encode())
