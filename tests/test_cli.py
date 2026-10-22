import io
import re
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from rich.color import ANSI_COLOR_NAMES
from rich.console import Console

from ctail import DEFAULT_RULES, _load_settings, _render_line, main


class ListingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config_dir = Path(self.directory.name)

    def invoke(self, *arguments, terminal=True):
        output = io.StringIO()
        with (
            redirect_stdout(output),
            patch.object(output, "isatty", return_value=terminal),
            patch("sys.argv", ["ctail", *arguments]),
            patch("ctail._config_directory", return_value=self.config_dir),
            patch(
                "ctail.Console",
                side_effect=lambda **kwargs: Console(
                    file=output, force_terminal=True, width=240, **kwargs
                ),
            ),
            patch("ctail._tail_process") as tail,
            patch("ctail._consume_stream") as consume,
        ):
            main()
        tail.assert_not_called()
        consume.assert_not_called()
        return output.getvalue()

    def write_rules(self):
        (self.config_dir / "rules.toml").write_text(
            '[[rules]]\npattern = "[literal]"\n'
            'color = "bold white on red"\nregex = false\n'
        )
        (self.config_dir / "ctail.toml").write_text(
            '[[rules]]\npattern = "user.*pattern"\ncolor = "orange3"\n'
        )

    def test_color_aliases_list_every_name_with_color(self):
        for option in ("-lsc", "--list-colors"):
            with self.subTest(option=option):
                output = self.invoke(option)
                self.assertIn("\x1b[", output)
                plain = re.sub(r"\x1b\[[0-9;]*m", "", output)
                self.assertTrue(set(ANSI_COLOR_NAMES) <= set(plain.splitlines()))
                self.assertIn("default", plain.splitlines())

    def test_help_lists_all_interactive_keys(self):
        with (
            redirect_stdout(io.StringIO()) as output,
            patch("sys.argv", ["ctail", "--help"]),
        ):
            with self.assertRaises(SystemExit) as caught:
                main()
        self.assertEqual(caught.exception.code, 0)
        for key in ("Space", "c", "r", "d", "?", "q"):
            self.assertIn(key, output.getvalue())

    def test_rules_aliases_show_sources_modes_and_priority(self):
        self.write_rules()
        for option in ("-shr", "--show-rules"):
            with self.subTest(option=option):
                output = self.invoke(option, "--no-color")
                self.assertNotIn("\x1b[", output)
                for value in (
                    "rules.toml", "ctail.toml", "built-in", "literal", "regex",
                    "bold white on red", "orange3",
                ):
                    self.assertIn(value, output)
                patterns = ["[literal]", "user.*pattern", *(
                    pattern for pattern, _ in DEFAULT_RULES
                )]
                positions = [output.index(pattern) for pattern in patterns]
                self.assertEqual(positions, sorted(positions))

    def test_combined_listings_and_background_preview(self):
        self.write_rules()
        output = self.invoke("-lsc", "-shr")
        self.assertLess(output.index("Named colors"), output.index("Coloring rules"))
        self.assertIn("\x1b[1;37;41m", output)

    def test_color_listing_ignores_invalid_configuration(self):
        (self.config_dir / "ctail.toml").write_text("invalid TOML")
        self.assertIn("bright_red", self.invoke("--list-colors"))

    def test_rule_listing_reports_invalid_configuration(self):
        (self.config_dir / "ctail.toml").write_text("invalid TOML")
        with patch("sys.stderr", new_callable=io.StringIO) as errors:
            with self.assertRaises(SystemExit) as caught:
                self.invoke("--show-rules")
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("invalid TOML", errors.getvalue())

    def test_rule_listing_reports_invalid_configuration_types(self):
        (self.config_dir / "ctail.toml").write_text("show_unmatched = 'yes'")
        with patch("sys.stderr", new_callable=io.StringIO) as errors:
            with self.assertRaises(SystemExit) as caught:
                self.invoke("--show-rules")
        self.assertEqual(caught.exception.code, 2)
        self.assertIn("'show_unmatched' must be true or false", errors.getvalue())

    def test_redirected_listings_are_plain(self):
        output = self.invoke("-lsc", "-shr", terminal=False)
        self.assertNotIn("\x1b[", output)

    def test_default_rules_without_configuration(self):
        output = self.invoke("--show-rules", "--no-color")
        for pattern, color in DEFAULT_RULES:
            self.assertIn(pattern, output)
            self.assertIn(color, output)

    def test_literal_and_background_rendering_preserved(self):
        self.write_rules()
        show_unmatched, rules, _ = _load_settings(self.config_dir)
        rendered = _render_line("before [literal] after", rules, show_unmatched)
        self.assertIsNotNone(rendered)
        self.assertEqual(rendered.plain, "before [literal] after")
        self.assertEqual(rendered.spans[0].style, "bold white on red")
        self.assertIsNone(_render_line("literal", rules, show_unmatched))


if __name__ == "__main__":
    unittest.main()
