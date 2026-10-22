from __future__ import annotations

import argparse
import os
import re
import select
import shutil
import subprocess
import sys
import tempfile
import termios
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tomlkit
from rich.color import ANSI_COLOR_NAMES, ColorParseError
from rich.console import Console, Group
from rich.errors import StyleSyntaxError
from rich.live import Live
from rich.style import Style
from rich.table import Table
from rich.text import Text
from tomlkit.exceptions import ParseError
from tomlkit.items import AoT, Array
from tomlkit.toml_document import TOMLDocument


@dataclass(frozen=True)
class ColorRule:
    pattern: str
    color: str
    regex: re.Pattern[str]
    regex_enabled: bool = True
    source: str = "built-in"
    disabled: bool = False
    source_index: int | None = None
    name: str | None = None


DEFAULT_RULES = (
    (r"(?i)\[(?:error|fatal)\]", "bold bright_red"),
    (r"(?i)\[(?:warn|warning)\]", "bold magenta"),
    (r"(?i)\[info\]", "bright_blue"),
    (r"(?i)\[debug\]", "bright_black"),
)


def _config_directory() -> Path:
    return Path.home() / ".config" / "ctail"


def _parse_rules(
    data: Any, source: Path, *, allow_builtins: bool = False
) -> list[ColorRule]:
    if not isinstance(data, list):
        raise TypeError(f"{source}: 'rules' must be an array of tables")

    parsed: list[ColorRule] = []
    for index, item in enumerate(data, start=1):
        if not isinstance(item, dict):
            raise TypeError(f"{source}: rule {index} must be a table")
        pattern = item.get("pattern")
        name = item.get("name")
        builtin = item.get("builtin", False)
        disabled = item.get("disabled", False)
        regex_enabled = item.get("regex", True)
        if name is not None and not isinstance(name, str):
            raise TypeError(f"{source}: rule {index} 'name' must be a string")
        if name == "":
            raise ValueError(f"{source}: rule {index} needs a non-empty string 'name'")
        if not isinstance(pattern, str):
            raise TypeError(f"{source}: rule {index} 'pattern' must be a string")
        if not pattern:
            raise ValueError(f"{source}: rule {index} needs a non-empty string 'pattern'")
        if not isinstance(builtin, bool):
            raise TypeError(f"{source}: rule {index} 'builtin' must be true or false")
        if builtin and (not allow_builtins or pattern not in dict(DEFAULT_RULES)):
            raise ValueError(
                f"{source}: rule {index} must reference a known built-in "
                "pattern in ctail.toml"
            )
        color = item.get("color", dict(DEFAULT_RULES).get(pattern) if builtin else None)
        if not isinstance(color, str):
            raise TypeError(f"{source}: rule {index} 'color' must be a string")
        if not color:
            raise ValueError(f"{source}: rule {index} needs a non-empty string 'color'")
        if not isinstance(disabled, bool):
            raise TypeError(f"{source}: rule {index} 'disabled' must be true or false")
        if not isinstance(regex_enabled, bool):
            raise TypeError(f"{source}: rule {index} 'regex' must be true or false")
        if builtin and not regex_enabled:
            raise ValueError(f"{source}: rule {index}: built-in patterns must use regex")
        try:
            Style.parse(color)
            expression = pattern if regex_enabled else re.escape(pattern)
            compiled = re.compile(expression)
        except (ColorParseError, StyleSyntaxError, re.error) as error:
            raise ValueError(f"{source}: invalid rule {index}: {error}") from error
        parsed.append(ColorRule(
            pattern, color, compiled, regex_enabled,
            "built-in" if builtin else str(source), disabled, index - 1,
            name=name,
        ))
    return parsed


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as config_file:
            data = tomllib.load(config_file)
    except tomllib.TOMLDecodeError as error:
        raise ValueError(f"{path}: invalid TOML: {error}") from error
    if not isinstance(data, dict):
        raise TypeError(f"{path}: expected TOML tables")
    return data


