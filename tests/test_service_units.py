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

    def test_a_token_containing_a_placeholder_is_not_rewritten(self, monkeypatch):
        """Chained .replace() spliced the log path into an argv token that
        contained the later placeholder; single-pass substitution must emit
        the literal (finding-20260725-h08f)."""
        weird = ["/Users/pat/__SKEIN_LOG_PATH__/skein-server", "__SKEIN_SERVER_ARGS__"]
        monkeypatch.setattr("skein.server.server_command", lambda: weird)
        parsed = plistlib.loads(render_plist().encode())
        assert parsed["ProgramArguments"] == weird

    def test_a_carriage_return_survives_the_round_trip(self, monkeypatch):
        """An XML parser normalizes a literal \\r to \\n; the renderer must
        emit it as a numeric reference so the token comes back byte-identical."""
        weird = ["/odd\rpath/skein-server"]
        monkeypatch.setattr("skein.server.server_command", lambda: weird)
        parsed = plistlib.loads(render_plist().encode())
        assert parsed["ProgramArguments"] == weird

    @pytest.mark.parametrize(
        "bad_char",
        ["\x01", "\x0b", "\ud800", "\udfff", "\ufffe", "\uffff"],
        ids=["C0-SOH", "C0-VT", "lone-high-surrogate", "lone-low-surrogate", "FFFE", "FFFF"],
    )
    def test_an_xml_unrepresentable_token_is_refused_not_mangled(self, monkeypatch, bad_char):
        """XML 1.0 cannot carry C0 controls (beyond tab/newline/CR), lone
        surrogates (which surrogateescape puts in undecodable paths), or
        U+FFFE/U+FFFF; rendering must refuse in the module's idiom rather
        than emit a plist launchd cannot parse."""
        monkeypatch.setattr("skein.server.server_command", lambda: [f"/bad{bad_char}path"])
        with pytest.raises(SystemExit) as excinfo:
            render_plist()
        assert "XML" in str(excinfo.value)

    def test_tab_and_newline_in_tokens_still_round_trip(self, monkeypatch):
        """The refusal is exactly the unrepresentable set — tab and newline
        are legal XML and must keep round-tripping byte-identical."""
        weird = ["/odd\tpath/with\nnewline"]
        monkeypatch.setattr("skein.server.server_command", lambda: weird)
        assert plistlib.loads(render_plist().encode())["ProgramArguments"] == weird

    def test_a_relative_home_still_yields_absolute_log_paths(self, tmp_path, monkeypatch):
        """HOME=relative-home makes Path.home() relative; the log paths must
        come out absolute anyway, anchored where the render ran
        (finding-20260725-qn1j)."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("HOME", "relative-home")
        parsed = plistlib.loads(render_plist().encode())
        assert Path(parsed["StandardOutPath"]).is_absolute()
        assert parsed["StandardOutPath"].startswith(str(tmp_path))

    def test_an_undeterminable_home_is_refused_not_a_traceback(self, monkeypatch):
        """HOME='~' (or no HOME and no passwd entry) makes Path.home() raise
        RuntimeError; the renderer must refuse in the module's idiom
        (finding-20260725-4c8b)."""
        monkeypatch.setenv("HOME", "~")
        with pytest.raises(SystemExit) as excinfo:
            render_plist()
        assert "home directory" in str(excinfo.value)

    def test_main_print_plist_refuses_cleanly_on_an_undeterminable_home(
        self, tmp_path, monkeypatch
    ):
        """Through the real entrypoint: SKEIN_HOME set (so the pre-existing
        get_config path is bypassed — that crash exists on master), HOME='~',
        and --print-plist must exit via the clean message, not a traceback."""
        from skein import server

        monkeypatch.setenv("SKEIN_HOME", str(tmp_path / "skein-home"))
        monkeypatch.setenv("HOME", "~")
        with pytest.raises(SystemExit) as excinfo:
            server.main(["--print-plist"])
        assert "home directory" in str(excinfo.value)

    def test_print_plist_output_is_utf8_regardless_of_stdout_encoding(self, tmp_path):
        """The document declares UTF-8, so the bytes on stdout must BE UTF-8
        even when the locale says otherwise — a cp1252 stdout wrote cp1252
        bytes under a UTF-8 declaration (finding-20260725-yd47). Subprocess,
        because the boundary is the real stdout encoding."""
        import os
        import subprocess

        home = tmp_path / "casa-española"
        home.mkdir()
        out = subprocess.run(
            [sys.executable, "-m", "skein.server", "--print-plist"],
            capture_output=True,
            env={
                **os.environ,
                "HOME": str(home),
                "PYTHONIOENCODING": "cp1252",
                "PYTHONPATH": str(Path(__file__).resolve().parent.parent),
            },
        )
        assert out.returncode == 0, out.stderr.decode(errors="replace")
        parsed = plistlib.loads(out.stdout)  # rejects non-UTF-8 bytes under the header
        assert str(home) in parsed["StandardOutPath"]

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
