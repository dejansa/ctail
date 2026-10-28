import io
import os
import pty
import select
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from rich.console import Console

from ctail.cli import (
    _associate_profile,
    _load_settings,
    _manage_profiles,
    _matching_profile,
    _parse_profile_paths,
    _parse_rule_profiles,
    _select_profile,
    _single_file_argument,
    main,
)


class ProfilePathTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.config_path = self.root / "ctail.toml"
        self.rules_path = self.root / "rules.toml"
        self.rules_path.write_text("")

    def parse(self, entries):
        data = {"profiles": {"one": {}, "two": {}}, "profile_paths": entries}
        profiles = _parse_rule_profiles(data, self.config_path)
        return _parse_profile_paths(data, self.config_path, profiles)

    def test_file_directory_glob_and_order(self):
        directory = self.root / "logs"
        mappings = self.parse([
            {"profile": "one", "paths": [f"{directory}/"]},
            {"profile": "two", "paths": [f"{directory}/**/*.log"]},
        ])
        self.assertEqual(_matching_profile(str(directory / "a.log"), mappings), "one")
        self.assertEqual(_matching_profile(str(directory / "nested" / "a.log"), mappings), "two")
        self.assertIsNone(_matching_profile(str(directory / "nested" / "a.txt"), mappings))
        exact = self.parse([{"profile": "one", "paths": [str(directory / "x.log")]}])
        self.assertEqual(_matching_profile(str(directory / "." / "x.log"), exact), "one")
        self.assertIsNone(_matching_profile(str(directory / "other.log"), exact))

    def test_home_and_symlink_are_lexical_and_no_file_is_required(self):
        with patch.dict(os.environ, {"HOME": str(self.root)}):
            mappings = self.parse([{"profile": "one", "paths": ["~/link/*.log"]}])
        (self.root / "link").symlink_to(self.root / "real", target_is_directory=True)
        self.assertEqual(_matching_profile(str(self.root / "link" / "missing.log"), mappings), "one")
        self.assertIsNone(_matching_profile(str(self.root / "real" / "missing.log"), mappings))

    def test_invalid_mappings_are_reported(self):
        for entries in (
            "not a list",
            ["not a table"],
            [{"profile": "missing", "paths": ["/tmp/*"]}],
            [{"profile": "one", "paths": []}],
            [{"profile": "one", "paths": ["relative/*.log"]}],
            [{"profile": "one", "paths": [1]}],
            [{"profile": "one", "paths": [""]}],
        ):
            with self.subTest(entries=entries), self.assertRaises((ValueError, TypeError)):
                self.parse(entries)

    def test_source_file_detection_is_conservative(self):
        for args, expected in (
            (["-f", "/tmp/a"], "/tmp/a"),
            (["-n", "20", "--follow=name", "/tmp/a"], "/tmp/a"),
            (["--lines=3", "-c10", "/tmp/a"], "/tmp/a"),
            (["--", "-filename"], "-filename"),
            (["-n", "20"], None),
            (["--unknown", "/tmp/a"], None),
            (["/tmp/a", "/tmp/b"], None),
            (["-"], None),
        ):
            with self.subTest(args=args):
                self.assertEqual(_single_file_argument(args), expected)
        self.assertEqual(_single_file_argument(["--", "/tmp/a"], less=True), "/tmp/a")
        self.assertIsNone(_single_file_argument(["a", "b"], less=True))
        self.assertIsNone(_single_file_argument([], less=True))

    def test_matching_profile_does_not_change_saved_profile(self):
        log = self.root / "a.log"
        original = (
            'active_profile = "two"\n'
            f'[[profile_paths]]\nprofile = "one"\npaths = ["{log}"]\n'
            "[profiles.one]\n[profiles.two]\n"
        )
        self.config_path.write_text(original)
        source = Mock()
        source.fileno.return_value = 10
        process = Mock(stdout=source)
        process.poll.return_value = 0
        stdin = Mock()
        stdin.isatty.return_value = True
        output = io.StringIO()
        with (
            redirect_stdout(output),
            patch("sys.stdin", stdin),
            patch("sys.argv", ["ctail", "-f", str(log)]),
            patch("ctail.cli._config_directory", return_value=self.root),
            patch("ctail.cli._tail_process", return_value=process) as tail,
            patch("ctail.cli._consume_stream") as consume,
        ):
            main()
        tail.assert_called_once_with(["-f", str(log)])
        self.assertEqual(consume.call_args.args[7], "one")
        self.assertTrue(consume.call_args.kwargs["session_only_profile"])
        self.assertEqual(self.config_path.read_text(), original)

    def test_explicit_profile_overrides_match_and_remains_persistent(self):
        log = self.root / "a.log"
        self.config_path.write_text(
            f'[[profile_paths]]\nprofile = "one"\npaths = ["{log}"]\n'
            "[profiles.one]\n[profiles.two]\n"
        )
        stdin = Mock()
        stdin.isatty.return_value = True
        process = Mock(stdout=Mock())
        process.poll.return_value = 0
        with (
            redirect_stdout(io.StringIO()),
            patch("sys.stdin", stdin),
            patch("sys.argv", ["ctail", "--profile", "two", "-f", str(log)]),
            patch("ctail.cli._config_directory", return_value=self.root),
            patch("ctail.cli._tail_process", return_value=process),
            patch("ctail.cli._consume_stream") as consume,
        ):
            main()
        self.assertEqual(consume.call_args.args[7], "two")
        self.assertFalse(consume.call_args.kwargs["session_only_profile"])
        self.assertEqual(tomllib.loads(self.config_path.read_text())["active_profile"], "two")

    def test_less_uses_profile_for_file_but_not_multiple_files(self):
        log = self.root / "a.log"
        self.config_path.write_text(
            'active_profile = "two"\n'
            f'[[profile_paths]]\nprofile = "one"\npaths = ["{log}"]\n'
            '[profiles.one]\n[[profiles.one.rules]]\npattern = "one"\ncolor = "red"\n'
            '[profiles.two]\n[[profiles.two.rules]]\npattern = "two"\ncolor = "blue"\n'
        )
        for args, expected in (([str(log)], "one"), ([str(log), "other"], "two")):
            with (
                self.subTest(args=args),
                redirect_stdout(io.StringIO()),
                patch("sys.argv", ["ctail", "--less", *args]),
                patch("ctail.cli._config_directory", return_value=self.root),
                patch("ctail.cli._show_with_less") as pager,
            ):
                main()
            self.assertIn(expected, [rule.pattern for rule in pager.call_args.args[2]])
        self.assertEqual(tomllib.loads(self.config_path.read_text())["active_profile"], "two")

    def test_piped_input_and_ambiguous_tail_options_use_saved_profile(self):
        log = self.root / "a.log"
        self.config_path.write_text(
            'active_profile = "two"\n'
            f'[[profile_paths]]\nprofile = "one"\npaths = ["{log}"]\n'
            "[profiles.one]\n[profiles.two]\n"
        )
        for is_terminal, args in (
            (False, [str(log)]),
            (True, ["--unknown-option", str(log)]),
            (True, [str(log), str(self.root / "b.log")]),
        ):
            stdin = Mock()
            stdin.isatty.return_value = is_terminal
            process = Mock(stdout=Mock())
            process.poll.return_value = 0
            with (
                self.subTest(is_terminal=is_terminal, args=args),
                redirect_stdout(io.StringIO()),
                patch("sys.stdin", stdin),
                patch("sys.argv", ["ctail", *args]),
                patch("ctail.cli._config_directory", return_value=self.root),
                patch("ctail.cli._tail_process", return_value=process),
                patch("ctail.cli._consume_stream") as consume,
            ):
                main()
            self.assertEqual(consume.call_args.args[7], "two")
            self.assertFalse(consume.call_args.kwargs["session_only_profile"])
            self.assertIsNone(consume.call_args.kwargs["source_file"])

    def test_interactive_override_is_session_only(self):
        self.config_path.write_text('active_profile = "two"\n[profiles.one]\n[profiles.two]\n')
        output = io.StringIO()
        console = Console(file=output, color_system=None)
        state = [0, 0, 0, 0, 0, 0, [0] * 8]
        with patch("ctail.cli._select_profile", return_value=(False, None)):
            self.assertIsNone(_manage_profiles(
                console, [], self.rules_path, 11, state, "one", session_only=True
            ))
        self.assertEqual(tomllib.loads(self.config_path.read_text())["active_profile"], "two")
        with (
            patch("ctail.cli._select_profile", return_value=(True, None)),
            patch("ctail.cli._prompt_profile_name", return_value="new"),
        ):
            self.assertEqual(_manage_profiles(
                console, [], self.rules_path, 11, state, "one", session_only=True
            ), "new")
        data = tomllib.loads(self.config_path.read_text())
        self.assertEqual(data["active_profile"], "two")
        self.assertIn("new", data["profiles"])

    def test_association_confirmation_escaping_and_precedence(self):
        self.config_path.write_text(
            '# retained\n[[profile_paths]]\nprofile = "two"\npaths = ["/tmp/*"]\n'
            "[profiles.one]\n[profiles.two]\n"
        )
        filename = str(self.root / "log[1]*.txt")
        output = io.StringIO()
        console = Console(file=output, color_system=None, width=200)
        before = self.config_path.read_text()
        with patch("ctail.cli._read_rule_key", side_effect=["f", "cancel"]):
            _associate_profile(console, self.config_path, 11, filename, "one")
        self.assertEqual(self.config_path.read_text(), before)
        with patch("ctail.cli._read_rule_key", side_effect=["f", "y"]):
            _associate_profile(console, self.config_path, 11, filename, "one")
        data = tomllib.loads(self.config_path.read_text())
        profiles = _parse_rule_profiles(data, self.config_path)
        mappings = _parse_profile_paths(data, self.config_path, profiles)
        self.assertEqual(data["profile_paths"][0]["profile"], "one")
        self.assertEqual(_matching_profile(filename, mappings), "one")
        self.assertIsNone(_matching_profile(str(self.root / "log1x.txt"), mappings))
        self.assertIn("This takes priority", output.getvalue())
        self.assertIn(data["profile_paths"][0]["paths"][0], output.getvalue())
        self.assertIn("# retained", self.config_path.read_text())

    def test_association_directory_matches_direct_children_only(self):
        self.config_path.write_text("[profiles.one]\n")
        console = Console(file=io.StringIO(), color_system=None)
        filename = str(self.root / "file.log")
        with patch("ctail.cli._read_rule_key", side_effect=["d", "y"]):
            _associate_profile(console, self.config_path, 11, filename, "one")
        data = tomllib.loads(self.config_path.read_text())
        mappings = _parse_profile_paths(
            data, self.config_path, _parse_rule_profiles(data, self.config_path)
        )
        self.assertEqual(_matching_profile(filename, mappings), "one")
        self.assertIsNone(_matching_profile(str(self.root / "sub" / "file.log"), mappings))
        _load_settings(self.root, "one")

    def test_selector_association_action_uses_highlighted_profile(self):
        self.config_path.write_text("[profiles.one]\n")
        console = Console(file=io.StringIO(), color_system=None)
        with (
            patch("ctail.cli.Live"),
            patch("ctail.cli._read_rule_key", side_effect=["a", "down", "a"]),
        ):
            result = _select_profile(console, self.config_path, 11, source_file="file.log")
        self.assertEqual(result, ("associate", "one"))


class ProfilePathPtyTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("less"), "less is required")
    def test_real_less_and_piped_input_choose_expected_profiles(self):
        with tempfile.TemporaryDirectory() as home:
            root = Path(home)
            config_dir = root / ".config" / "ctail"
            config_dir.mkdir(parents=True)
            log = root / "file.log"
            log.write_text("ONE\nTWO\n")
            (config_dir / "ctail.toml").write_text(
                'active_profile = "other"\nshow_unmatched = false\n'
                f'[[profile_paths]]\nprofile = "logs"\npaths = ["{log}"]\n'
                '[profiles.logs]\n[[profiles.logs.rules]]\npattern = "ONE"\ncolor = "red"\n'
                '[profiles.other]\n[[profiles.other.rules]]\npattern = "TWO"\ncolor = "blue"\n'
            )
            (config_dir / "rules.toml").write_text("")
            command = [sys.executable, "-c", "from ctail.cli import main; main()", "--less"]
            env = dict(os.environ, HOME=home, TERM="xterm-256color")
            file_result = subprocess.run(
                [*command, str(log)], env=env, text=True, capture_output=True, timeout=5
            )
            self.assertEqual(file_result.returncode, 0, file_result.stderr)
            self.assertIn("ONE", file_result.stdout)
            self.assertNotIn("TWO", file_result.stdout)
            pipe_result = subprocess.run(
                command, input="ONE\nTWO\n", env=env, text=True,
                capture_output=True, timeout=5,
            )
            self.assertEqual(pipe_result.returncode, 0, pipe_result.stderr)
            self.assertIn("TWO", pipe_result.stdout)
            self.assertNotIn("ONE", pipe_result.stdout)

    def test_auto_profile_rule_scope_and_manual_override(self):
        with tempfile.TemporaryDirectory() as home:
            root = Path(home)
            config_dir = root / ".config" / "ctail"
            config_dir.mkdir(parents=True)
            config_path = config_dir / "ctail.toml"
            log = root / "file.log"
            log.touch()
            config_path.write_text(
                'active_profile = "other"\n'
                f'[[profile_paths]]\nprofile = "logs"\npaths = ["{log}"]\n'
                "[profiles.logs]\n[profiles.other]\n"
            )
            rules_path = config_dir / "rules.toml"
            rules_path.write_text("")
            master, slave = pty.openpty()
            process = subprocess.Popen(
                [sys.executable, "-c", "from ctail.cli import main; main()", "-f", str(log)],
                stdin=slave, stdout=slave, stderr=slave,
                env=dict(os.environ, HOME=home, TERM="xterm-256color"),
            )

            def wait_for(marker):
                output = bytearray()
                deadline = time.monotonic() + 8
                while marker not in output:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not select.select([master], [], [], remaining)[0]:
                        self.fail(f"Timed out waiting for {marker!r}: {bytes(output)!r}")
                    output.extend(os.read(master, 65536))

            try:
                wait_for(b"selected by file path")
                os.write(master, b"c")
                wait_for(b"Literal text to color")
                os.write(master, b"scoped-rule\n")
                wait_for(b"Rich color/style")
                os.write(master, b"red\n")
                wait_for(b"Added literal rule")
                os.write(master, b"p")
                wait_for(b"Select profile")
                os.write(master, b"\r")
                wait_for(b"Selected profile")
                os.write(master, b"q")
                process.wait(timeout=5)
                self.assertEqual(process.returncode, 0)
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)
                os.close(master)
                os.close(slave)
            self.assertEqual(tomllib.loads(config_path.read_text())["active_profile"], "other")
            rules = tomllib.loads(rules_path.read_text())
            self.assertEqual(rules["profiles"]["logs"]["rules"][0]["pattern"], "scoped-rule")

    def test_association_is_confirmed_and_used_on_next_start(self):
        with tempfile.TemporaryDirectory() as home:
            root = Path(home)
            config_dir = root / ".config" / "ctail"
            config_dir.mkdir(parents=True)
            config_path = config_dir / "ctail.toml"
            config_path.write_text('active_profile = "other"\n[profiles.logs]\n[profiles.other]\n')
            (config_dir / "rules.toml").write_text("")
            log = root / "file.log"
            log.touch()
            master, slave = pty.openpty()
            process = subprocess.Popen(
                [sys.executable, "-c", "from ctail.cli import main; main()", "-f", str(log)],
                stdin=slave, stdout=slave, stderr=slave,
                env=dict(os.environ, HOME=home, TERM="xterm-256color"),
            )

            def wait_for(marker):
                output = bytearray()
                deadline = time.monotonic() + 8
                while marker not in output:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not select.select([master], [], [], remaining)[0]:
                        self.fail(f"Timed out waiting for {marker!r}: {bytes(output)!r}")
                    output.extend(os.read(master, 65536))

            try:
                wait_for(b"Space: pause/resume")
                os.write(master, b"p")
                wait_for(b"Select profile")
                os.write(master, b"\x1b[B" + b"a")
                wait_for(b"Associate profile with:")
                os.write(master, b"f")
                wait_for(b"y: confirm")
                self.assertNotIn("profile_paths", tomllib.loads(config_path.read_text()))
                os.write(master, b"y")
                wait_for(b"Association saved")
                os.write(master, b"\x1b")
                time.sleep(0.3)
                os.write(master, b"q")
                process.wait(timeout=5)
                self.assertEqual(process.returncode, 0)
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)
                os.close(master)
                os.close(slave)
            data = tomllib.loads(config_path.read_text())
            self.assertEqual(data["active_profile"], "other")
            mappings = _parse_profile_paths(
                data, config_path, _parse_rule_profiles(data, config_path)
            )
            self.assertEqual(_matching_profile(str(log), mappings), "logs")


if __name__ == "__main__":
    unittest.main()