def _load_settings(config_dir: Path) -> tuple[bool, list[ColorRule], Path]:
    config_path = config_dir / "ctail.toml"
    rules_path = config_dir / "rules.toml"
    show_unmatched = False
    configured_rules: list[ColorRule] = []

    if config_path.exists():
        config = _read_toml(config_path)
        show_unmatched = config.get("show_unmatched", False)
        if not isinstance(show_unmatched, bool):
            raise TypeError(f"{config_path}: 'show_unmatched' must be true or false")
        configured_rules = _parse_rules(
            config.get("rules", []), config_path, allow_builtins=True
        )

    ad_hoc_rules: list[ColorRule] = []
    if rules_path.exists():
        ad_hoc_rules = _parse_rules(_read_toml(rules_path).get("rules", []), rules_path)

    overrides: dict[str, ColorRule] = {}
    for rule in configured_rules:
        if rule.source == "built-in":
            if rule.pattern in overrides:
                raise ValueError(f"{config_path}: duplicate built-in override {rule.pattern!r}")
            overrides[rule.pattern] = rule
    configured_rules = [rule for rule in configured_rules if rule.source != "built-in"]
    defaults = [
        overrides.get(pattern, ColorRule(pattern, color, re.compile(pattern)))
        for pattern, color in DEFAULT_RULES
    ]
    return show_unmatched, ad_hoc_rules + configured_rules + defaults, rules_path


def _list_colors(console: Console) -> None:
    console.print("Named colors (foreground or background):")
    for name in sorted({"default", *ANSI_COLOR_NAMES}):
        console.print(Text(name, style=name))
    console.print(
        'Styles: bold magenta, white on red, black on orange3\n'
        'Custom colors: #rrggbb, rgb(r,g,b), color(0) through color(255)'
    )


def _show_rules(console: Console, rules: list[ColorRule]) -> None:
    table = Table(title="Coloring rules (highest priority first)")
    for heading in ("Priority", "Source", "Status", "Mode", "Name", "Pattern", "Style"):
        table.add_column(heading)
    for priority, rule in enumerate(rules, start=1):
        table.add_row(
            str(priority),
            Text(rule.source),
            "disabled" if rule.disabled else "enabled",
            "regex" if rule.regex_enabled else "literal",
            rule.name or "-",
            Text(rule.pattern),
            Text(rule.color, style=rule.color),
        )
    console.print(table)


def _render_line(line: str, rules: list[ColorRule], show_unmatched: bool) -> Text | None:
    matches: list[tuple[int, int, ColorRule]] = []
    cursor = 0
    while cursor < len(line):
        candidate: tuple[int, int, ColorRule, re.Match[str]] | None = None
        for priority, rule in enumerate(rules):
            if rule.disabled:
                continue
            match = rule.regex.search(line, cursor)
            if match is None:
                continue
            if match.end() == match.start():
                raise ValueError(f"Color rule {rule.pattern!r} matched an empty string")
            possible = (match.start(), priority, rule, match)
            if candidate is None or possible[:2] < candidate[:2]:
                candidate = possible
        if candidate is None:
            break
        start, priority, rule, match = candidate
        matches.append((start, match.end(), rule))
        cursor = match.end()

    if not matches and not show_unmatched:
        return None

    rendered = Text()
    cursor = 0
    for start, end, rule in matches:
        rendered.append(line[cursor:start])
        rendered.append(line[start:end], style=rule.color)
        cursor = end
    rendered.append(line[cursor:])
    return rendered


def _read_rule_document(path: Path) -> tuple[TOMLDocument, AoT | Array]:
    if path.exists():
        try:
            document = tomlkit.parse(path.read_text(encoding="utf-8"))
        except ParseError as error:
            raise ValueError(f"{path}: invalid TOML: {error}") from error
    else:
        document = tomlkit.document()
    _parse_rules(
        document.unwrap().get("rules", []), path,
        allow_builtins=path.name == "ctail.toml",
    )
    if "rules" not in document:
        document["rules"] = tomlkit.aot()
    entries = document["rules"]
    if not isinstance(entries, (AoT, Array)):
        raise TypeError(f"{path}: 'rules' must be an array of tables")
    return document, entries


