import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

import ctail.cli as cli
from ctail.cli import (
    DEFAULT_RULES,
    _change_rule,
    _edit_rule,
    _initialize_config,
    _load_settings,
    _move_ad_hoc_rule,
    _parse_profile_paths,
    _parse_rule_profiles,
    _read_toml,
    _write_ad_hoc_rule,
    _write_profile,
)


class SplitRuleTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.config = self.root / "ctail.toml"
        self.config.write_text('show_unmatched = true\n')
        (self.root / "default.rules.toml").write_text(
            '[[rules]]\npattern = "shared"\ncolor = "red"\nshared = true\n'
            '[[rules]]\npattern = "default"\ncolor = "blue"\n'
        )
        (self.root / "logs.rules.toml").write_text(
            '[[rules]]\npattern = "profile"\ncolor = "green"\n'
        )

    def load(self, profile=None):
        return _load_settings(self.root, profile)[1]

    def test_scopes_and_path_mappings(self):
        self.config.write_text(
            'show_unmatched = true\n[[profile_paths]]\n'
            'profile = "logs"\npaths = ["/tmp/*.log"]\n'
        )
        config = _read_toml(self.config)
        profiles = _parse_rule_profiles(config, self.config)
        self.assertEqual(_parse_profile_paths(config, self.config, profiles)[0].profile, "logs")
        self.assertEqual([r.pattern for r in self.load()[:2]], ["shared", "default"])
        self.assertEqual([r.pattern for r in self.load("logs")[:2]], ["shared", "profile"])

    def test_edit_toggle_create_and_move(self):
        _write_ad_hoc_rule(self.config, "added", "yellow", "logs")
        rule = next(r for r in self.load("logs") if r.pattern == "added")
        _edit_rule(rule, self.config, "edited", "blue", profile="logs")
        rule = next(r for r in self.load("logs") if r.pattern == "edited")
        _change_rule(rule, self.config, delete=False, profile="logs")
        rule = next(r for r in self.load("logs") if r.pattern == "edited")
        self.assertTrue(rule.disabled)
        self.assertTrue(_move_ad_hoc_rule(rule, self.config, None, profile="logs"))
        self.assertTrue(next(r for r in self.load() if r.pattern == "edited").shared)
        self.assertNotIn("edited", (self.root / "logs.rules.toml").read_text())
        self.assertEqual(tomllib.loads(self.config.read_text()), {"show_unmatched": True})

    def test_builtin_overrides_are_scoped(self):
        builtin = next(r for r in self.load("logs") if r.pattern == DEFAULT_RULES[0][0])
        _change_rule(builtin, self.config, delete=False, profile="logs")
        self.assertTrue(next(r for r in self.load("logs") if r.pattern == builtin.pattern).disabled)
        self.assertFalse(next(r for r in self.load() if r.pattern == builtin.pattern).disabled)

    def test_create_profile_and_validate_name(self):
        _write_profile(self.config, "new")
        self.assertTrue((self.root / "new.rules.toml").is_file())
        self.assertEqual(self.load("new")[0].pattern, "shared")
        for name in ("default", "../other", "a/b", "a\\b", ""):
            with self.subTest(name=name), self.assertRaises(ValueError):
                _write_profile(self.config, name)

    def test_mixed_legacy_and_file_scopes_write_to_existing_scope(self):
        (self.root / "default.rules.toml").unlink()
        self.config.write_text('[[rules]]\npattern = "old"\ncolor = "red"\n')
        _write_ad_hoc_rule(self.config, "shared", "yellow")
        _write_ad_hoc_rule(self.config, "scoped", "blue", "logs")
        self.assertEqual(self.load()[0].pattern, "shared")
        self.assertEqual([r.pattern for r in self.load("logs")[:2]], ["shared", "scoped"])
        self.assertFalse((self.root / "default.rules.toml").exists())

    def test_bad_rule_file_and_collisions_reported(self):
        (self.root / "logs.rules.toml").write_text('show_unmatched = true\n')
        with self.assertRaisesRegex(ValueError, "only contain"):
            self.load("logs")
        (self.root / "logs.rules.toml").write_text("")
        self.config.write_text("[profiles.logs]\n")
        with self.assertRaisesRegex(ValueError, "also defined"):
            self.load("logs")

    def test_first_run_and_existing_config(self):
        empty = self.root / "new"
        _initialize_config(empty)
        self.assertTrue((empty / "default.rules.toml").is_file())
        self.assertTrue((empty / "http-access.rules.toml").is_file())
        self.assertIn("HTTP request method", [r.name for r in _load_settings(empty, "http-access")[1]])
        existing = self.root / "existing"
        existing.mkdir()
        (existing / "ctail.toml").write_text("show_unmatched = true\n")
        _initialize_config(existing)
        self.assertFalse((existing / "default.rules.toml").exists())

    def test_failed_rule_save_preserves_original(self):
        path = self.root / "logs.rules.toml"
        before = path.read_bytes()
        with patch("ctail.cli.os.replace", side_effect=OSError("write denied")):
            with self.assertRaisesRegex(OSError, "write denied"):
                _write_ad_hoc_rule(self.config, "added", "yellow", "logs")
        self.assertEqual(path.read_bytes(), before)

    def test_move_rolls_back_destination_if_source_save_fails(self):
        source = self.root / "logs.rules.toml"
        target = self.root / "default.rules.toml"
        original_source, original_target = source.read_bytes(), target.read_bytes()
        rule = next(r for r in self.load("logs") if r.pattern == "profile")
        save = cli._save_rule_document

        def fail_source(path, document):
            if path == source:
                raise OSError("source write denied")
            save(path, document)

        with patch("ctail.cli._save_rule_document", side_effect=fail_source):
            with self.assertRaisesRegex(OSError, "source write denied"):
                _move_ad_hoc_rule(rule, self.config, None, profile="logs")
        self.assertEqual(source.read_bytes(), original_source)
        self.assertEqual(target.read_bytes(), original_target)

if __name__ == "__main__":
    unittest.main()
