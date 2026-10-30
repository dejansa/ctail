import io
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from rich.console import Console

from ctail.cli import (
    DEFAULT_RULES,
    _change_rule,
    _edit_rule,
    _initialize_config,
    _load_settings,
    _manage_rules,
    _move_ad_hoc_rule,
    _parse_rules,
    _render_line,
    _write_ad_hoc_rule,
)


class UnifiedRuleTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.path = self.root / "ctail.toml"
        self.path.write_text(
            '# retained\nshow_unmatched = true\n'
            '[[rules]]\npattern = "shared"\ncolor = "red"\nshared = true\n'
            '[[rules]]\npattern = "default"\ncolor = "blue"\n'
            '[profiles.logs]\n'
            '[[profiles.logs.rules]]\npattern = "profile"\ncolor = "green"\n'
            '[profiles.other]\n'
        )

    def load(self, profile=None):
        return _load_settings(self.root, profile)[1]

    def test_scopes_priority_and_legacy_file_ignored(self):
        (self.root / "rules.toml").write_text("not valid toml")
        show, rules, path = _load_settings(self.root)
        self.assertTrue(show)
        self.assertEqual(path, self.path)
        self.assertEqual([r.pattern for r in rules[:2]], ["shared", "default"])
        self.assertEqual(
            [r.pattern for r in self.load("logs")[:2]], ["shared", "profile"]
        )
        self.assertEqual(self.load("other")[0].pattern, "shared")

    def test_shared_precedence_and_builtin_override(self):
        self.path.write_text(
            '[[rules]]\npattern = "event"\ncolor = "red"\nshared = true\n'
            '[profiles.logs]\n'
            '[[profiles.logs.rules]]\npattern = "event"\ncolor = "blue"\n'
            '[[profiles.logs.rules]]\nbuiltin = true\n'
            f'pattern = {self._toml_literal(DEFAULT_RULES[0][0])}\ndisabled = true\n'
        )
        rules = self.load("logs")
        rendered = _render_line("event", rules, False)
        self.assertIsNotNone(rendered)
        self.assertEqual(rendered.spans[0].style, "red")
        self.assertTrue(next(r for r in rules if r.source == "built-in").disabled)

    @staticmethod
    def _toml_literal(value):
        return "'" + value + "'"

    def test_interactive_writes_edits_and_disables_in_one_file(self):
        _write_ad_hoc_rule(self.path, "new", "yellow")
        self.assertTrue(self.load()[0].shared)
        self.assertEqual(self.load("logs")[0].pattern, "new")
        _write_ad_hoc_rule(self.path, "scoped", "blue", "logs")
        scoped = next(r for r in self.load("logs") if r.pattern == "scoped")
        _edit_rule(scoped, self.path, "updated", "magenta", profile="logs")
        updated = next(r for r in self.load("logs") if r.pattern == "updated")
        _change_rule(updated, self.path, delete=False, profile="logs")
        self.assertTrue(next(r for r in self.load("logs") if r.pattern == "updated").disabled)
        self.assertNotIn("updated", [r.pattern for r in self.load("other")])
        self.assertIn("# retained", self.path.read_text())
        self.assertFalse((self.root / "rules.toml").exists())

    def test_move_default_to_shared_and_shared_to_profile(self):
        default = next(r for r in self.load() if r.pattern == "default")
        self.assertTrue(_move_ad_hoc_rule(default, self.path, None))
        shared = next(r for r in self.load("logs") if r.pattern == "default")
        self.assertTrue(shared.shared)
        self.assertTrue(_move_ad_hoc_rule(shared, self.path, "other", profile="logs"))
        self.assertNotIn("default", [r.pattern for r in self.load("logs")])
        moved = next(r for r in self.load("other") if r.pattern == "default")
        self.assertFalse(moved.shared)
        self.assertEqual(moved.profile_name, "other")
        self.assertFalse(_move_ad_hoc_rule(moved, self.path, "other", profile="other"))

    def test_manager_moves_default_rule_to_shared(self):
        rules = self.load()
        with patch("ctail.cli._read_rule_key", side_effect=["down", "m", "enter", "y", "cancel"]):
            _manage_rules(Console(file=io.StringIO()), rules, self.path, 0)
        self.assertTrue(next(r for r in self.load("logs") if r.pattern == "default").shared)

    def test_shared_validation(self):
        for value in (1, "true", None):
            with self.subTest(value=value), self.assertRaisesRegex(TypeError, "'shared'"):
                _parse_rules([{"pattern": "x", "color": "red", "shared": value}], self.path)
        with self.assertRaisesRegex(ValueError, "only top-level"):
            _parse_rules(
                [{"pattern": "x", "color": "red", "shared": True}],
                self.path,
                profile_name="logs",
            )
        with self.assertRaisesRegex(ValueError, "only top-level"):
            _parse_rules(
                [{"pattern": DEFAULT_RULES[0][0], "builtin": True, "shared": True}],
                self.path,
                allow_builtins=True,
            )

    def test_failed_save_preserves_configuration(self):
        before = self.path.read_bytes()
        with patch("ctail.cli.os.replace", side_effect=OSError("write denied")):
            with self.assertRaisesRegex(OSError, "write denied"):
                _write_ad_hoc_rule(self.path, "new", "red")
        self.assertEqual(self.path.read_bytes(), before)

    def test_first_run_only_creates_main_config(self):
        empty = self.root / "new"
        _initialize_config(empty)
        self.assertTrue((empty / "ctail.toml").is_file())
        self.assertFalse((empty / "rules.toml").exists())
        with (empty / "ctail.toml").open("rb") as file:
            tomllib.load(file)


if __name__ == "__main__":
    unittest.main()