def _save_rule_document(path: Path, document: TOMLDocument) -> None:
    _parse_rules(
        document.unwrap().get("rules", []), path,
        allow_builtins=path.name == "ctail.toml",
    )
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(tomlkit.dumps(document))
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        temporary_path.chmod(mode)
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _write_ad_hoc_rule(path: Path, pattern: str, color: str) -> None:
    document, entries = _read_rule_document(path)
    entry = tomlkit.inline_table() if isinstance(entries, Array) else tomlkit.table()
    entry.update({"pattern": pattern, "color": color, "regex": False, "disabled": False})
    entries.insert(0, entry)
    _save_rule_document(path, document)


def _change_rule(rule: ColorRule, rules_path: Path, *, delete: bool) -> None:
    if delete and rule.source == "built-in":
        raise ValueError("Built-in rules cannot be deleted; disable them instead.")
    _, current_rules, _ = _load_settings(rules_path.parent)
    if rule not in current_rules:
        raise ValueError("Rules changed on disk; restart ctail before editing this rule.")
    path = rules_path.with_name("ctail.toml") if rule.source == "built-in" else Path(rule.source)
    document, entries = _read_rule_document(path)
    if rule.source_index is None:
        if rule.source != "built-in":
            raise ValueError("Rule has no saved TOML entry.")
        entry = tomlkit.inline_table() if isinstance(entries, Array) else tomlkit.table()
        entry.update(
            {"builtin": True, "pattern": rule.pattern, "disabled": not rule.disabled}
        )
        entries.append(entry)
    elif delete:
        entries.pop(rule.source_index)
    else:
        entries[rule.source_index]["disabled"] = not rule.disabled
    _save_rule_document(path, document)


def _add_rule(
    console: Console,
    rules: list[ColorRule],
    rules_path: Path,
    keyboard_fd: int,
    original_terminal: list[Any],
) -> None:
    termios.tcsetattr(keyboard_fd, termios.TCSADRAIN, original_terminal)
    try:
        pattern = input("Literal text to color (blank cancels): ").strip()
        if not pattern:
            console.print("Canceled.", style="dim")
            return
        color = input("Rich color/style (for example: orange3 or bold magenta): ").strip()
        if not color:
            console.print("Canceled.", style="dim")
            return
        try:
            Style.parse(color)
            _write_ad_hoc_rule(rules_path, pattern, color)
            _, loaded_rules, _ = _load_settings(rules_path.parent)
        except (ColorParseError, StyleSyntaxError, OSError, TypeError, ValueError) as error:
            console.print(f"Could not add color rule: {error}", style="bold red")
            return
        rules[:] = loaded_rules
        console.print(f"Added literal rule for {pattern!r}.", style="dim")
    except EOFError:
        console.print("Canceled.", style="dim")
    finally:
        active = termios.tcgetattr(keyboard_fd)
        active[3] &= ~(termios.ICANON | termios.ECHO)
        active[6][termios.VMIN] = 1
        active[6][termios.VTIME] = 0
        termios.tcsetattr(keyboard_fd, termios.TCSADRAIN, active)


def _read_rule_key(keyboard_fd: int) -> str:
    key = os.read(keyboard_fd, 1)
    if key == b"\x1b":
        sequence = bytearray()
        for _ in range(2):
            ready, _, _ = select.select([keyboard_fd], [], [], 0.15)
            if not ready:
                return "cancel"
            sequence.extend(os.read(keyboard_fd, 1))
            if len(sequence) == 1 and sequence not in (b"[", b"O"):
                return "cancel"
        if sequence in (b"[A", b"OA"):
            return "up"
        if sequence in (b"[B", b"OB"):
            return "down"
        return "cancel"
    if key in (b"\r", b"\n"):
        return "enter"
    return "cancel" if not key else ""


