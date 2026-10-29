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
from unittest.mock import MagicMock, patch

from rich.cells import cell_len
from rich.color import ANSI_COLOR_NAMES
from rich.console import Console
from rich.segment import Segment
from rich.style import Style
from rich.table import Table

from ctail.cli import (
    DEFAULT_RULES,
    _add_rule,
    _change_rule,
    _consume_stream,
    _EDITOR_COLORS,
    _RULE_EDITOR_COLORS,
    _RULE_EDITOR_FIELDS,
    _editor_change_style,
    _editor_style,
    _editor_text_window,
    _edit_rule,
    _load_settings,
    _manage_rules,
    _copy_ad_hoc_rule,
    _parse_rules,
    _read_rule_key,
    _rename_rule,
    _render_add_rule_editor,
    _render_line,
    _render_rule_manager,
    _render_rule_editor,
    _rule_editor,
    _RuleEditorState,
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
        self.config_path = self.config_dir / "ctail.toml"
        self.rules_path = self.config_path
        self.output = io.StringIO()
        self.console = Console(file=self.output, width=160, height=8, color_system=None)

    def load_rules(self):
        return _load_settings(self.config_dir)[1]

    def write_custom_rules(self):
        self.rules_path.write_text(
            '# main comment\nshow_unmatched = true # keep this\n'
            '# ad-hoc comment\nnote = "keep me"\n\n'
            '[[rules]]\npattern = "literal"\ncolor = "red"\nregex = false\nshared = true\n'
            '# disabled comment\ndisabled = true\n\n'
            '[[rules]]\npattern = "other"\ncolor = "green"\nshared = true\n\n'
            '[[rules]]\npattern = "user"\ncolor = "blue"\n\n'
            '[extra]\nvalue = 42\n'
        )

    def run_manager(self, rules, keys, profile=None):
        with (
            patch("ctail.cli._read_rule_key", side_effect=keys),
            patch("ctail.cli.Live") as live,
        ):
            _manage_rules(self.console, rules, self.rules_path, 0, profile=profile)
        for call in live.return_value.__enter__.return_value.update.call_args_list:
            self.console.print(call.args[0])

    def run_editor(self, rules, keys, profile=None, rule=None):
        live = MagicMock()
        with patch("ctail.cli._read_rule_key", side_effect=keys):
            message = _rule_editor(
                self.console, live, rules, self.rules_path, 0, profile, rule
            )
        return message, live

    def test_rule_dialogs_show_mode_style_and_preview_on_separate_rows(self):
        rule = _parse_rules([{"pattern": "example", "color": "red"}], self.rules_path)[0]
        for adding in (True, False):
            for height in (8, 12, 24):
                with self.subTest(adding=adding, height=height):
                    output = io.StringIO()
                    console = Console(file=output, width=160, height=height, color_system=None)
                    state = _RuleEditorState("example", "red on green", None, None if adding else rule)
                    view = (
                        _render_add_rule_editor(console, state) if adding
                        else _render_rule_editor(console, state)
                    )
                    console.print(view)
                    lines = output.getvalue().splitlines()
                    mode = next(index for index, line in enumerate(lines) if "Mode:" in line)
                    self.assertTrue(lines[mode + 1].lstrip(" >").startswith("Style:"))
                    self.assertTrue(lines[mode + 2].startswith("Preview:"))
                    self.assertEqual(lines[mode + 3], "")
                    self.assertLessEqual(len(lines), height)

    def test_add_dialog_shortcuts_and_default_only_scope(self):
        self.config_path.write_text("[profiles.other]\n")
        rules = self.load_rules()
        message, _ = self.run_editor(
            rules, list("example") + ["right", "down", "b", "right", "down", "i", "enter"]
        )
        self.assertEqual(message, "Added rule.")
        entry = tomllib.loads(self.config_path.read_text())["rules"][0]
        self.assertFalse(entry.get("shared", False))
        style = _editor_style(entry["color"])
        self.assertTrue(style.bold)
        self.assertTrue(style.reverse)
        self.assertEqual(style.color.name, "black")
        self.assertEqual(style.bgcolor.name, "black")
        self.assertNotIn("example", [rule.pattern for rule in _load_settings(self.config_dir, "other")[1]])

    def test_add_dialog_colors_layout_and_compact_display(self):
        self.assertEqual(
            _RULE_EDITOR_COLORS[:9],
            ("none", "black", "white", "red", "green", "blue", "yellow", "magenta", "cyan"),
        )
        for field in (1, 2):
            output = io.StringIO()
            console = Console(file=output, width=120, height=24, color_system=None)
            state = _RuleEditorState("x", "none", None, None, field=field)
            console.print(_render_add_rule_editor(console, state))
            text = output.getvalue()
            lines = text.splitlines()
            self.assertIn(">none", lines[2])
            self.assertIn("black", lines[3])
            self.assertIn("white", lines[4])
            self.assertIn("red", lines[5])
            self.assertIn("green", lines[6])
            self.assertIn("Style: none", text)
            for control in ("Scope:", "Bold:", "Inverse:", " Save", " Cancel"):
                self.assertNotIn(control, text)
        for width in (42, 60, 80, 160):
            with self.subTest(width=width):
                output = io.StringIO()
                console = Console(file=output, width=width, height=8, color_system=None)
                state = _RuleEditorState("x", "bold reverse bright_red on bright_white", None, None)
                state.message = "Could not save rule"
                console.print(_render_add_rule_editor(console, state))
                lines = output.getvalue().splitlines()
                self.assertLessEqual(len(lines), 8)
                self.assertIn("Could not save rule", output.getvalue())
        output = io.StringIO()
        console = Console(file=output, width=42, height=8, color_system=None)
        state = _RuleEditorState("x", "bold reverse bright_red on bright_white", None, None)
        state.message = "Could not save rule: invalid regex " + "x" * 50
        console.print(_render_add_rule_editor(console, state))
        self.assertLessEqual(len(output.getvalue().splitlines()), 8)
        self.assertIn("x" * 40, output.getvalue())

    def test_add_dialog_text_edit_regex_and_escape(self):
        rules = self.load_rules()
        message, _ = self.run_editor(
            rules, ["enter", "x", ".", "cancel", "x", "right", "enter"]
        )
        self.assertEqual(message, "Added rule.")
        self.assertEqual(rules[0].pattern, "x")
        self.assertFalse(rules[0].regex_enabled)
        message, live = self.run_editor(
            rules, ["enter", "a", "+", "enter", "right", "enter"]
        )
        self.assertEqual(message, "Added rule.")
        self.assertTrue(any(
            any("Mode: regex" in getattr(item, "plain", "")
                for item in call.args[0].renderables)
            for call in live.update.call_args_list[3:4]
        ))
        self.assertTrue(rules[0].regex_enabled)
        self.assertIsNotNone(_render_line("aaa", [rules[0]], False))

    def test_add_dialog_escape_discards_color_changes(self):
        for keys in (
            ["x", "right", "down", "b", "cancel"],
            ["x", "right", "right", "down", "i", "cancel"],
        ):
            with self.subTest(keys=keys):
                rules = self.load_rules()
                before = list(rules)
                message, _ = self.run_editor(rules, keys)
                self.assertEqual(message, "Canceled.")
                self.assertEqual(rules, before)
                self.assertFalse(self.config_path.exists())

    def test_add_dialog_automatic_regex_and_manual_override(self):
        for character in r".^$*+?{}[]\|()":
            with self.subTest(character=character):
                rules = self.load_rules()
                message, live = self.run_editor(rules, ["x", character, "cancel"])
                self.assertEqual(message, "Canceled.")
                frames = live.update.call_args_list
                self.assertTrue(any(
                    any("Mode: regex" in getattr(item, "plain", "")
                        for item in call.args[0].renderables)
                    for call in frames
                ))
        rules = self.load_rules()
        message, _ = self.run_editor(rules, ["x", ".", "down", "enter", "up", "enter"])
        self.assertEqual(message, "Added rule.")
        self.assertFalse(rules[0].regex_enabled)

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
        data = tomllib.loads(self.config_path.read_text())
        self.assertTrue(data["show_unmatched"])
        self.assertEqual(data["extra"]["value"], 42)

    def test_delete_custom_rules_from_both_files(self):
        self.write_custom_rules()
        for pattern in ("literal", "user"):
            rule = next(rule for rule in self.load_rules() if rule.pattern == pattern)
            _change_rule(rule, self.rules_path, delete=True)
            self.assertNotIn(pattern, [rule.pattern for rule in self.load_rules()])
        self.assertEqual(tomllib.loads(self.rules_path.read_text())["note"], "keep me")

    def test_profile_rule_can_be_toggled_and_deleted_in_place(self):
        self.config_path.write_text(
            '[[rules]]\npattern = "default"\ncolor = "red"\n\n'
            '[[rules]]\npattern = "shared-rule"\ncolor = "cyan"\nshared = true\n\n'
            "[profiles.nginx]\n"
            '[[profiles.nginx.rules]]\n'
            'pattern = "main-profile-rule"\ncolor = "yellow"\n'
            '[[profiles.nginx.rules]]\n'
            'pattern = "profile-rule"\ncolor = "green"\n\n'
            '[[profiles.nginx.rules]]\n'
            'pattern = "second-profile-rule"\ncolor = "blue"\n'
        )
        rule = next(
            rule for rule in _load_settings(self.config_dir, "nginx")[1]
            if rule.pattern == "profile-rule"
        )

        _change_rule(rule, self.rules_path, delete=False, profile="nginx")

        disabled = next(
            rule for rule in _load_settings(self.config_dir, "nginx")[1]
            if rule.pattern == "profile-rule"
        )
        self.assertTrue(disabled.disabled)
        _change_rule(disabled, self.rules_path, delete=True, profile="nginx")

        remaining = _load_settings(self.config_dir, "nginx")[1]
        self.assertNotIn("profile-rule", [rule.pattern for rule in remaining])
        self.assertIn("second-profile-rule", [rule.pattern for rule in remaining])
        self.assertEqual(
            tomllib.loads(self.rules_path.read_text())["profiles"]["nginx"]["rules"][0][
                "pattern"
            ],
            "main-profile-rule",
        )
        self.assertIn("main-profile-rule", [
            rule.pattern for rule in _load_settings(self.config_dir, "nginx")[1]
        ])
        self.assertIn("shared-rule", [
            rule.pattern for rule in _load_settings(self.config_dir, "nginx")[1]
        ])

    def test_interactive_rule_writer_scopes_rules_to_active_profile(self):
        self.config_path.write_text("[profiles.nginx]\n")
        self.rules_path.write_text('[[rules]]\npattern = "shared"\ncolor = "red"\n')

        _write_ad_hoc_rule(
            self.rules_path, "request_id=", "bright_cyan", profile_name="nginx"
        )

        data = tomllib.loads(self.rules_path.read_text())
        self.assertEqual(data["rules"][0]["pattern"], "shared")
        self.assertEqual(
            data["profiles"]["nginx"]["rules"][0]["pattern"], "request_id="
        )
        self.assertIn("request_id=", [
            rule.pattern for rule in _load_settings(self.config_dir, "nginx")[1]
        ])
        self.assertNotIn("request_id=", [
            rule.pattern for rule in _load_settings(self.config_dir)[1]
        ])

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
        entries = tomllib.loads((self.config_dir / "default.rules.toml").read_text())["rules"]
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

    def test_rename_changes_only_selected_name_and_preserves_comments(self):
        self.write_custom_rules()
        before = tomllib.loads(self.rules_path.read_text())
        rules = self.load_rules()
        self.run_manager(rules, ["down", "r", *"New name", "enter", "cancel"])
        expected = before
        expected["rules"][1]["name"] = "New name"
        self.assertEqual(tomllib.loads(self.rules_path.read_text()), expected)
        self.assertIn("# main comment", self.rules_path.read_text())
        self.assertIn("# disabled comment", self.rules_path.read_text())
        self.assertEqual(rules, self.load_rules())
        self.assertEqual(rules[1].name, "New name")
        self.assertIn("Renamed rule.", self.output.getvalue())
        self.assertIn("r: rename", self.output.getvalue())

    def test_rename_edits_prefilled_name_and_cancels(self):
        self.rules_path.write_text(
            '[[rules]]\nname="Old"\npattern="x"\ncolor="red"\n'
        )
        rules = self.load_rules()
        self.run_manager(
            rules, ["R", "left", "backspace", "a", "right", "!", "enter", "cancel"]
        )
        self.assertEqual(rules[0].name, "Oad!")
        before = self.rules_path.read_bytes()
        self.run_manager(rules, ["r", *"discard", "cancel", "cancel"])
        self.assertEqual(self.rules_path.read_bytes(), before)
        self.assertEqual(rules[0].name, "Oad!")

    def test_rename_empty_name_reports_error_and_allows_retry(self):
        self.write_custom_rules()
        rules = self.load_rules()
        self.run_manager(rules, ["r", "enter", " ", "enter", "N", "enter", "cancel"])
        self.assertIn("Rule name must not be empty", self.output.getvalue())
        self.assertEqual(rules[0].name, " N")

    def test_rename_scopes_and_inline_arrays_preserve_other_fields(self):
        for split in (False, True):
            for profile in (None, "web"):
                with self.subTest(split=split, profile=profile):
                    with tempfile.TemporaryDirectory() as directory:
                        config = Path(directory) / "ctail.toml"
                        entry = (
                            '{name="Old", pattern="same", color="red", regex=false, '
                            'disabled=true}'
                        )
                        if split:
                            config.write_text("")
                            path = config.with_name(f"{profile or 'default'}.rules.toml")
                            path.write_text(f"rules=[{entry}, {entry}]\n")
                        else:
                            path = config
                            section = "[profiles.web]\n" if profile else ""
                            path.write_text(section + f"rules=[{entry}, {entry}]\n")
                        rules = _load_settings(config.parent, profile)[1]
                        before = tomllib.loads(path.read_text())
                        _rename_rule(rules[1], config, "Renamed", profile=profile)
                        entries = (
                            before["profiles"]["web"]["rules"]
                            if profile and not split else before["rules"]
                        )
                        entries[1]["name"] = "Renamed"
                        self.assertEqual(tomllib.loads(path.read_text()), before)
                        self.assertEqual(
                            _load_settings(config.parent, profile)[1][1].name, "Renamed"
                        )

    def test_manager_hides_builtin_rules_and_cannot_rename_them(self):
        self.config_path.write_text("[profiles.web]\n")
        rules = _load_settings(self.config_dir, "web")[1]
        self.run_manager(rules, ["r", *"Error", "enter", "cancel"], profile="web")
        self.assertNotIn("built-in", self.output.getvalue())
        self.assertIsNone(rules[0].name)
        self.assertNotIn("rules", tomllib.loads(self.config_path.read_text())["profiles"]["web"])

    def test_rename_stale_rule_and_failed_save_preserve_data(self):
        self.write_custom_rules()
        rules = self.load_rules()
        before = list(rules)
        data = self.rules_path.read_bytes()
        with patch("ctail.cli.os.replace", side_effect=OSError("write denied")):
            self.run_manager(rules, ["r", *"New", "enter", "cancel", "cancel"])
        self.assertEqual(rules, before)
        self.assertEqual(self.rules_path.read_bytes(), data)
        self.assertIn("Could not rename rule: write denied", self.output.getvalue())
        self.rules_path.write_text(self.rules_path.read_text().replace('"literal"', '"edited"'))
        data = self.rules_path.read_bytes()
        with self.assertRaisesRegex(ValueError, "changed on disk"):
            _rename_rule(rules[0], self.rules_path, "New")
        self.assertEqual(self.rules_path.read_bytes(), data)

    def test_rename_reload_failure_reports_saved_state(self):
        self.write_custom_rules()
        rules = self.load_rules()
        before = list(rules)
        with patch("ctail.cli._load_settings", side_effect=[
            (False, rules, self.config_path), OSError("read denied")
        ]):
            self.run_manager(rules, ["r", *"New", "enter", "cancel"])
        self.assertEqual(rules, before)
        self.assertEqual(tomllib.loads(self.config_path.read_text())["rules"][0]["name"], "New")
        self.assertIn("Rule saved but could not reload: read denied", self.output.getvalue())

    def test_failed_save_preserves_disk_and_running_rules(self):
        self.write_custom_rules()
        rules = self.load_rules()
        before = list(rules)
        data = self.rules_path.read_bytes()
        with patch("ctail.cli.os.replace", side_effect=OSError("write denied")):
            self.run_manager(rules, ["x", "y", "cancel"])
        self.assertEqual(rules, before)
        self.assertEqual(self.rules_path.read_bytes(), data)
        self.assertIn("write denied", self.output.getvalue())
        self.assertEqual(sorted(path.name for path in self.config_dir.iterdir()),
                         ["ctail.toml"])

    def test_management_updates_live_rules_only_after_successful_save(self):
        self.write_custom_rules()
        rules = self.load_rules()
        self.run_manager(rules, ["d", "cancel"])
        self.assertFalse(rules[0].disabled)
        self.assertIsNotNone(_render_line("literal", rules, False))
        self.run_manager(rules, ["x", "y", "cancel"])
        self.assertIsNone(_render_line("literal", rules, False))
        self.assertEqual(rules, self.load_rules())
        self.assertEqual(rules[0].source_index, 0)

    def test_adding_live_rule_reloads_source_indices_and_preserves_disabled_state(self):
        self.write_custom_rules()
        rules = self.load_rules()
        terminal = [0, 0, 0, termios.ICANON | termios.ECHO, 0, 0, [0] * 32]
        with (
            patch("ctail.cli._read_rule_key", side_effect=[
                "enter", "n", "e", "w", "enter", "right", "enter",
            ]),
            patch("ctail.cli.Live"),
        ):
            _add_rule(self.console, rules, self.rules_path, 0, terminal)
        self.assertEqual(rules, self.load_rules())
        added = next(rule for rule in rules if rule.pattern == "new")
        disabled = next(rule for rule in rules if rule.pattern == "literal")
        self.assertEqual(added.source_index, 0)
        self.assertTrue(disabled.disabled)
        self.assertEqual(disabled.source_index, 1)
        _change_rule(disabled, self.rules_path, delete=True)
        self.assertIn("new", [rule.pattern for rule in self.load_rules()])

    def test_editor_add_uses_active_profile(self):
        self.config_path.write_text("[profiles.one]\n[profiles.two]\n")
        rules = _load_settings(self.config_dir, "one")[1]
        add = ["enter", "x", "enter"]
        message, _ = self.run_editor(rules, add + ["right", "enter"], "one")
        self.assertEqual(message, "Added rule.")
        data = tomllib.loads(self.rules_path.read_text())
        self.assertEqual(data["profiles"]["one"]["rules"][0]["pattern"], "x")
        self.assertNotIn("x", [rule.pattern for rule in _load_settings(self.config_dir, "two")[1]])

    def test_editor_add_regex_persists_and_matches(self):
        rules = self.load_rules()
        message, _ = self.run_editor(
            rules, list(r"error\d+") + ["right", "enter"]
        )
        self.assertEqual(message, "Added rule.")
        self.assertTrue(tomllib.loads((self.config_dir / "default.rules.toml").read_text())["rules"][0]["regex"])
        self.assertTrue(rules[0].regex_enabled)
        self.assertIsNotNone(_render_line("error123", [rules[0]], False))
        self.assertIsNone(_render_line(r"error\d+", [rules[0]], False))

    def test_editor_add_regex_to_named_profile(self):
        self.config_path.write_text("[profiles.one]\n")
        rules = _load_settings(self.config_dir, "one")[1]
        message, _ = self.run_editor(
            rules, list("x+") + ["right", "enter"], "one"
        )
        self.assertEqual(message, "Added rule.")
        entry = tomllib.loads(self.rules_path.read_text())["profiles"]["one"]["rules"][0]
        self.assertTrue(entry["regex"])
        self.assertIsNotNone(_render_line("xxx", [rules[0]], False))

    def test_editor_edit_can_switch_literal_and_regex(self):
        self.rules_path.write_text('[[rules]]\npattern="a+"\ncolor="red"\nregex=false\n')
        rules = self.load_rules()
        for expected in (True, False):
            with self.subTest(regex=expected):
                message, _ = self.run_editor(
                    rules, ["down", "enter", "up", "enter"], rule=rules[0]
                )
                self.assertEqual(message, "Edited rule.")
                self.assertEqual(rules[0].regex_enabled, expected)
                self.assertEqual(tomllib.loads(self.rules_path.read_text())["rules"][0]["regex"], expected)

    def test_editor_invalid_new_regex_does_not_save(self):
        rules = self.load_rules()
        message, live = self.run_editor(
            rules, ["[", "right", "enter", "cancel"]
        )
        self.assertEqual(message, "Canceled.")
        self.assertFalse(self.rules_path.exists())
        self.assertTrue(any(
            "invalid rule" in call.args[0].renderables[-1].plain
            for call in live.update.call_args_list
        ))

    def test_editor_builtin_regex_mode_cannot_be_changed(self):
        rules = self.load_rules()
        original = rules[0]
        message, live = self.run_editor(
            rules, ["down", "enter", "up", "enter"], rule=original
        )
        self.assertEqual(message, "Edited rule.")
        self.assertTrue(rules[0].regex_enabled)
        self.assertTrue(any(
            "must use regex" in call.args[0].renderables[-1].plain
            for call in live.update.call_args_list
        ))

    def test_editor_cancels_and_does_not_save_invalid_values(self):
        self.write_custom_rules()
        rules = self.load_rules()
        before, contents = list(rules), self.rules_path.read_bytes()
        message, live = self.run_editor(
            rules, ["right", "enter", "cancel"]
        )
        self.assertEqual(message, "Canceled.")
        self.assertEqual(rules, before)
        self.assertEqual(self.rules_path.read_bytes(), contents)
        frames = [call.args[0] for call in live.update.call_args_list]
        self.assertTrue(any("non-empty" in frame.renderables[-1].plain for frame in frames))

    def test_editor_edit_preserves_metadata_comments_and_regex(self):
        self.rules_path.write_text(
            '# comment\n[[rules]]\nname="Example"\npattern="a+"\n'
            'color="red"\nregex=true\ndisabled=true\nextra="keep"\n'
            '[[rules]]\npattern="a+"\ncolor="blue"\n'
        )
        rules = self.load_rules()
        message, _ = self.run_editor(
            rules, ["enter", "backspace", "b", "enter", "right", "enter"],
            rule=rules[0],
        )
        self.assertEqual(message, "Edited rule.")
        data = tomllib.loads(self.rules_path.read_text())
        self.assertEqual(data["rules"][0]["pattern"], "ab")
        self.assertEqual(data["rules"][1]["pattern"], "a+")
        self.assertEqual(data["rules"][0]["name"], "Example")
        self.assertEqual(data["rules"][0]["extra"], "keep")
        self.assertTrue(data["rules"][0]["regex"])
        self.assertTrue(data["rules"][0]["disabled"])
        self.assertIn("# comment", self.rules_path.read_text())

    def test_editor_edits_main_file_and_existing_builtin_override_in_place(self):
        self.config_path.write_text(
            '# keep comment\n[[rules]]\npattern="user"\ncolor="red"\nname="Example"\n'
            '\n[[rules]]\nbuiltin=true\n'
            'pattern=\'(?i)\\[(?:error|fatal)\\]\'\ncolor="blue"\ndisabled=true\n'
        )
        rules = self.load_rules()
        custom = next(rule for rule in rules if rule.pattern == "user")
        self.run_editor(rules, ["right", "down", "enter"], rule=custom)
        builtin = next(rule for rule in rules if rule.pattern == DEFAULT_RULES[0][0])
        self.run_editor(rules, ["right", "down", "enter"], rule=builtin)
        data = tomllib.loads(self.config_path.read_text())
        self.assertEqual(len(data["rules"]), 2)
        self.assertEqual(data["rules"][0]["name"], "Example")
        self.assertNotEqual(data["rules"][0]["color"], "red")
        self.assertNotEqual(data["rules"][1]["color"], "blue")
        self.assertTrue(data["rules"][1]["disabled"])
        self.assertIn("# keep comment", self.config_path.read_text())

    def test_editor_rejects_bad_regex_and_retains_original(self):
        self.rules_path.write_text('[[rules]]\npattern="x"\ncolor="red"\n')
        rules = self.load_rules()
        before = self.rules_path.read_bytes()
        message, live = self.run_editor(
            rules, ["enter", "[", "enter", "right", "enter", "cancel"],
            rule=rules[0],
        )
        self.assertEqual(message, "Canceled.")
        self.assertEqual(self.rules_path.read_bytes(), before)
        self.assertTrue(any(
            "invalid rule" in call.args[0].renderables[-1].plain
            for call in live.update.call_args_list
        ))

    def test_editor_complex_style_is_preserved_and_controls_do_not_simplify(self):
        color = "italic #123456 on blue, bold rgb(1,2,3)"
        self.rules_path.write_text(
            f'[[rules]]\npattern="(x)"\ncolor="{color}"\n'
        )
        rules = self.load_rules()
        message, live = self.run_editor(
            rules, ["right", "down", "enter"], rule=rules[0]
        )
        self.assertEqual(message, "Edited rule.")
        self.assertEqual(tomllib.loads(self.rules_path.read_text())["rules"][0]["color"], color)
        self.assertTrue(any(
            "Complex style" in call.args[0].renderables[-1].plain
            for call in live.update.call_args_list
        ))

    def test_edit_builtin_creates_override_in_current_profile_and_locks_pattern(self):
        self.config_path.write_text("[profiles.one]\n")
        rules = _load_settings(self.config_dir, "one")[1]
        rule = rules[0]
        message, live = self.run_editor(
            rules, ["enter", "right", "down", "enter"], "one", rule
        )
        self.assertEqual(message, "Edited rule.")
        entry = tomllib.loads(self.config_path.read_text())["profiles"]["one"]["rules"][0]
        self.assertTrue(entry["builtin"])
        self.assertEqual(entry["pattern"], rule.pattern)
        self.assertNotEqual(entry["color"], rule.color)
        self.assertEqual(self.load_rules()[0].color, rule.color)
        self.assertTrue(any(
            "cannot be changed" in call.args[0].renderables[-1].plain
            for call in live.update.call_args_list
        ))

    def test_editor_stale_rule_and_failed_save_preserve_data(self):
        self.write_custom_rules()
        rules = self.load_rules()
        old_rule = next(rule for rule in rules if rule.pattern == "literal")
        self.rules_path.write_text(self.rules_path.read_text().replace('"literal"', '"changed"'))
        contents = self.rules_path.read_bytes()
        message, live = self.run_editor(
            rules, ["right", "enter", "cancel"], rule=old_rule
        )
        self.assertEqual(message, "Canceled.")
        self.assertEqual(self.rules_path.read_bytes(), contents)
        self.assertTrue(any(
            "changed on disk" in call.args[0].renderables[-1].plain
            for call in live.update.call_args_list
        ))
        rules = self.load_rules()
        before = list(rules)
        with patch("ctail.cli.os.replace", side_effect=OSError("write denied")):
            message, live = self.run_editor(
                rules, ["right", "enter", "cancel"], rule=rules[0]
            )
        self.assertEqual(message, "Canceled.")
        self.assertEqual(self.rules_path.read_bytes(), contents)
        self.assertEqual(rules, before)
        self.assertTrue(any(
            "write denied" in call.args[0].renderables[-1].plain
            for call in live.update.call_args_list
        ))

    def test_editor_reports_reload_failure_after_successful_save(self):
        rules = self.load_rules()
        with patch("ctail.cli._load_settings", side_effect=[
            (False, rules, self.rules_path), OSError("reload failed")
        ]):
            message, _ = self.run_editor(
                rules, ["enter", "x", "enter", "right", "enter"]
            )
        self.assertIn("saved but could not reload: reload failed", message)
        self.assertEqual(
            tomllib.loads((self.config_dir / "default.rules.toml").read_text())["rules"][0]["pattern"], "x"
        )
        self.assertNotIn("x", [rule.pattern for rule in rules])

    def test_editor_text_edit_cursor_and_cancellation(self):
        rules = self.load_rules()
        message, _ = self.run_editor(
            rules, ["enter", "a", "b", "left", "é", "enter",
                    "enter", "x", "cancel", "right", "enter"]
        )
        self.assertEqual(message, "Added rule.")
        self.assertEqual(rules[0].pattern, "aéb")

    def test_editor_accepts_direct_pattern_input_and_navigation(self):
        rules = self.load_rules()
        message, _ = self.run_editor(
            rules, list("caféx") + ["backspace", "right", "b", "right", "i", "enter"]
        )
        self.assertEqual(message, "Added rule.")
        self.assertEqual(rules[0].pattern, "café")
        style = _editor_style(rules[0].color)
        self.assertIsNotNone(style)
        self.assertTrue(style.bold)
        self.assertTrue(style.reverse)

    def test_editor_direct_typing_does_not_change_builtin_pattern(self):
        rules = self.load_rules()
        original = rules[0]
        message, live = self.run_editor(rules, ["x", "backspace", "cancel"], rule=original)
        self.assertEqual(message, "Canceled.")
        self.assertEqual(rules[0], original)
        self.assertTrue(any(
            "cannot be changed" in call.args[0].renderables[-1].plain
            for call in live.update.call_args_list
        ))

    def test_editor_can_toggle_bold_and_inverse_back_off(self):
        rules = self.load_rules()
        message, _ = self.run_editor(
            rules, ["x", "right", "b", "b", "right", "i", "i", "enter"]
        )
        self.assertEqual(message, "Added rule.")
        style = _editor_style(rules[0].color)
        self.assertIsNotNone(style)
        self.assertIsNone(style.bold)
        self.assertIsNone(style.reverse)
        self.assertEqual(rules[0].color, "none")
        self.assertEqual(
            tomllib.loads((self.config_dir / "default.rules.toml").read_text())["rules"][0]["color"], "none"
        )

    def test_editor_toggling_off_only_unsets_selected_attribute(self):
        for field, initial, expected in (
            ("Bold", "bold reverse red on blue", "reverse red on blue"),
            ("Inverse", "bold reverse red on blue", "bold red on blue"),
            ("Bold", "bold not reverse red on blue", "not reverse red on blue"),
            ("Inverse", "not bold reverse red on blue", "not bold red on blue"),
        ):
            with self.subTest(field=field, initial=initial):
                state = _RuleEditorState("x", initial, None, None)
                _editor_change_style(state, field, 1)
                self.assertEqual(state.color, expected)

    def test_editor_preserves_explicit_negative_attributes_on_color_change(self):
        state = _RuleEditorState("x", "not bold not reverse yellow", None, None)
        _editor_change_style(state, "Foreground", 1)
        style = _editor_style(state.color)
        self.assertIsNotNone(style)
        self.assertIs(style.bold, False)
        self.assertIs(style.reverse, False)

    def test_editor_table_has_fixed_columns_and_pattern_selection_marker(self):
        expected_width = max(map(cell_len, _EDITOR_COLORS)) + 1
        console = Console(file=io.StringIO(), width=100, height=24, color_system=None)
        for field in range(3):
            with self.subTest(field=field):
                state = _RuleEditorState("x", "red on blue", None, None, field=field)
                view = _render_rule_editor(console, state)
                table = view.renderables[0]
                self.assertEqual([column.width for column in table.columns], [55, expected_width, expected_width])
                output = io.StringIO()
                Console(file=output, width=100, height=24, color_system=None).print(table)
                selected_line = next(line for line in output.getvalue().splitlines() if ">" in line)
                self.assertEqual(selected_line.index(">"), (0, 55, 55 + expected_width)[field])

    def test_editor_pattern_column_displays_forty_characters(self):
        pattern = "abcdefghijklmnopqrstuvwxyz0123456789ABCD"
        output = io.StringIO()
        console = Console(file=output, width=100, height=24, color_system=None)
        table = _render_rule_editor(console, _RuleEditorState(pattern, "red", None, None)).renderables[0]
        console.print(table)
        self.assertIn(pattern, output.getvalue())

    def test_editor_narrow_terminal_keeps_pattern_cursor_and_color_headers_visible(self):
        pattern = "abcdefghij" * 10
        rule = _parse_rules(
            [{"pattern": pattern, "color": "red on blue", "regex": False}], self.rules_path
        )[0]
        for width in (42, 60, 80):
            for cursor in (0, 50, 100):
                with self.subTest(width=width, cursor=cursor):
                    output = io.StringIO()
                    console = Console(file=output, width=width, height=24, color_system=None)
                    state = _RuleEditorState(
                        pattern, "red on blue", None, rule,
                        editing=True, text=pattern, cursor=cursor,
                    )
                    table = _render_rule_editor(console, state).renderables[0]
                    console.print(table)
                    lines = output.getvalue().splitlines()
                    self.assertIn("Edit rule", lines[0])
                    self.assertIn("Foreground", lines[1])
                    self.assertIn("Background", lines[1])
                    pattern_width = table.columns[0].width
                    self.assertEqual(pattern_width, {42: 22, 60: 40, 80: 55}[width])
                    selected = next(line for line in lines if line.startswith(">"))
                    self.assertIn("|", selected[:pattern_width])
                    self.assertLessEqual(max(map(cell_len, lines)), width)

    def test_add_and_edit_render_the_same_form(self):
        rule = _parse_rules(
            [{"pattern": "example", "color": "red on green"}], self.rules_path
        )[0]
        add_output = io.StringIO()
        edit_output = io.StringIO()
        add_console = Console(file=add_output, width=100, height=24, color_system=None)
        edit_console = Console(file=edit_output, width=100, height=24, color_system=None)
        add_console.print(_render_add_rule_editor(
            add_console, _RuleEditorState("example", "red on green", None, None)
        ))
        edit_console.print(_render_rule_editor(
            edit_console, _RuleEditorState("example", "red on green", None, rule)
        ))
        add_lines = add_output.getvalue().splitlines()
        edit_lines = edit_output.getvalue().splitlines()
        self.assertIn("Add rule", add_lines[0])
        self.assertIn("Edit rule", edit_lines[0])
        self.assertEqual(add_lines[1:], edit_lines[1:])
        self.assertFalse(any(
            label in edit_output.getvalue()
            for label in ("Scope:", "Bold:", "Inverse:", " Save", " Cancel")
        ))

    def test_edit_navigation_and_shortcuts_use_add_fields(self):
        self.rules_path.write_text('[[rules]]\npattern="x"\ncolor="red"\n')
        rules = self.load_rules()
        rule = next(rule for rule in rules if rule.pattern == "x")
        fields = []

        def capture(console, state):
            fields.append(_RULE_EDITOR_FIELDS[state.field])
            return _render_rule_editor(console, state)

        with patch("ctail.cli._render_rule_editor", side_effect=capture):
            message, _ = self.run_editor(
                rules, ["right", "b", "right", "i", "right", "enter", "up", "enter"],
                rule=rule,
            )
        self.assertEqual(message, "Edited rule.")
        self.assertEqual(fields[:6], [
            "Pattern", "Foreground", "Foreground", "Background", "Background", "Mode",
        ])
        style = _editor_style(next(rule for rule in rules if rule.pattern == "x").color)
        self.assertTrue(style.bold)
        self.assertTrue(style.reverse)

    def test_editor_layout_and_instructions(self):
        output = io.StringIO()
        console = Console(file=output, width=100, height=24, color_system=None)
        state = _RuleEditorState("aaaaaaa", "yellow", "eventsd", None, field=1)
        console.print(_render_rule_editor(console, state))
        lines = output.getvalue().splitlines()
        self.assertIn("Add rule", lines[0])
        self.assertEqual(lines[1].split(), ["Pattern", "Foreground", "Background"])
        self.assertEqual(len(lines[2:7]), 5)
        self.assertIn("aaaaaaa", lines[4])
        self.assertIn(">yellow", lines[4])
        self.assertEqual(
            lines[7:],
            [
                " Mode: literal", " Style: yellow", "Preview: aaaaaaa", "",
                "Arrows: fields/colors; Pattern Down: mode; Mode Enter: toggle; Up: colors",
                "Foreground b: bold; Background i: inverse; Color Enter: save; Esc: discard",
            ],
        )

    def test_editor_short_terminal_reduces_color_rows_and_keeps_notice(self):
        output = io.StringIO()
        console = Console(file=output, width=100, height=20, color_system=None)
        state = _RuleEditorState("x", "yellow", None, None, field=1)
        state.message = "Could not save rule"
        view = _render_rule_editor(console, state)
        console.print(view)
        lines = output.getvalue().splitlines()
        self.assertLessEqual(len(lines), 20)
        self.assertIn("Could not save rule", lines[-1])

    def test_editor_text_editing_instructions_are_separate(self):
        output = io.StringIO()
        console = Console(file=output, width=100, height=24, color_system=None)
        state = _RuleEditorState("x", "yellow", "eventsd", None, editing=True, text="x", cursor=1)
        console.print(_render_rule_editor(console, state))
        lines = output.getvalue().splitlines()
        self.assertIn("Editing pattern: Left/Right move; Backspace deletes", lines)
        self.assertIn("Enter: accept text; Esc: discard text", lines)

    def test_editor_uses_compact_layout_for_very_short_terminal(self):
        output = io.StringIO()
        console = Console(file=output, width=160, height=8, color_system=None)
        state = _RuleEditorState("x", "yellow", "eventsd", None, field=3)
        state.message = "Could not save rule"
        view = _render_rule_editor(console, state)
        self.assertEqual(view.renderables[0].row_count, 1)
        console.print(view)
        lines = output.getvalue().splitlines()
        self.assertLessEqual(len(lines), 8)
        self.assertIn("Mode: literal", output.getvalue())
        self.assertIn("Style: yellow", output.getvalue())
        self.assertIn("Preview: x", output.getvalue())
        self.assertIn("Could not save rule", lines)

    def test_editor_long_text_keeps_cursor_visible(self):
        for text, cursor in (("x" * 100, 100), ("x" * 100, 50), ("日" * 100, 100)):
            with self.subTest(text=text, cursor=cursor):
                window = _editor_text_window(text, cursor, 12)
                self.assertIn("|", window)
                self.assertLessEqual(cell_len(window), 12)

    def test_editor_colors_and_visual_navigation(self):
        self.assertTrue(set(ANSI_COLOR_NAMES).issubset(_EDITOR_COLORS))
        self.assertIn("default", _EDITOR_COLORS)
        state = _RuleEditorState("x", "red on blue", None, None, field=1)
        view = _render_rule_editor(Console(file=io.StringIO(), width=100, height=24), state)
        self.assertEqual(view.renderables[0].row_count, 5)
        rules = self.load_rules()
        message, _ = self.run_editor(
            rules, ["enter", "x", "enter", "right", "down", "b", "right", "down", "i", "enter"]
        )
        self.assertEqual(message, "Added rule.")
        style = _editor_style(rules[0].color)
        self.assertIsNotNone(style)
        self.assertTrue(style.bold)
        self.assertTrue(style.reverse)
        self.assertIsNotNone(style.bgcolor)

    def test_manager_add_and_edit_share_editor(self):
        rules = self.load_rules()
        self.run_manager(
            rules, ["a", "enter", "x", "enter", "right", "enter",
                    "e", "enter", "y", "enter", "right", "enter", "cancel"]
        )
        self.assertEqual(rules[0].pattern, "xy")
        self.assertIn("Added rule.", self.output.getvalue())
        self.assertIn("Edited rule.", self.output.getvalue())

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

    def test_copy_ad_hoc_rule_to_profile_preserves_fields_and_source(self):
        self.rules_path.write_text(
            '# retained\n[[rules]]\nname = "first"\npattern = "same"\n'
            'color = "red"\nregex = false\nshared = true\n\n'
            '[[rules]]\nname = "second"\npattern = "same"\n'
            'color = "blue"\nregex = true\ndisabled = true\nextra = "keep"\nshared = true\n'
            '[profiles.one]\n[profiles.two]\n'
        )
        self.rules_path.chmod(0o640)
        rule = _load_settings(self.config_dir, "one")[1][1]
        self.assertTrue(_copy_ad_hoc_rule(rule, self.rules_path, "two", profile="one"))
        data = tomllib.loads(self.rules_path.read_text())
        self.assertEqual(data["rules"][0]["name"], "second")
        moved = data["profiles"]["two"]["rules"][0]
        self.assertEqual(moved, {
            "name": "second", "pattern": "same", "color": "blue",
            "regex": True, "disabled": True, "extra": "keep",
        })
        self.assertIn("# retained", self.rules_path.read_text())
        self.assertEqual(self.rules_path.stat().st_mode & 0o777, 0o640)
        self.assertIn("second", [
            item.name for item in _load_settings(self.config_dir, "one")[1]
        ])
        self.assertFalse(_copy_ad_hoc_rule(rule, self.rules_path, "two", profile="one"))
        scoped = next(
            item for item in _load_settings(self.config_dir, "two")[1]
            if item.name == "second"
        )
        self.assertFalse(_copy_ad_hoc_rule(scoped, self.rules_path, "two", profile="two"))

    def test_copy_supports_inline_and_array_of_tables(self):
        self.rules_path.write_text(
            'rules = [{pattern="x", color="red", shared=true}] # shared\n'
            '[profiles.one]\n[[profiles.one.rules]]\npattern="y"\ncolor="blue"\n'
        )
        rules = _load_settings(self.config_dir, "one")[1]
        self.assertTrue(_copy_ad_hoc_rule(rules[0], self.rules_path, "one", profile="one"))
        data = tomllib.loads(self.rules_path.read_text())
        self.assertEqual(data["rules"][0]["pattern"], "x")
        self.assertEqual(data["profiles"]["one"]["rules"][0]["pattern"], "x")
        self.assertEqual(data["profiles"]["one"]["rules"][1]["pattern"], "y")
        self.assertIn("# shared", self.rules_path.read_text())

    def test_copy_rejects_stale_invalid_and_failed_writes(self):
        self.rules_path.write_text('[[rules]]\npattern="x"\ncolor="red"\n[profiles.one]\n')
        rule = self.load_rules()[0]
        with self.assertRaisesRegex(ValueError, "no longer exists"):
            _copy_ad_hoc_rule(rule, self.rules_path, "missing")
        before = self.rules_path.read_bytes()
        with patch("ctail.cli.os.replace", side_effect=OSError("write denied")):
            with self.assertRaisesRegex(OSError, "write denied"):
                _copy_ad_hoc_rule(rule, self.rules_path, "one")
        self.assertEqual(self.rules_path.read_bytes(), before)
        self.rules_path.write_text(self.rules_path.read_text().replace('"x"', '"edited"'))
        before = self.rules_path.read_bytes()
        with self.assertRaisesRegex(ValueError, "changed on disk"):
            _copy_ad_hoc_rule(rule, self.rules_path, "one")
        self.assertEqual(self.rules_path.read_bytes(), before)

    def test_manager_copy_cancel_and_failed_save_preserve_state(self):
        self.config_path.write_text("[profiles.one]\n[profiles.two]\n")
        self.rules_path.write_text('[[rules]]\npattern="x"\ncolor="red"\n')
        rules = self.load_rules()
        before_rules = list(rules)
        before_file = self.rules_path.read_bytes()
        self.run_manager(rules, ["c", "enter", "n", "cancel"])
        self.assertEqual(rules, before_rules)
        self.assertEqual(self.rules_path.read_bytes(), before_file)
        with patch("ctail.cli.os.replace", side_effect=OSError("write denied")):
            self.run_manager(rules, ["c", "enter", "y", "cancel"])
        self.assertIn("write denied", self.output.getvalue())
        self.assertEqual(rules, before_rules)
        self.assertEqual(self.rules_path.read_bytes(), before_file)

    def test_manager_copy_defaults_to_active_profile_and_keeps_source(self):
        self.rules_path.write_text(
            '[[rules]]\npattern="x"\ncolor="red"\nshared=true\n'
            '[profiles.one]\n[profiles.two]\n'
        )
        rules = _load_settings(self.config_dir, "one")[1]
        self.run_manager(rules, ["c", "enter", "y", "cancel"], profile="one")
        self.assertIn("Copied rule.", self.output.getvalue())
        self.assertIn("x", [rule.pattern for rule in rules])
        self.assertIn("x", [rule.pattern for rule in _load_settings(self.config_dir, "two")[1]])
        self.assertTrue(any(rule.profile_name == "one" for rule in rules if rule.pattern == "x"))

    def test_manager_copy_omits_current_profile_from_destinations(self):
        self.config_path.write_text("[profiles.one]\n[profiles.two]\n")
        self.rules_path.write_text(
            '[[profiles.one.rules]]\npattern="x"\ncolor="red"\n'
        )
        rules = _load_settings(self.config_dir, "one")[1]
        before_file = self.rules_path.read_bytes()
        self.run_manager(rules, ["c", "cancel", "cancel"], profile="one")
        self.assertIn("two", self.output.getvalue())
        self.assertNotIn("one", self.output.getvalue())
        self.assertEqual(self.rules_path.read_bytes(), before_file)

    def test_manager_hides_builtin_overrides_and_does_not_toggle_them(self):
        self.config_path.write_text("[profiles.one]\n")
        rules = _load_settings(self.config_dir, "one")[1]
        _change_rule(rules[0], self.rules_path, delete=False, profile="one")
        rules = _load_settings(self.config_dir, "one")[1]
        self.run_manager(rules, ["d", "cancel"], profile="one")
        self.assertTrue(rules[0].disabled)
        self.assertFalse(self.load_rules()[0].disabled)
        self.assertTrue(tomllib.loads(self.config_path.read_text())[
            "profiles"]["one"]["rules"][0]["disabled"])
        self.assertNotIn("built-in", self.output.getvalue())

    def test_manager_table_aligns_columns_and_fits_terminal(self):
        self.rules_path.write_text(
            '[[rules]]\nname="Alpha"\npattern="short"\ncolor="red"\nregex=false\n'
            '[[rules]]\nname="Beta"\npattern="longer"\ncolor="blue"\nregex=true\n'
        )
        output = io.StringIO()
        console = Console(file=output, width=160, height=20, color_system=None)
        console.print(_render_rule_manager(console, self.load_rules(), 0, ""))
        lines = output.getvalue().splitlines()
        first = next(line for line in lines if "Alpha" in line)
        second = next(line for line in lines if "Beta" in line)
        for first_value, second_value in (
            ("enabled", "enabled"), ("ctail.toml", "ctail.toml"), ("-", "-"),
            ("literal", "regex"), ("Alpha", "Beta"), ("short", "longer"),
            ("red", "blue"),
        ):
            self.assertEqual(first.rindex(first_value), second.rindex(second_value))
        narrow = io.StringIO()
        small_console = Console(file=narrow, width=70, height=20, color_system=None)
        small_console.print(_render_rule_manager(small_console, self.load_rules(), 0, ""))
        self.assertTrue(all(len(line) <= 70 for line in narrow.getvalue().splitlines()))
        self.assertIn("enabled", narrow.getvalue())
        self.assertIn("ctail.toml", narrow.getvalue())
        self.assertIn("literal", narrow.getvalue())

    def test_manager_pads_selected_cells_and_previews_to_column_width(self):
        rules = _parse_rules(
            [
                {"name": "界", "pattern": "x", "color": "red on white"},
                {
                    "name": "longer name",
                    "pattern": "longer pattern",
                    "color": "blue on white",
                    "disabled": True,
                    "regex": False,
                },
            ],
            self.rules_path,
        )
        for selected in (0, 1):
            with self.subTest(selected=selected):
                console = Console(width=160, height=20, color_system="standard")
                table = _render_rule_manager(console, rules, selected, "").renderables[0]
                widths = table._calculate_column_widths(console, console.options)
                lines = console.render_lines(table, pad=False)
                for index, rule in enumerate(rules):
                    line = next(
                        line for line in lines
                        if any(segment.text.rstrip() == rule.pattern for segment in line)
                    )
                    offset = 0
                    for column, width in enumerate(widths):
                        # The table's leading space separates adjacent cells.
                        cell = list(Segment.divide(line, [offset + 1, offset + width]))[1]
                        expected = (
                            Style.parse(rule.color) if column >= 6
                            else Style(reverse=True) if index == selected else Style()
                        )
                        self.assertEqual(
                            sum(segment.cell_length for segment in cell), width - 1,
                        )
                        for segment in cell:
                            self.assertEqual(segment.style or Style(), expected)
                        offset += width

    def test_manager_preserves_preview_colors_when_selected(self):
        for color in ("yellow on bright_black", "reverse yellow on bright_black"):
            for selected in (0, 1):
                with self.subTest(color=color, selected=selected):
                    rules = _parse_rules(
                        [
                            {"pattern": "PASOLINK", "color": color},
                            {"pattern": "other", "color": "red"},
                        ],
                        self.rules_path,
                    )
                    console = Console(width=160, height=20, color_system="standard")
                    table = _render_rule_manager(console, rules, selected, "").renderables[0]
                    segments = list(console.render(table))
                    for value in ("PASOLINK", color):
                        previews = [
                            segment for segment in segments if segment.text.rstrip() == value
                        ]
                        self.assertTrue(previews)
                        for segment in previews:
                            self.assertEqual(segment.style, Style.parse(color))
                    markers = [segment for segment in segments if segment.text.strip() == ">"]
                    self.assertEqual(len(markers), 1)
                    self.assertTrue(markers[0].style.reverse)

    def test_manager_displays_pattern_without_python_repr(self):
        for pattern in ("PASOLINK", r"\d{4}-\d{2}", "'quoted'", 'a"b', "[WARNING]"):
            with self.subTest(pattern=pattern):
                rules = _parse_rules(
                    [{"pattern": pattern, "color": "yellow on bright_black", "regex": False}],
                    self.rules_path,
                )
                console = Console(width=160, height=20, color_system="standard")
                table = _render_rule_manager(console, rules, 0, "").renderables[0]
                segments = list(console.render(table))
                previews = [
                    segment.text.rstrip() for segment in segments
                    if segment.style == Style.parse("yellow on bright_black")
                ]
                self.assertEqual(previews, [pattern, "yellow on bright_black"])

    def test_manager_pattern_column_fits_rendered_content_plus_one(self):
        for pattern in ("short", "x" * 25, "界" * 12, "line\nbreak" * 3):
            with self.subTest(pattern=pattern):
                rules = _parse_rules(
                    [{"pattern": pattern, "color": "red", "regex": False}],
                    self.rules_path,
                )
                output = io.StringIO()
                console = Console(file=output, width=160, height=20, color_system=None)
                console.print(_render_rule_manager(console, rules, 0, "").renderables[0])
                lines = output.getvalue().splitlines()
                header = next(line for line in lines if "Pattern" in line)
                width = header.index("Style") - header.index("Pattern") - 1
                self.assertEqual(
                    width, max(cell_len("Pattern"), *(cell_len(line)
                                                     for line in pattern.split("\n"))) + 1,
                )
                for line in pattern.split("\n"):
                    self.assertIn(line, output.getvalue())

    def test_manager_columns_fit_longest_displayed_content_plus_one(self):
        source = self.config_dir / ("long-source-" + "x" * 20 + ".rules")
        rules = _parse_rules(
            [
                {
                    "name": "界" * 12,
                    "pattern": "x" * 35,
                    "color": "bold bright_red on bright_white",
                    "regex": False,
                }
            ],
            source,
            profile_name="long-profile-" + "y" * 20,
        )
        output = io.StringIO()
        console = Console(file=output, width=300, height=20, color_system=None)
        table = _render_rule_manager(console, rules, 0, "").renderables[0]
        console.print(table)
        expected = (
            ("", ">"), ("Status", "enabled"), ("Source", source.name),
            ("Profile", rules[0].profile_name), ("Type", "literal"),
            ("Name", rules[0].name), ("Pattern", rules[0].pattern),
            ("Style", rules[0].color),
        )
        for column, (heading, value) in zip(table.columns, expected):
            with self.subTest(heading=heading):
                self.assertEqual(column.max_width, max(cell_len(heading), cell_len(value)) + 1)
                self.assertIn(value, output.getvalue())
        header = next(line for line in output.getvalue().splitlines() if "Status" in line)
        selected_row = next(line for line in output.getvalue().splitlines() if "enabled" in line)
        starts = [selected_row.index(">")] + [header.index(heading) for heading, _ in expected[1:]]
        for index, (_, value) in enumerate(expected[:-1]):
            self.assertEqual(
                starts[index + 1] - starts[index] - 1,
                max(cell_len(expected[index][0]), cell_len(value)) + 1,
            )

    def test_manager_wraps_long_source_profile_and_style_on_narrow_terminal(self):
        source = self.config_dir / ("long-source-" + "x" * 20 + ".rules")
        rules = _parse_rules(
            [{"pattern": "x", "color": "bold bright_red on bright_white"}],
            source,
            profile_name="long-profile-" + "y" * 20,
        )
        output = io.StringIO()
        console = Console(file=output, width=70, height=20, color_system=None)
        console.print(_render_rule_manager(console, rules, 0, "").renderables[0])
        lines = output.getvalue().splitlines()
        header_index = next(i for i, line in enumerate(lines) if "Profile" in line)
        header = lines[header_index]
        source_start, profile_start, type_start, style_start = (
            header.index("Source"), header.index("Profile"),
            header.index("Type"), header.index("Style"),
        )
        rows = lines[header_index + 1:]
        for start, end, value in (
            (source_start, profile_start, source.name),
            (profile_start, type_start, rules[0].profile_name),
            (style_start, None, rules[0].color),
        ):
            actual = "".join(line[start:end].strip() for line in rows)
            self.assertEqual(actual.replace(" ", ""), value.replace(" ", ""))
        self.assertTrue(all(cell_len(line) <= 70 for line in lines))

    def test_manager_does_not_display_builtin_overrides(self):
        self.config_path.write_text("[profiles.asaklogs]\n")
        rules = _load_settings(self.config_dir, "asaklogs")[1]
        _change_rule(rules[0], self.rules_path, delete=False, profile="asaklogs")
        rules = _load_settings(self.config_dir, "asaklogs")[1]
        self.assertEqual(rules[0].profile_name, "asaklogs")
        output = io.StringIO()
        console = Console(file=output, width=160, height=20, color_system=None)
        console.print(_render_rule_manager(console, rules, 0, "").renderables[0])
        self.assertNotIn("built-in", output.getvalue())
        self.assertNotIn(DEFAULT_RULES[0][0], output.getvalue())
        _change_rule(rules[0], self.rules_path, delete=False, profile="asaklogs")
        self.assertFalse(_load_settings(self.config_dir, "asaklogs")[1][0].disabled)
        entries = tomllib.loads(self.config_path.read_text())["profiles"]["asaklogs"]["rules"]
        self.assertEqual(len(entries), 1)
        self.assertFalse(entries[0]["disabled"])

    def test_manager_builtin_file_override_stays_in_profile_when_edited(self):
        self.config_path.write_text("")
        profile_path = self.config_dir / "asaklogs.rules.toml"
        profile_path.write_text(
            f"[[rules]]\nbuiltin=true\npattern='{DEFAULT_RULES[0][0]}'\n"
            'color="bold bright_red on bright_white"\n'
        )
        rules = _load_settings(self.config_dir, "asaklogs")[1]
        output = io.StringIO()
        console = Console(file=output, width=160, height=20, color_system=None)
        console.print(_render_rule_manager(console, rules, 0, "").renderables[0])
        self.assertNotIn("built-in", output.getvalue())
        self.assertNotIn(DEFAULT_RULES[0][0], output.getvalue())
        _edit_rule(rules[0], self.rules_path, rules[0].pattern, "green", profile="asaklogs")
        self.assertEqual(_load_settings(self.config_dir, "asaklogs")[1][0].color, "green")
        self.assertEqual(self.load_rules()[0].color, DEFAULT_RULES[0][1])
        self.assertEqual(len(tomllib.loads(profile_path.read_text())["rules"]), 1)

    def test_manager_displays_patterns_with_crowded_columns(self):
        patterns = ["log data result:", "GET log_analysis request", r"\w+\s+\d+\s+\w+"]
        rules = _parse_rules(
            [
                {"pattern": pattern, "name": "Authentication failure request", "color": "red"}
                for pattern in patterns
            ],
            self.rules_path,
        )
        rules.extend(self.load_rules())
        output = io.StringIO()
        console = Console(file=output, width=100, height=30, color_system=None)
        console.print(_render_rule_manager(console, rules, 0, "").renderables[0])
        lines = output.getvalue().splitlines()
        header = next(line for line in lines if "Pattern" in line)
        name_start, pattern_start, style_start = (
            header.index("Name"), header.index("Pattern"), header.index("Style")
        )
        rows = []
        for line in lines[lines.index(header) + 1:]:
            if "ctail.toml" in line:
                rows.append(["", ""])
            elif "built-in" in line:
                break
            if rows:
                rows[-1][0] += line[name_start:pattern_start].strip()
                rows[-1][1] += line[pattern_start:style_start].strip()
        self.assertEqual(len(rows), len(patterns))
        for (name, pattern_text), pattern in zip(rows, patterns):
            with self.subTest(pattern=pattern):
                self.assertEqual(name.replace(" ", ""), "Authenticationfailurerequest")
                self.assertEqual(pattern_text.replace(" ", ""), pattern.replace(" ", ""))

    def test_manager_uses_available_terminal_width_for_long_patterns(self):
        pattern = "P" * 80
        rules = _parse_rules(
            [{"pattern": pattern, "name": "N" * 40, "color": "red", "regex": False}],
            self.rules_path,
        )
        output = io.StringIO()
        console = Console(file=output, width=180, height=30, color_system=None)
        console.print(_render_rule_manager(console, rules, 0, "").renderables[0])
        self.assertIn(pattern, output.getvalue())

    def test_manager_table_fits_content_and_terminal_width(self):
        self.rules_path.write_text(
            '[[rules]]\nname="Alpha"\npattern="short"\ncolor="red"\nregex=false\n'
        )
        rules = self.load_rules()
        outputs = []
        for width in (160, 240):
            output = io.StringIO()
            console = Console(file=output, width=width, height=20, color_system=None)
            console.print(_render_rule_manager(console, rules, 0, "").renderables[0])
            outputs.append(output.getvalue())
        self.assertEqual(outputs[0], outputs[1])
        self.assertLess(max(map(len, outputs[0].splitlines())), 160)

        self.rules_path.write_text(
            f'[[rules]]\nname="{"N" * 60}"\npattern="{"P" * 80}"\n'
            'color="red"\nregex=false\n'
        )
        output = io.StringIO()
        console = Console(file=output, width=240, height=20, color_system=None)
        console.print(_render_rule_manager(console, self.load_rules(), 0, "").renderables[0])
        wide_lines = output.getvalue().splitlines()
        self.assertLessEqual(max(map(len, wide_lines)), 240)
        self.assertIn("N" * 60, output.getvalue())
        self.assertIn("P" * 80, output.getvalue())

        narrow = io.StringIO()
        console = Console(file=narrow, width=70, height=20, color_system=None)
        console.print(_render_rule_manager(console, self.load_rules(), 0, "").renderables[0])
        self.assertTrue(all(cell_len(line) <= 70 for line in narrow.getvalue().splitlines()))
        self.assertIn("red", narrow.getvalue())

    def test_manager_paging_keeps_selected_wrapped_rule_and_controls_visible(self):
        rules = _parse_rules(
            [
                {"pattern": f"rule{index}-" + "x" * 45, "name": "N" * 35, "color": "red"}
                for index in range(8)
            ],
            self.rules_path,
        )
        for selected in (0, 3, 7):
            with self.subTest(selected=selected):
                output = io.StringIO()
                console = Console(file=output, width=100, height=16, color_system=None)
                console.print(_render_rule_manager(console, rules, selected, ""))
                lines = output.getvalue().splitlines()
                self.assertLessEqual(len(lines), 16)
                self.assertTrue(any(">" in line and f"rule{selected}-" in line for line in lines))
                self.assertIn(f"{selected + 1}/8", lines[-1])

    def test_manager_long_wrapped_name_and_pattern_fit_short_terminal(self):
        rules = _parse_rules(
            [{"pattern": "P" * 80, "name": "N" * 60, "color": "red"}],
            self.rules_path,
        )
        output = io.StringIO()
        console = Console(file=output, width=70, height=16, color_system=None)
        console.print(_render_rule_manager(console, rules, 0, ""))
        lines = output.getvalue().splitlines()
        self.assertLessEqual(len(lines), 16)
        header = next(line for line in lines if "Pattern" in line)
        name_start, pattern_start, style_start = (
            header.index("Name"), header.index("Pattern"), header.index("Style")
        )
        row_lines = lines[lines.index(header) + 1:lines.index("")]
        name = "".join(line[name_start:pattern_start].strip() for line in row_lines)
        pattern = "".join(line[pattern_start:style_start].strip() for line in row_lines)
        self.assertEqual(name, "N" * 60)
        self.assertEqual(pattern, "P" * 80)
        self.assertIn("1/1", lines[-1])

    def test_listing_includes_disabled_rules_and_status(self):
        self.write_custom_rules()
        _show_rules(self.console, self.load_rules())
        output = self.output.getvalue()
        self.assertIn("Status", output)
        self.assertIn("disabled", output)
        self.assertIn("enabled", output)
        self.assertIn("literal", output)

    def test_manager_arrows_and_paging(self):
        self.write_custom_rules()
        contents = self.rules_path.read_text().replace(
            'pattern = "literal"', 'name = "Literal match"\npattern = "literal"'
        )
        self.rules_path.write_text(contents)
        rules = self.load_rules()
        self.run_manager(rules, ["up", "down", "down", "d", "cancel"])
        self.assertTrue(next(rule for rule in rules if rule.pattern == "user").disabled)
        self.assertIn("Disabled rule", self.output.getvalue())

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

    def test_manager_ignores_builtin_actions_when_no_custom_rules_exist(self):
        rules = self.load_rules()
        self.run_manager(rules, ["x", "m", "c", "cancel"])
        self.assertNotIn("built-in", self.output.getvalue())
        self.assertNotIn("cannot be deleted", self.output.getvalue())
        self.assertFalse(self.config_path.exists())
        self.assertEqual(rules, self.load_rules())

    def test_escape_cancels_without_writing_or_changing_live_rules(self):
        self.write_custom_rules()
        rules = self.load_rules()
        before = list(rules)
        contents = [path.read_bytes() for path in (self.rules_path, self.config_path)]
        self.run_manager(rules, ["x", "cancel", "c", "cancel", "cancel"])
        self.assertEqual(rules, before)
        self.assertEqual(contents, [
            path.read_bytes() for path in (self.rules_path, self.config_path)
        ])

    def test_key_decoder_arrows_enter_escape_and_eof(self):
        for sequence, expected in (
            (b"\x1b[A", "up"), (b"\x1b[B", "down"),
            (b"\x1bOA", "up"), (b"\x1bOB", "down"),
            (b"\x1b[C", "right"), (b"\x1b[D", "left"),
            (b"\x1bOC", "right"), (b"\x1bOD", "left"),
            ("é".encode(), "é"), (b"\x7f", "backspace"),
            (b"\r", "enter"), (b"\n", "enter"), (b"\x1b", "cancel"),
            (b"\x1b[", "cancel"), (b"", "cancel"), (b"x", "x"),
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
            self.assertEqual(manage.call_count, 1)
            self.assertIsNone(manage.call_args.kwargs["profile"])
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
        for command in ("Pause or resume", "Add a color rule", "rules manager", "Show this command list", "Quit ctail"):
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

    def test_pty_builtin_rules_stay_active_but_are_hidden_from_manager(self):
        self.config_path.write_text("show_unmatched = false\n")
        with self.running_app() as (master_fd, log_path):
            os.write(master_fd, b"r")
            output = self.read_until(master_fd, b"Rules manager")
            self.assertNotIn(DEFAULT_RULES[0][0].encode(), output)
            os.write(master_fd, b"d")
            os.write(master_fd, b"\x1b")
            self.assertFalse((self.config_dir / "default.rules.toml").exists())
            with log_path.open("a") as log:
                log.write("[ERROR] visible-error\n")
            self.read_until(master_fd, b"visible-error")
        self.assertFalse(self.load_rules()[0].disabled)

    def test_pty_arrow_deletion_and_escape_restore_terminal(self):
        self.write_custom_rules()
        before = self.rules_path.read_bytes()
        with self.running_app() as (master_fd, _):
            os.write(master_fd, b"r")
            self.read_until(master_fd, b"Rules manager")
            os.write(master_fd, b"x" + b"\x1b")
            self.read_until(master_fd, b"Canceled.")
            self.assertEqual(self.rules_path.read_bytes(), before)
            os.write(master_fd, b"\x1b[B\x1b[B\x1b[Axy")
            self.read_until(master_fd, b"Deleted rule")
            self.assertNotIn("other", [rule.pattern for rule in self.load_rules()])
            self.assertIn("literal", [rule.pattern for rule in self.load_rules()])
            after_deletion = self.rules_path.read_bytes()
            os.write(master_fd, b"x" + b"\x1b")
            self.read_until(master_fd, b"Canceled.")
            self.assertEqual(self.rules_path.read_bytes(), after_deletion)
            os.write(master_fd, b"xy")
            self.read_until(master_fd, b"Deleted rule")
            self.assertNotIn("user", [rule.pattern for rule in self.load_rules()])
            os.write(master_fd, b"\x1b")
            self.read_until(master_fd, b"\x1b[?1049l")

    def test_pty_rename_rule_persists_and_preserves_live_matching(self):
        self.write_custom_rules()
        with self.running_app() as (master_fd, log_path):
            os.write(master_fd, b"r")
            self.read_until(master_fd, b"Rules manager")
            os.write(master_fd, b"\x1b[Br")
            self.read_until(master_fd, b"Rename rule")
            os.write(master_fd, b"Other rule\r")
            self.read_until(master_fd, b"Renamed rule.")
            self.assertEqual(self.load_rules()[1].name, "Other rule")
            os.write(master_fd, b"\x1b")
            self.read_until(master_fd, b"\x1b[?1049l")
            with log_path.open("a") as log:
                log.write("other still-matches-after-rename\n")
            self.read_until(master_fd, b"still-matches-after-rename")

    def test_pty_copy_rule_to_other_profiles(self):
        self.rules_path.write_text(
            'active_profile = "one"\n[[rules]]\npattern="x"\ncolor="red"\n'
            'regex=false\nshared=true\n[profiles.one]\n[profiles.two]\n'
        )
        with self.running_app() as (master_fd, _):
            os.write(master_fd, b"r")
            self.read_until(master_fd, b"Rules manager")
            os.write(master_fd, b"c")
            self.read_until(master_fd, b"Copy rule to")
            os.write(master_fd, b"\r")
            self.read_until(master_fd, b"Copy rule to one?")
            os.write(master_fd, b"y")
            self.read_until(master_fd, b"Copied rule.")
            data = tomllib.loads(self.rules_path.read_text())
            self.assertEqual(data["profiles"]["one"]["rules"][0]["pattern"], "x")
            self.assertEqual(data["rules"][0]["pattern"], "x")
            os.write(master_fd, b"c")
            self.read_until(master_fd, b"Copy rule to")
            os.write(master_fd, b"\r")
            self.read_until(master_fd, b"Copy rule to two?")
            os.write(master_fd, b"y")
            self.read_until(master_fd, b"Copied rule.")
            data = tomllib.loads(self.rules_path.read_text())
            self.assertEqual(data["profiles"]["one"]["rules"][0]["pattern"], "x")
            self.assertEqual(data["profiles"]["two"]["rules"][0]["pattern"], "x")
            self.assertEqual(data["rules"][0]["pattern"], "x")
            os.write(master_fd, b"\x1b")
            self.read_until(master_fd, b"\x1b[?1049l")

    def test_pty_rule_editor_all_entry_points_and_live_output(self):
        self.config_path.write_text('show_unmatched = false\nactive_profile = "one"\n[profiles.one]\n')
        with self.running_app() as (master_fd, log_path):
            os.write(master_fd, b"c")
            self.read_until(master_fd, b"Add rule")
            os.write(
                master_fd,
                "café".encode() + b"\x1b[Cb\x1b[Ci\r",
            )
            self.read_until(master_fd, b"Added rule.")
            data = tomllib.loads(self.rules_path.read_text())
            self.assertEqual(data["profiles"]["one"]["rules"][0]["pattern"], "café")
            style = _editor_style(data["profiles"]["one"]["rules"][0]["color"])
            self.assertIsNotNone(style)
            self.assertTrue(style.bold)
            self.assertTrue(style.reverse)
            with log_path.open("a") as log:
                log.write("café from c\n")
            output = self.read_until(master_fd, b" from c")
            self.assertIn("café".encode(), output)

            os.write(master_fd, b"r")
            self.read_until(master_fd, b"Rules manager")
            os.write(master_fd, b"a")
            self.read_until(master_fd, b"Add rule")
            os.write(master_fd, b"\rmanager\r\x1b[C\r")
            self.read_until(master_fd, b"Added rule.")
            data = tomllib.loads(self.rules_path.read_text())
            self.assertEqual(data["profiles"]["one"]["rules"][0]["pattern"], "manager")

            os.write(master_fd, b"e")
            self.read_until(master_fd, b"Edit rule")
            os.write(master_fd, b"\r-edit\r\x1b[C\r")
            self.read_until(master_fd, b"Edited rule.")
            self.assertEqual(tomllib.loads(self.rules_path.read_text())["profiles"]["one"]["rules"][0]["pattern"],
                             "manager-edit")
            os.write(master_fd, b"\x1b")
            self.read_until(master_fd, b"\x1b[?1049l")
            with log_path.open("a") as log:
                log.write("manager-edit from editor\n")
            output = self.read_until(master_fd, b" from editor")
            self.assertIn(b"manager-edit", output)

    def test_pty_cancel_editor_preserves_rules_and_terminal(self):
        self.config_path.write_text("")
        self.rules_path.write_text('[[rules]]\npattern="x"\ncolor="red"\n')
        before = self.rules_path.read_bytes()
        with self.running_app() as (master_fd, _):
            os.write(master_fd, b"c")
            self.read_until(master_fd, b"Add rule")
            os.write(master_fd, b"\x1b")
            self.read_until(master_fd, b"Canceled.")
            self.assertEqual(self.rules_path.read_bytes(), before)

    def test_pty_add_regex_from_tail_and_manager(self):
        self.config_path.write_text("show_unmatched = false\n")
        with self.running_app() as (master_fd, log_path):
            os.write(master_fd, b"c")
            self.read_until(master_fd, b"Add rule")
            os.write(master_fd, b"tail[0-9]+\x1b[C\r")
            self.read_until(master_fd, b"Added rule.")

            os.write(master_fd, b"r")
            self.read_until(master_fd, b"Rules manager")
            os.write(master_fd, b"a")
            self.read_until(master_fd, b"Add rule")
            os.write(master_fd, b"manager[0-9]+\x1b[C\r")
            self.read_until(master_fd, b"Added rule.")
            entries = tomllib.loads((self.config_dir / "default.rules.toml").read_text())["rules"]
            self.assertEqual(
                [(entry["pattern"], entry["regex"]) for entry in entries[:2]],
                [("manager[0-9]+", True), ("tail[0-9]+", True)],
            )

            os.write(master_fd, b"\x1b")
            self.read_until(master_fd, b"\x1b[?1049l")
            with log_path.open("a") as log:
                log.write("tail123 regex from c\nmanager456 regex from a\n")
            output = self.read_until(master_fd, b"regex from a")
            self.assertIn(b"tail123", output)
            self.assertIn(b"regex from c", output)
            self.assertIn(b"manager456", output)

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
