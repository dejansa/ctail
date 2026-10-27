import io
import re
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from rich.color import ANSI_COLOR_NAMES
from rich.console import Console
from rich.style import Style

from ctail.cli import (
    DEFAULT_RULES,
    _consume_stream,
    _load_settings,
    _manage_profiles,
    _parse_rules,
    _read_active_profile,
    _read_rule_key,
    _render_line,
    _select_profile,
    _write_active_profile,
    _write_profile,
    main,
)


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
            patch("ctail.cli._config_directory", return_value=self.config_dir),
            patch(
                "ctail.cli.Console",
                side_effect=lambda **kwargs: Console(
                    file=output, force_terminal=True, width=240, **kwargs
                ),
            ),
            patch("ctail.cli._tail_process") as tail,
            patch("ctail.cli._consume_stream") as consume,
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
        for key in ("Enter", "Space", "p", "n", "c", "f", "r", "d", "?", "q"):
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

    def test_profile_rules_replace_main_rules_and_keep_global_rules(self):
        (self.config_dir / "ctail.toml").write_text(
            '[[rules]]\npattern = "default-only"\ncolor = "red"\n\n'
            '[profiles.nginx]\n'
            '[[profiles.nginx.rules]]\n'
            'name = "Nginx request method"\n'
            'pattern = "nginx-only"\n'
            'color = "green"\n'
        )
        (self.config_dir / "rules.toml").write_text(
            '[[rules]]\npattern = "shared-literal"\ncolor = "blue"\nregex = false\n\n'
            '[profiles.nginx]\n'
            '[[profiles.nginx.rules]]\n'
            'name = "Nginx request identifier"\n'
            'pattern = "nginx-request-id"\n'
            'color = "yellow"\n'
        )

        _, default_rules, _ = _load_settings(self.config_dir)
        _, profile_rules, _ = _load_settings(self.config_dir, "nginx")

        self.assertIn("default-only", [rule.pattern for rule in default_rules])
        self.assertNotIn("nginx-only", [rule.pattern for rule in default_rules])
        self.assertNotIn("default-only", [rule.pattern for rule in profile_rules])
        self.assertEqual(profile_rules[0].pattern, "shared-literal")
        profile_rule = next(
            rule for rule in profile_rules if rule.pattern == "nginx-request-id"
        )
        self.assertEqual(profile_rule.profile_name, "nginx")
        self.assertIn("nginx-only", [rule.pattern for rule in profile_rules])

    def test_unknown_profile_lists_available_profiles(self):
        (self.config_dir / "ctail.toml").write_text(
            "[profiles.nginx]\n"
        )

        with self.assertRaisesRegex(
            ValueError, "unknown profile 'missing'; available profiles: nginx"
        ):
            _load_settings(self.config_dir, "missing")

    def test_profile_option_shows_only_selected_profile_rules(self):
        (self.config_dir / "ctail.toml").write_text(
            '[[rules]]\npattern = "default-only"\ncolor = "red"\n\n'
            "[profiles.nginx]\n"
            '[[profiles.nginx.rules]]\n'
            'name = "Nginx access"\n'
            'pattern = "nginx-only"\n'
            'color = "green"\n'
        )
        (self.config_dir / "rules.toml").write_text(
            '[[rules]]\npattern = "shared-only"\ncolor = "blue"\nregex = false\n\n'
            "[profiles.nginx]\n"
            '[[profiles.nginx.rules]]\n'
            'name = "Nginx temporary rule"\n'
            'pattern = "nginx-temporary-only"\n'
            'color = "yellow"\nregex = false\n'
        )

        output = self.invoke("-p", "nginx", "-shr", "--no-color")

        self.assertIn("Coloring rules for profile 'nginx'", output)
        self.assertIn("profile: nginx", output)
        self.assertIn("nginx-only", output)
        self.assertIn("nginx-temporary-only", output)
        self.assertNotIn("default-only", output)
        self.assertNotIn("shared-only", output)
        self.assertNotIn(DEFAULT_RULES[0][0], output)

    def test_saved_active_profile_is_used_when_no_profile_is_specified(self):
        (self.config_dir / "ctail.toml").write_text(
            'active_profile = "nginx"\n'
            "[profiles.nginx]\n"
            "[[profiles.nginx.rules]]\n"
            'pattern = "nginx-only"\n'
            'color = "green"\n'
        )

        output = self.invoke("--show-rules", "--no-color")

        self.assertIn("Coloring rules for profile 'nginx'", output)
        self.assertIn("nginx-only", output)

    def test_profile_argument_is_persisted_for_normal_run(self):
        config_path = self.config_dir / "ctail.toml"
        config_path.write_text("[profiles.nginx]\n")
        output = io.StringIO()

        class InputStream:
            def __init__(self):
                self.buffer = self

            @staticmethod
            def fileno():
                return 12

            @staticmethod
            def isatty():
                return False

        input_stream = InputStream()
        with (
            redirect_stdout(output),
            patch.object(output, "isatty", return_value=False),
            patch("sys.stdin", input_stream),
            patch("sys.argv", ["ctail", "-p", "nginx"]),
            patch("ctail.cli._config_directory", return_value=self.config_dir),
            patch(
                "ctail.cli.Console",
                side_effect=lambda **kwargs: Console(
                    file=output, force_terminal=True, width=240, **kwargs
                ),
            ),
            patch("ctail.cli._consume_stream") as consume,
        ):
            main()

        consume.assert_called_once()
        self.assertEqual(_read_active_profile(config_path), "nginx")

    def test_writing_default_active_profile_clears_saved_selection(self):
        config_path = self.config_dir / "ctail.toml"
        config_path.write_text(
            'active_profile = "nginx"\n[profiles.nginx]\n'
        )

        _write_active_profile(config_path, None)

        self.assertIsNone(_read_active_profile(config_path))
        self.assertNotIn("active_profile", config_path.read_text())

    def test_list_profiles_alias_and_long_option(self):
        (self.config_dir / "ctail.toml").write_text(
            "[profiles.http-access]\n"
            "[profiles.database]\n"
        )

        for option in ("-lsp", "--list-profiles"):
            with self.subTest(option=option):
                output = self.invoke(option, "--no-color")
                self.assertIn("Configured profiles", output)
                self.assertIn("database", output)
                self.assertIn("http-access", output)

    def test_list_profiles_reports_when_none_are_configured(self):
        (self.config_dir / "ctail.toml").write_text("show_unmatched = true\n")

        output = self.invoke("--list-profiles", "--no-color")

        self.assertIn("No named profiles configured.", output)

    def test_write_profile_preserves_existing_configuration(self):
        config_path = self.config_dir / "ctail.toml"
        original = "# Keep this comment.\nshow_unmatched = true\n\n[profiles.existing]\n"
        config_path.write_text(original)

        _write_profile(config_path, "new profile")

        updated = config_path.read_text()
        self.assertIn("# Keep this comment.", updated)
        self.assertIn("[profiles.existing]", updated)
        self.assertIn('[profiles."new profile"]', updated)
        _, rules, _ = _load_settings(self.config_dir, "new profile")
        self.assertEqual([rule.pattern for rule in rules], [
            pattern for pattern, _ in DEFAULT_RULES
        ])

    def test_write_profile_rejects_duplicate_name_without_changing_file(self):
        config_path = self.config_dir / "ctail.toml"
        original = "[profiles.existing]\n"
        config_path.write_text(original)

        with self.assertRaisesRegex(ValueError, "already exists"):
            _write_profile(config_path, "existing")

        self.assertEqual(config_path.read_text(), original)

    def test_profile_selector_starts_at_default_and_n_adds_profile(self):
        config_path = self.config_dir / "ctail.toml"
        config_path.write_text("[profiles.alpha]\n[profiles.beta]\n")
        console = Console(file=io.StringIO(), no_color=True)

        with (
            patch("ctail.cli.Live"),
            patch("ctail.cli._read_rule_key", return_value="enter"),
        ):
            self.assertEqual(
                _select_profile(console, config_path, 11),
                (False, None),
            )
        with (
            patch("ctail.cli.Live"),
            patch("ctail.cli._read_rule_key", side_effect=["down", "down", "enter"]),
        ):
            self.assertEqual(
                _select_profile(console, config_path, 11),
                (False, "beta"),
            )
        with (
            patch("ctail.cli.Live"),
            patch("ctail.cli._read_rule_key", return_value="n"),
        ):
            self.assertEqual(_select_profile(console, config_path, 11), (True, None))
        with patch("ctail.cli.os.read", return_value=b"n"):
            self.assertEqual(_read_rule_key(11), "n")

    def test_profile_selector_marks_the_active_profile(self):
        config_path = self.config_dir / "ctail.toml"
        config_path.write_text("[profiles.alpha]\n")
        output = io.StringIO()
        console = Console(file=output, no_color=True, width=80)

        with (
            patch("ctail.cli.Live") as live,
            patch("ctail.cli._read_rule_key", return_value="enter"),
        ):
            _select_profile(console, config_path, 11, active_profile="alpha")

        console.print(live.call_args.args[0])
        self.assertIn("alpha (active)", output.getvalue())

    def test_escape_from_profile_selector_is_silent(self):
        rules_path = self.config_dir / "rules.toml"
        rules_path.write_text("")
        output = io.StringIO()
        console = Console(file=output, no_color=True)
        terminal_state = [0, 0, 0, 0, 0, 0, [0] * 8]

        with patch("ctail.cli._select_profile", return_value=None):
            selected = _manage_profiles(
                console, [], rules_path, 11, terminal_state, "nginx"
            )

        self.assertEqual(selected, "nginx")
        self.assertNotIn("Canceled", output.getvalue())

    def test_add_profile_key_saves_and_activates_new_profile(self):
        rules_path = self.config_dir / "rules.toml"
        rules_path.write_text("")
        console = Console(file=io.StringIO(), no_color=True)
        terminal_state = [0, 0, 0, 0, 0, 0, [0] * 8]
        rules = []

        with (
            patch("ctail.cli.Live"),
            patch("ctail.cli._read_rule_key", return_value="n"),
            patch("builtins.input", return_value="new-profile"),
            patch("ctail.cli.termios.tcsetattr"),
            patch("ctail.cli.termios.tcgetattr", return_value=terminal_state),
        ):
            selected = _manage_profiles(
                console, rules, rules_path, 11, terminal_state, None
            )

        self.assertEqual(selected, "new-profile")
        config_text = (self.config_dir / "ctail.toml").read_text()
        self.assertIn("[profiles.new-profile]", config_text)
        self.assertIn('active_profile = "new-profile"', config_text)
        self.assertEqual([rule.pattern for rule in rules], [
            pattern for pattern, _ in DEFAULT_RULES
        ])

    def test_selecting_profile_persists_it_and_default_clears_it(self):
        config_path = self.config_dir / "ctail.toml"
        config_path.write_text("[profiles.nginx]\n")
        rules_path = self.config_dir / "rules.toml"
        rules_path.write_text("")
        console = Console(file=io.StringIO(), no_color=True)
        terminal_state = [0, 0, 0, 0, 0, 0, [0] * 8]
        rules = []

        with patch("ctail.cli._select_profile", return_value=(False, "nginx")):
            selected = _manage_profiles(
                console, rules, rules_path, 11, terminal_state, None
            )
        self.assertEqual(selected, "nginx")
        self.assertEqual(_read_active_profile(config_path), "nginx")

        with patch("ctail.cli._select_profile", return_value=(False, None)):
            selected = _manage_profiles(
                console, rules, rules_path, 11, terminal_state, "nginx"
            )
        self.assertIsNone(selected)
        self.assertIsNone(_read_active_profile(config_path))

    def test_first_run_creates_example_configuration(self):
        self.config_dir = Path(self.directory.name) / ".config" / "ctail"

        output = self.invoke("--show-rules", "--no-color")

        main_config_path = self.config_dir / "ctail.toml"
        rules_config_path = self.config_dir / "rules.toml"
        self.assertTrue(main_config_path.is_file())
        self.assertTrue(rules_config_path.is_file())
        self.assertIn("show_unmatched = true", main_config_path.read_text())
        self.assertIn("[profiles.http-access]", main_config_path.read_text())
        self.assertIn("Syslog timestamp", output)
        self.assertIn("Log analysis GET requests", rules_config_path.read_text())

    def test_first_run_preserves_existing_configuration(self):
        original = 'show_unmatched = false\n\n[[rules]]\npattern = "custom"\ncolor = "red"\n'
        original_rules = '[[rules]]\npattern = "custom literal"\ncolor = "blue"\nregex = false\n'
        (self.config_dir / "ctail.toml").write_text(original)
        (self.config_dir / "rules.toml").write_text(original_rules)

        self.invoke("--show-rules", "--no-color")

        self.assertEqual((self.config_dir / "ctail.toml").read_text(), original)
        self.assertEqual((self.config_dir / "rules.toml").read_text(), original_rules)

    def test_first_run_adds_missing_file_without_replacing_existing_one(self):
        original = 'show_unmatched = false\n\n[[rules]]\npattern = "custom"\ncolor = "red"\n'
        (self.config_dir / "ctail.toml").write_text(original)

        self.invoke("--show-rules", "--no-color")

        self.assertEqual((self.config_dir / "ctail.toml").read_text(), original)
        self.assertIn(
            "Log analysis GET requests",
            (self.config_dir / "rules.toml").read_text(),
        )

    def test_literal_and_background_rendering_preserved(self):
        self.write_rules()
        show_unmatched, rules, _ = _load_settings(self.config_dir)
        rendered = _render_line("before [literal] after", rules, show_unmatched)
        self.assertIsNotNone(rendered)
        self.assertEqual(rendered.plain, "before [literal] after")
        self.assertEqual(rendered.spans[0].style, "bold white on red")
        self.assertIsNone(_render_line("literal", rules, show_unmatched))

    def test_capture_group_colors_and_optional_match_remainder(self):
        line = "before status=404 route=/x"
        pattern = r"(status=)(\d{3}).*"
        source = self.config_dir / "ctail.toml"

        for colors, expected in (
            (
                "bright_cyan, bold bright_red, bright_black",
                {
                    8: Style.parse("bright_cyan"),
                    14: Style.parse("bold bright_red"),
                    20: Style.parse("bright_black"),
                },
            ),
            (
                "bright_cyan, bold bright_red",
                {
                    8: Style.parse("bright_cyan"),
                    14: Style.parse("bold bright_red"),
                    20: Style.null(),
                },
            ),
            (
                "bright_cyan",
                {
                    8: Style.parse("bright_cyan"),
                    14: Style.null(),
                    20: Style.null(),
                },
            ),
        ):
            with self.subTest(colors=colors):
                rules = _parse_rules(
                    [{"pattern": pattern, "color": colors}], source
                )
                rendered = _render_line(line, rules, False)
                self.assertIsNotNone(rendered)
                assert rendered is not None
                self.assertEqual(rendered.plain, line)
                for offset, style in expected.items():
                    self.assertEqual(rendered.get_style_at_offset(offset), style)

    def test_capture_group_color_parser_preserves_rgb_commas(self):
        rules = _parse_rules(
            [{"pattern": "(value)(\\d+)", "color": "rgb(1,2,3), red"}],
            self.config_dir / "ctail.toml",
        )
        self.assertEqual(rules[0].group_colors, ("rgb(1,2,3)", "red"))

    def test_f_toggles_unmatched_line_visibility_for_current_session(self):
        output = io.StringIO()
        console = Console(file=output, no_color=True)
        ready_fds = iter(([11], [10], [11], [10], [10]))
        reads = {
            11: [b"f", b"f"],
            10: [b"visible unmatched\n", b"hidden unmatched\n", b""],
        }

        with (
            patch(
                "ctail.cli.select.select",
                side_effect=lambda *_args: (next(ready_fds), [], []),
            ),
            patch("ctail.cli.os.read", side_effect=lambda fd, _size: reads[fd].pop(0)),
        ):
            completed = _consume_stream(
                10, console, [], False, keyboard_fd=11
            )

        self.assertTrue(completed)
        self.assertIn("visible unmatched", output.getvalue())
        self.assertNotIn("hidden unmatched", output.getvalue())
        self.assertIn("Unmatched lines shown.", output.getvalue())
        self.assertIn("Unmatched lines hidden.", output.getvalue())

    def test_enter_prints_blank_lines_without_interrupting_log_output(self):
        output = io.StringIO()
        console = Console(file=output, no_color=True)
        ready_fds = iter(([11], [11], [10], [10]))
        reads = {
            11: [b"\n", b"\r"],
            10: [b"new event\n", b""],
        }

        with (
            patch(
                "ctail.cli.select.select",
                side_effect=lambda *_args: (next(ready_fds), [], []),
            ),
            patch("ctail.cli.os.read", side_effect=lambda fd, _size: reads[fd].pop(0)),
        ):
            completed = _consume_stream(10, console, [], False, keyboard_fd=11)

        self.assertTrue(completed)
        self.assertEqual(output.getvalue(), "\n\nnew event\n")

    def test_p_selects_profile_for_subsequent_log_lines(self):
        output = io.StringIO()
        console = Console(file=output, no_color=True)
        config_path = self.config_dir / "ctail.toml"
        config_path.write_text(
            "[profiles.nginx]\n"
            "[[profiles.nginx.rules]]\n"
            'pattern = "nginx-only"\n'
            'color = "green"\n'
        )
        ready_fds = iter(([11], [10], [10]))
        reads = {11: [b"p"], 10: [b"nginx-only\n", b""]}
        terminal_state = [0, 0, 0, 0, 0, 0, [0] * 8]

        with (
            patch(
                "ctail.cli.select.select",
                side_effect=lambda *_args: (next(ready_fds), [], []),
            ),
            patch("ctail.cli.os.read", side_effect=lambda fd, _size: reads[fd].pop(0)),
            patch("ctail.cli.Live"),
            patch("ctail.cli._read_rule_key", side_effect=["down", "enter"]),
        ):
            completed = _consume_stream(
                10,
                console,
                [],
                False,
                keyboard_fd=11,
                rules_path=self.config_dir / "rules.toml",
                original_terminal=terminal_state,
            )

        self.assertTrue(completed)
        self.assertIn("nginx-only", output.getvalue())


class LessModeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config_dir = Path(self.directory.name)

    def test_less_aliases_colorize_the_entire_file_without_tailing(self):
        source = self.config_dir / "application.log"
        source.write_text("first line\n[ERROR] failure\nlast line\n", encoding="utf-8")
        (self.config_dir / "ctail.toml").write_text(
            "show_unmatched = true\n", encoding="utf-8"
        )
        pagers = []

        class PagerInput(io.StringIO):
            def close(self):
                self.contents = self.getvalue()
                super().close()

        class Pager:
            def __init__(self):
                self.stdin = PagerInput()
                pagers.append(self)

            @staticmethod
            def wait():
                return 0

        for option in ("-l", "--less"):
            with self.subTest(option=option):
                output = io.StringIO()
                with (
                    redirect_stdout(output),
                    patch.object(output, "isatty", return_value=True),
                    patch("sys.argv", ["ctail", option, str(source)]),
                    patch("ctail.cli._config_directory", return_value=self.config_dir),
                    patch("ctail.cli._less_process", side_effect=Pager),
                ):
                    main()

                rendered = pagers[-1].stdin.contents
                self.assertIn("first line", rendered)
                self.assertIn("last line", rendered)
                self.assertIn("\x1b[", rendered)


if __name__ == "__main__":
    unittest.main()