def _select_rule(
    console: Console, rules: list[ColorRule], keyboard_fd: int, *, delete: bool
) -> ColorRule | None:
    choices = [rule for rule in rules if not delete or rule.source != "built-in"]
    if not choices:
        console.print("No removable rules.", style="dim")
        return None
    selected = 0

    def render() -> Text:
        action = "Delete rule" if delete else "Enable/disable rule"
        view = Text(f"{action}\nUp/Down: select  Enter: apply  Escape: cancel\n\n")
        page_size = max(1, console.size.height - 5)
        start = (selected // page_size) * page_size
        for index in range(start, min(start + page_size, len(choices))):
            rule = choices[index]
            source = "built-in" if rule.source == "built-in" else Path(rule.source).name
            row = Text(
                f"{'> ' if index == selected else '  '}"
                f"[{'disabled' if rule.disabled else 'enabled'}] {source} "
                f"({'regex' if rule.regex_enabled else 'literal'}): "
            )
            if rule.name:
                row.append(f"{rule.name}: ")
            row.append(repr(rule.pattern), style=rule.color)
            row.append(f" ({rule.color})")
            row.truncate(max(1, console.size.width - 1), overflow="ellipsis")
            if index == selected:
                row.stylize("reverse")
            view.append_text(row)
            view.append("\n")
        view.append(f"\n{selected + 1}/{len(choices)}")
        return view

    with Live(
        render(), console=console, screen=True, transient=True,
        auto_refresh=False, redirect_stdout=False, redirect_stderr=False,
    ) as live:
        while True:
            key = _read_rule_key(keyboard_fd)
            if key == "cancel":
                return None
            if key == "enter":
                return choices[selected]
            if key == "up":
                selected = max(0, selected - 1)
            elif key == "down":
                selected = min(len(choices) - 1, selected + 1)
            live.update(render(), refresh=True)


def _manage_rules(
    console: Console, rules: list[ColorRule], rules_path: Path,
    keyboard_fd: int, *, delete: bool,
) -> None:
    rule = _select_rule(console, rules, keyboard_fd, delete=delete)
    if rule is None:
        console.print("Canceled.", style="dim")
        return
    try:
        _change_rule(rule, rules_path, delete=delete)
        _, loaded_rules, _ = _load_settings(rules_path.parent)
    except (OSError, TypeError, ValueError) as error:
        console.print(Text(f"Could not change color rule: {error}", style="bold red"))
        return
    rules[:] = loaded_rules
    action = "Deleted" if delete else ("Enabled" if rule.disabled else "Disabled")
    console.print(Text(f"{action} rule for {rule.pattern!r}.", style="dim"))


def _show_interactive_help(console: Console, keyboard_fd: int) -> None:
    commands = Table(show_header=True, header_style="bold")
    commands.add_column("Key", style="cyan", no_wrap=True)
    commands.add_column("Command")
    commands.add_row("Space", "Pause or resume log output")
    commands.add_row("c", "Add a literal color rule")
    commands.add_row("f", "Toggle display of unmatched lines")
    commands.add_row("r", "Select and remove a custom rule")
    commands.add_row("d", "Select a rule and enable or disable it")
    commands.add_row("?", "Show this command list")
    commands.add_row("q", "Quit ctail")
    help_view = Group(
        Text("ctail interactive commands", style="bold"),
        commands,
        Text("Press any key to return to tailing", style="dim"),
    )
    with Live(
        help_view, console=console, screen=True, transient=True,
        auto_refresh=False, redirect_stdout=False, redirect_stderr=False,
    ):
        os.read(keyboard_fd, 1)


def _consume_stream(
    source_fd: int,
    console: Console,
    rules: list[ColorRule],
    show_unmatched: bool,
    keyboard_fd: int | None = None,
    rules_path: Path | None = None,
    original_terminal: list[Any] | None = None,
) -> bool:
    buffer = bytearray()
    paused = False
    running = True
    while running:
        read_fds = [source_fd] if not paused else []
        if keyboard_fd is not None:
            read_fds.append(keyboard_fd)
        if not read_fds:
            continue
        ready, _, _ = select.select(read_fds, [], [], 0.25)

        if keyboard_fd is not None and keyboard_fd in ready:
            key = os.read(keyboard_fd, 1)
            if key == b" ":
                paused = not paused
                console.print("Paused." if paused else "Resumed.", style="dim")
            elif key in (b"f", b"F"):
                show_unmatched = not show_unmatched
                state = "shown" if show_unmatched else "hidden"
                console.print(f"Unmatched lines {state}.", style="dim")
            elif key in (b"c", b"C") and rules_path is not None and original_terminal is not None:
                _add_rule(console, rules, rules_path, keyboard_fd, original_terminal)
            elif key in (b"r", b"R", b"d", b"D") and rules_path is not None:
                _manage_rules(
                    console, rules, rules_path, keyboard_fd, delete=key in (b"r", b"R")
                )
            elif key == b"?":
                _show_interactive_help(console, keyboard_fd)
            elif key in (b"q", b"Q"):
                running = False

        if not running:
            break
        if source_fd not in ready or paused:
            continue
        chunk = os.read(source_fd, 8192)
        if not chunk:
            if buffer:
                line = buffer.decode("utf-8", errors="replace")
                rendered = _render_line(line, rules, show_unmatched)
                if rendered is not None:
                    console.print(rendered, end="", soft_wrap=True)
            return True
        buffer.extend(chunk)
        while True:
            newline = buffer.find(b"\n")
            if newline < 0:
                break
            raw_line = bytes(buffer[: newline + 1])
            del buffer[: newline + 1]
            rendered = _render_line(raw_line.decode("utf-8", errors="replace"), rules, show_unmatched)
            if rendered is not None:
                console.print(rendered, end="", soft_wrap=True)
    return False


def _tail_process(arguments: list[str]) -> subprocess.Popen[bytes]:
    tail = shutil.which("tail")
    if tail is None:
        raise RuntimeError("could not find the 'tail' executable")
    try:
        process = subprocess.Popen(
            [tail, *arguments],
            stdout=subprocess.PIPE,
            bufsize=0,
        )
    except OSError as error:
        raise RuntimeError(f"could not start tail: {error}") from error
    if process.stdout is None:
        process.terminate()
        raise RuntimeError("tail did not provide an output stream")
    return process


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Tail logs and colorize matching parts of each line.",
        epilog=(
            "Interactive file-following keys (when stdin and stdout are terminals):\n"
            "  Space  Pause or resume log output\n"
            "  c      Add a literal color rule\n"
            "  r      Select and remove a custom rule\n"
            "  d      Select a rule and enable or disable it\n"
            "  ?      Show the interactive command list\n"
            "  q      Quit ctail"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="disable colored output",
    )
    parser.add_argument(
        "-lsc", "--list-colors",
        action="store_true",
        help="list available named colors and exit",
    )
    parser.add_argument(
        "-shr", "--show-rules",
        action="store_true",
        help="show ad-hoc, user-configured, and built-in coloring rules and exit",
    )
    options, tail_arguments = parser.parse_known_args()

    console = Console(
        color_system="auto" if sys.stdout.isatty() and not options.no_color else None
    )
    config_dir = _config_directory()
    process: subprocess.Popen[bytes] | None = None
    keyboard_fd: int | None = None
    original_terminal: list[Any] | None = None
    try:
        if options.list_colors:
            _list_colors(console)
            if not options.show_rules:
                return
        show_unmatched, rules, rules_path = _load_settings(config_dir)
        if options.show_rules:
            _show_rules(console, rules)
            return
        has_file_source = sys.stdin.isatty() and bool(tail_arguments)
        process = _tail_process(tail_arguments) if has_file_source else None
        source = process.stdout if process is not None else sys.stdin.buffer
        if source is None:
            raise RuntimeError("no input stream available")

        if process is not None and sys.stdin.isatty() and sys.stdout.isatty():
            keyboard_fd = sys.stdin.fileno()
            original_terminal = termios.tcgetattr(keyboard_fd)
            active_terminal = termios.tcgetattr(keyboard_fd)
            active_terminal[3] &= ~(termios.ICANON | termios.ECHO)
            active_terminal[6][termios.VMIN] = 1
            active_terminal[6][termios.VTIME] = 0
            termios.tcsetattr(keyboard_fd, termios.TCSADRAIN, active_terminal)
            console.print(
                "Space: pause/resume  c: add rule  r: remove rule  "
                "d: enable/disable rule  ?: help  q: quit", style="dim",
            )

        _consume_stream(
            source.fileno(),
            console,
            rules,
            show_unmatched,
            keyboard_fd,
            rules_path if keyboard_fd is not None else None,
            original_terminal,
        )
    except (OSError, RuntimeError, TypeError, ValueError, tomllib.TOMLDecodeError) as error:
        parser.exit(2, f"ctail: error: {error}\n")
    finally:
        if keyboard_fd is not None and original_terminal is not None:
            termios.tcsetattr(keyboard_fd, termios.TCSADRAIN, original_terminal)
        if process is not None:
            if process.poll() is None:
                process.terminate()
            process.wait()
