import io
import os
import pty
import select
import subprocess
import sys
import tempfile
import termios
import time
import tomllib
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from rich.console import Console
from rich.table import Table

from ctail.cli import (
    DEFAULT_RULES,
    _add_rule,
    _change_rule,
    _consume_stream,
    _load_settings,
    _manage_rules,
    _parse_rules,
    _read_rule_key,
    _render_line,
    _select_rule,
    _show_rules,
    _write_ad_hoc_rule,
)


class RuleManagementTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.home = Path(self.directory.name)
        self.config_dir = self.home / ".config" / "ctail"
        self.config_dir.mkdir(parents=True)
        self.rules_path = self.config_dir / "rules.toml"
        self.config_path = self.config_dir / "ctail.toml"
        self.output = io.StringIO()
        self.console = Console(file=self.output, width=160, height=8, color_system=None)

    def load_rules(self):
        return _load_settings(self.config_dir)[1]

    def write_custom_rules(self):
        self.rules_path.write_text(
            '# ad-hoc comment\nnote = "keep me"\n\n'
            '[[rules]]\npattern = "literal"\ncolor = "red"\nregex = false\n'
            '# disabled comment\ndisabled = true\n\n'
            '[[rules]]\npattern = "other"\ncolor = "green"\n'
        )
        self.config_path.write_text(
            '# main comment\nshow_unmatched = true # keep this\n\n'
            '[[rules]]\npattern = "user"\ncolor = "blue"\n\n'
            '[extra]\nvalue = 42\n'
        )

    def test_disabled_must_be_boolean(self):
        for value in ("true", 1, None):
            with self.subTest(value=value), self.assertRaisesRegex(
                TypeError, "'disabled' must be true or false"
            ):
                _parse_rules(
                    [{"pattern": "x", "color": "red", "disabled": value}],
                    self.rules_path,
                )

    def test_invalid_rule_types_raise_type_error(self):
        for data in ("not a rules array", ["not a rule table"]):
            with self.subTest(data=data), self.assertRaises(TypeError):
                _parse_rules(data, self.rules_path)

    def test_name_is_optional_and_preserved_when_provided(self):
        rules = _parse_rules(
            [
                {"pattern": "unnamed", "color": "red"},
                {"name": "Named rule", "pattern": "named", "color": "blue"},
            ],
            self.rules_path,
        )
        self.assertIsNone(rules[0].name)
        self.assertEqual(rules[1].name, "Named rule")

    def test_name_must_be_a_non_empty_string_when_provided(self):
        for name, exception in ((1, TypeError), ("", ValueError)):
            with self.subTest(name=name), self.assertRaises(exception):
                _parse_rules(
                    [{"name": name, "pattern": "x", "color": "red"}],
                    self.rules_path,
                )

    def test_disabled_rules_do_not_match_or_trigger_empty_match_errors(self):
        rules = _parse_rules(
            [{"pattern": ".*?", "color": "red", "disabled": True}],
            self.rules_path,
        )
        self.assertIsNone(_render_line("anything", rules, False))
        rendered = _render_line("anything", rules, True)
        assert rendered is not None
        self.assertEqual(rendered.plain, "anything")
        self.assertEqual(rendered.spans, [])

    def test_disabled_high_priority_rule_allows_lower_priority_match(self):
        rules = _parse_rules(
            [
                {"pattern": "x", "color": "red", "disabled": True},
                {"pattern": "x", "color": "green"},
            ],
            self.rules_path,
        )
        rendered = _render_line("x", rules, False)
        assert rendered is not None
        self.assertEqual(rendered.spans[0].style, "green")

    def test_add_preserves_disabled_rules_comments_and_extra_settings(self):
        self.write_custom_rules()
        _write_ad_hoc_rule(self.rules_path, "new", "white on red")
        text = self.rules_path.read_text()
        self.assertIn("# ad-hoc comment", text)
        self.assertIn("# disabled comment", text)
        data = tomllib.loads(text)
        self.assertEqual(data["note"], "keep me")
        self.assertFalse(data["rules"][0]["regex"])
        self.assertFalse(data["rules"][0]["disabled"])
        self.assertTrue(data["rules"][1]["disabled"])
        self.assertEqual([rule.pattern for rule in self.load_rules()][:3],
                         ["new", "literal", "other"])

    def test_toggle_custom_rules_in_both_files_and_reload(self):
        self.write_custom_rules()
        for pattern in ("literal", "user"):
            with self.subTest(pattern=pattern):
                rule = next(rule for rule in self.load_rules() if rule.pattern == pattern)
                _change_rule(rule, self.rules_path, delete=False)
                changed = next(rule for rule in self.load_rules() if rule.pattern == pattern)
                self.assertEqual(changed.disabled, not rule.disabled)
                _change_rule(changed, self.rules_path, delete=False)
                restored = next(rule for rule in self.load_rules() if rule.pattern == pattern)
                self.assertEqual(restored.disabled, rule.disabled)
        self.assertIn("# main comment", self.config_path.read_text())
        self.assertIn("# keep this", self.config_path.read_text())
        self.assertEqual(tomllib.loads(self.config_path.read_text())["extra"]["value"], 42)

    def test_delete_custom_rules_from_both_files(self):
        self.write_custom_rules()
        for pattern in ("literal", "user"):
            rule = next(rule for rule in self.load_rules() if rule.pattern == pattern)
            _change_rule(rule, self.rules_path, delete=True)
            self.assertNotIn(pattern, [rule.pattern for rule in self.load_rules()])
        self.assertEqual(tomllib.loads(self.rules_path.read_text())["note"], "keep me")
        data = tomllib.loads(self.config_path.read_text())
        self.assertTrue(data["show_unmatched"])
        self.assertEqual(data["extra"]["value"], 42)
        self.assertIn("# main comment", self.config_path.read_text())

    def test_duplicate_patterns_edit_selected_occurrence(self):
        self.rules_path.write_text(
            '[[rules]]\npattern = "same"\ncolor = "red"\n\n'
            '[[rules]]\npattern = "same"\ncolor = "red"\n'
        )
        _change_rule(self.load_rules()[1], self.rules_path, delete=False)
        rules = self.load_rules()
        self.assertFalse(rules[0].disabled)
        self.assertTrue(rules[1].disabled)
        _change_rule(rules[1], self.rules_path, delete=True)
        self.assertEqual(len(self.load_rules()), len(DEFAULT_RULES) + 1)
        self.assertFalse(self.load_rules()[0].disabled)

    def test_builtin_toggle_creates_and_updates_single_override(self):
        rule = self.load_rules()[0]
        self.assertIsNone(rule.source_index)
        _change_rule(rule, self.rules_path, delete=False)
        disabled = self.load_rules()[0]
        self.assertTrue(disabled.disabled)
        self.assertEqual(disabled.source, "built-in")
        self.assertIsNone(_render_line("[ERROR] failure", self.load_rules(), False))
        _change_rule(disabled, self.rules_path, delete=False)
        self.assertFalse(self.load_rules()[0].disabled)
        self.assertIsNotNone(_render_line("[ERROR] failure", self.load_rules(), False))
        entries = tomllib.loads(self.config_path.read_text())["rules"]
        self.assertEqual(len(entries), 1)
        self.assertTrue(entries[0]["builtin"])
        self.assertFalse(entries[0]["disabled"])

    def test_builtin_override_is_distinct_from_custom_rule_with_same_pattern(self):
        self.write_custom_rules()
        self.config_path.write_text(
            self.config_path.read_text() + "\n"
            "[[rules]]\n"
            f"pattern = '{DEFAULT_RULES[0][0]}'\ncolor = 'magenta'\n"
        )
        builtin = next(rule for rule in self.load_rules() if rule.source == "built-in")
        _change_rule(builtin, self.rules_path, delete=False)
        rules = self.load_rules()
        same_pattern = [rule for rule in rules if rule.pattern == builtin.pattern]
        self.assertEqual(len(same_pattern), 2)
        self.assertFalse(same_pattern[0].disabled)
        self.assertTrue(same_pattern[1].disabled)
        self.assertEqual(len(rules), len(DEFAULT_RULES) + 4)

    def test_invalid_builtin_overrides_are_rejected(self):
        invalid_overrides = (
            (
                '[[rules]]\nbuiltin = true\npattern = "unknown"\ndisabled = true\n',
                ValueError,
            ),
            (
                '[[rules]]\nbuiltin = "true"\npattern = "x"\ncolor = "red"\n',
                TypeError,
            ),
            (
                f"[[rules]]\nbuiltin = true\npattern = '{DEFAULT_RULES[0][0]}'\nregex = false\n",
                ValueError,
            ),
            (
                2 * f"[[rules]]\nbuiltin = true\npattern = '{DEFAULT_RULES[0][0]}'\n",
                ValueError,
            ),
        )
        for contents, exception in invalid_overrides:
            with self.subTest(contents=contents):
                self.config_path.write_text(contents)
                with self.assertRaises(exception):
                    self.load_rules()
        self.config_path.unlink()
        self.rules_path.write_text(
            f"[[rules]]\nbuiltin = true\npattern = '{DEFAULT_RULES[0][0]}'\n"
        )
        with self.assertRaisesRegex(ValueError, "in ctail.toml"):
            self.load_rules()

    def test_builtin_deletion_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "cannot be deleted"):
            _change_rule(self.load_rules()[0], self.rules_path, delete=True)
        self.assertFalse(self.config_path.exists())

    def test_changed_entry_is_not_overwritten(self):
        self.write_custom_rules()
        rule = self.load_rules()[0]
        self.rules_path.write_text(self.rules_path.read_text().replace('"literal"', '"edited"'))
        before = self.rules_path.read_bytes()
        with self.assertRaisesRegex(ValueError, "changed on disk"):
            _change_rule(rule, self.rules_path, delete=True)
        self.assertEqual(self.rules_path.read_bytes(), before)

    def test_failed_save_preserves_disk_and_running_rules(self):
        self.write_custom_rules()
        rules = self.load_rules()
        before = list(rules)
        data = self.rules_path.read_bytes()
        with (
            patch("ctail.cli._select_rule", return_value=rules[0]),
            patch("ctail.cli.os.replace", side_effect=OSError("write denied")),
        ):
            _manage_rules(self.console, rules, self.rules_path, 0, delete=True)
        self.assertEqual(rules, before)
        self.assertEqual(self.rules_path.read_bytes(), data)
        self.assertIn("write denied", self.output.getvalue())
        self.assertEqual(sorted(path.name for path in self.config_dir.iterdir()),
                         ["ctail.toml", "rules.toml"])

    def test_management_updates_live_rules_only_after_successful_save(self):
        self.write_custom_rules()
        rules = self.load_rules()
        with patch("ctail.cli._select_rule", return_value=rules[0]):
            _manage_rules(self.console, rules, self.rules_path, 0, delete=False)
        self.assertFalse(rules[0].disabled)
        self.assertIsNotNone(_render_line("literal", rules, False))
        with patch("ctail.cli._select_rule", return_value=rules[0]):
            _manage_rules(self.console, rules, self.rules_path, 0, delete=True)
        self.assertIsNone(_render_line("literal", rules, False))
        self.assertEqual(rules, self.load_rules())
        self.assertEqual(rules[0].source_index, 0)

    def test_adding_live_rule_reloads_source_indices_and_preserves_disabled_state(self):
        self.write_custom_rules()
        rules = self.load_rules()
        terminal = [0, 0, 0, termios.ICANON | termios.ECHO, 0, 0, [0] * 32]
        with (
            patch("builtins.input", side_effect=["new", "bold white on red"]),
            patch("ctail.cli.termios.tcsetattr"),
            patch("ctail.cli.termios.tcgetattr", return_value=terminal),
        ):
            _add_rule(self.console, rules, self.rules_path, 0, terminal)
        self.assertEqual(rules, self.load_rules())
        self.assertEqual(rules[0].pattern, "new")
        self.assertTrue(rules[1].disabled)
        self.assertEqual(rules[1].source_index, 1)
        _change_rule(rules[1], self.rules_path, delete=True)
        self.assertEqual(self.load_rules()[0].pattern, "new")

    def test_file_permissions_are_preserved(self):
        self.write_custom_rules()
        self.rules_path.chmod(0o640)
        _change_rule(self.load_rules()[0], self.rules_path, delete=False)
        self.assertEqual(self.rules_path.stat().st_mode & 0o777, 0o640)

    def test_inline_rule_arrays_remain_supported(self):
        self.rules_path.write_text('rules = [{pattern="x", color="red"}] # keep\n')
        _write_ad_hoc_rule(self.rules_path, "y", "blue")
        _change_rule(self.load_rules()[1], self.rules_path, delete=False)
        self.assertTrue(self.load_rules()[1].disabled)
        _change_rule(self.load_rules()[0], self.rules_path, delete=True)
        self.assertEqual(self.load_rules()[0].pattern, "x")
        self.assertIn("# keep", self.rules_path.read_text())

    def test_listing_includes_disabled_rules_and_status(self):
        self.write_custom_rules()
        _show_rules(self.console, self.load_rules())
        output = self.output.getvalue()
        self.assertIn("Status", output)
        self.assertIn("disabled", output)
        self.assertIn("enabled", output)
        self.assertIn("literal", output)

    def test_selector_arrows_paging_and_enter(self):
        self.write_custom_rules()
        contents = self.rules_path.read_text().replace(
            'pattern = "literal"', 'name = "Literal match"\npattern = "literal"'
        )
        self.rules_path.write_text(contents)
        rules = self.load_rules()
        with patch("ctail.cli._read_rule_key", side_effect=[
            "up", "down", "down", "down", "up", "enter",
        ]):
            chosen = _select_rule(self.console, rules, 0, delete=False)
        self.assertEqual(chosen, rules[2])
        self.assertIn("Literal match", self.output.getvalue())

    def test_listing_includes_rule_name_and_pattern(self):
        self.rules_path.write_text(
            '[[rules]]\nname = "Request identifier"\n'
            'pattern = "request_id="\ncolor = "cyan"\n'
        )
        _show_rules(self.console, self.load_rules())
        output = self.output.getvalue()
        self.assertIn("Name", output)
        self.assertIn("Request identifier", output)
        self.assertIn("request_id=", output)

    def test_delete_selector_excludes_builtins(self):
        self.write_custom_rules()
        rules = self.load_rules()
        with patch("ctail.cli._read_rule_key", side_effect=["down"] * 20 + ["enter"]):
            chosen = _select_rule(self.console, rules, 0, delete=True)
        self.assertEqual(chosen, rules[2])
        self.assertIsNone(_select_rule(self.console, rules[3:], 0, delete=True))
        self.assertIn("No removable rules", self.output.getvalue())

    def test_escape_cancels_without_writing_or_changing_live_rules(self):
        self.write_custom_rules()
        for delete in (False, True):
            rules = self.load_rules()
            before = list(rules)
            contents = [path.read_bytes() for path in (self.rules_path, self.config_path)]
            with patch("ctail.cli._read_rule_key", return_value="cancel"):
                _manage_rules(self.console, rules, self.rules_path, 0, delete=delete)
            self.assertEqual(rules, before)
            self.assertEqual(contents, [
                path.read_bytes() for path in (self.rules_path, self.config_path)
            ])

    def test_key_decoder_arrows_enter_escape_and_eof(self):
        for sequence, expected in (
            (b"\x1b[A", "up"), (b"\x1b[B", "down"),
            (b"\x1bOA", "up"), (b"\x1bOB", "down"),
            (b"\r", "enter"), (b"\n", "enter"), (b"\x1b", "cancel"),
            (b"\x1b[", "cancel"), (b"", "cancel"), (b"x", ""),
        ):
            with self.subTest(sequence=sequence):
                read_fd, write_fd = os.pipe()
                try:
                    os.write(write_fd, sequence)
                    os.close(write_fd)
                    self.assertEqual(_read_rule_key(read_fd), expected)
                finally:
                    os.close(read_fd)

    def test_stream_dispatches_rule_management_keys(self):
        read_fd, write_fd = os.pipe()
        os.close(write_fd)
        try:
            with (
                patch("ctail.cli.select.select", return_value=([99], [], [])),
                patch("ctail.cli.os.read", side_effect=[b"r", b"D", b"q"]),
                patch("ctail.cli._manage_rules") as manage,
            ):
                _consume_stream(
                    read_fd, self.console, self.load_rules(), False, 99, self.rules_path
                )
            self.assertEqual(manage.call_count, 2)
            self.assertTrue(manage.call_args_list[0].kwargs["delete"])
            self.assertFalse(manage.call_args_list[1].kwargs["delete"])
        finally:
            os.close(read_fd)

    def test_help_key_shows_panel_and_waits_for_any_dismissal_key(self):
        read_fd, write_fd = os.pipe()
        os.close(write_fd)
        with (
            patch("ctail.cli.os.read", return_value=b"x") as read,
            patch("ctail.cli.Live") as live,
        ):
            from ctail.cli import _show_interactive_help

            _show_interactive_help(self.console, read_fd)
        read.assert_called_once_with(read_fd, 1)
        help_view = live.call_args.args[0]
        help_output = io.StringIO()
        Console(file=help_output, width=160, color_system=None).print(help_view)
        self.assertIn("ctail interactive commands", help_output.getvalue())
        self.assertIn("Press any key to return to tailing", help_output.getvalue())
        self.assertEqual(len(help_view.renderables), 3)
        self.assertIsInstance(help_view.renderables[1], Table)
        lines = help_output.getvalue().splitlines()
        self.assertTrue(lines[0].startswith("ctail interactive commands"))
        table_start = next(i for i, line in enumerate(lines) if line.startswith("┏"))
        table_end = next(i for i, line in enumerate(lines) if line.startswith("└"))
        self.assertTrue(lines[table_start].startswith("┏"))
        self.assertTrue(lines[table_end + 1].startswith("Press any key to return to tailing"))
        for command in ("Pause or resume", "Add a literal", "remove a custom", "enable or disable", "Show this command list", "Quit ctail"):
            self.assertIn(command, help_output.getvalue())
        os.close(read_fd)

    def read_until(self, master_fd, marker):
        output = bytearray()
        deadline = time.monotonic() + 8
        while marker not in output:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.fail(f"Timed out waiting for {marker!r}: {bytes(output)!r}")
            ready, _, _ = select.select([master_fd], [], [], remaining)
            if ready:
                output.extend(os.read(master_fd, 65536))
        return bytes(output)

    @contextmanager
    def running_app(self):
        log_path = self.home / "sample.log"
        log_path.touch()
        master_fd, slave_fd = pty.openpty()
        original_terminal = termios.tcgetattr(slave_fd)
        process = subprocess.Popen(
            [sys.executable, "-c", "from ctail.cli import main; main()", "-f", str(log_path)],
            stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
            env=dict(os.environ, HOME=str(self.home), TERM="xterm-256color"),
        )
        try:
            self.read_until(master_fd, b"Space: pause/resume")
            yield master_fd, log_path
            os.write(master_fd, b"q")
            process.wait(timeout=5)
            self.assertEqual(process.returncode, 0)
            self.assertEqual(termios.tcgetattr(slave_fd), original_terminal)
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
            os.close(master_fd)
            os.close(slave_fd)

    def test_pty_builtin_disable_enable_applies_to_live_stream(self):
        with self.running_app() as (master_fd, log_path):
            os.write(master_fd, b"d")
            self.read_until(master_fd, b"Enable/disable rule")
            os.write(master_fd, b"\r")
            self.read_until(master_fd, b"Disabled rule")
            self.assertTrue(self.load_rules()[0].disabled)
            with log_path.open("a") as log:
                log.write("[ERROR] hidden-error\n[INFO] visible-info\n")
            output = self.read_until(master_fd, b"visible-info")
            self.assertNotIn(b"hidden-error", output)
            os.write(master_fd, b"d")
            self.read_until(master_fd, b"Enable/disable rule")
            os.write(master_fd, b"\r")
            self.read_until(master_fd, b"Enabled rule")
            with log_path.open("a") as log:
                log.write("[ERROR] visible-error\n")
            self.read_until(master_fd, b"visible-error")
        self.assertFalse(self.load_rules()[0].disabled)

    def test_pty_arrow_deletion_and_escape_restore_terminal(self):
        self.write_custom_rules()
        before = self.rules_path.read_bytes()
        with self.running_app() as (master_fd, _):
            os.write(master_fd, b"r")
            self.read_until(master_fd, b"Delete rule")
            os.write(master_fd, b"\x1b")
            self.read_until(master_fd, b"Canceled.")
            self.assertEqual(self.rules_path.read_bytes(), before)
            os.write(master_fd, b"r")
            self.read_until(master_fd, b"Delete rule")
            os.write(master_fd, b"\x1b[B\x1b[B\x1b[A\r")
            self.read_until(master_fd, b"Deleted rule")
            self.assertNotIn("other", [rule.pattern for rule in self.load_rules()])
            self.assertIn("literal", [rule.pattern for rule in self.load_rules()])
            after_deletion = self.rules_path.read_bytes()
            os.write(master_fd, b"d")
            self.read_until(master_fd, b"Enable/disable rule")
            os.write(master_fd, b"\x1b")
            self.read_until(master_fd, b"Canceled.")
            self.assertEqual(self.rules_path.read_bytes(), after_deletion)
            os.write(master_fd, b"r")
            self.read_until(master_fd, b"Delete rule")
            os.write(master_fd, b"\x1b[B\r")
            self.read_until(master_fd, b"Deleted rule")
            self.assertNotIn("user", [rule.pattern for rule in self.load_rules()])

    def test_pty_help_panel_dismissal_returns_to_live_tailing(self):
        with self.running_app() as (master_fd, log_path):
            os.write(master_fd, b"?")
            panel = self.read_until(master_fd, b"Press any key to return to tailing")
            self.assertIn(b"ctail interactive commands", panel)
            self.assertIn(b"Pause or resume", panel)
            self.assertIn(b"Show this command list", panel)
            os.write(master_fd, b"x")
            with log_path.open("a") as log:
                log.write("[INFO] help dismissed\n")
            self.read_until(master_fd, b"help dismissed")


if __name__ == "__main__":
    unittest.main()
