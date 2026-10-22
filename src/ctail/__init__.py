from __future__ import annotations

import argparse
import json
import os
import re
import select
import shutil
import subprocess
import sys
import termios
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rich.color import ColorParseError
from rich.console import Console
from rich.errors import StyleSyntaxError
from rich.style import Style
from rich.text import Text


@dataclass(frozen=True)
class ColorRule:
    pattern: str
    color: str
    regex: re.Pattern[str]


DEFAULT_RULES = (
    (r"(?i)\[(?:error|fatal)\]", "bold bright_red"),
    (r"(?i)\[(?:warn|warning)\]", "bold yellow"),
    (r"(?i)\[info\]", "bright_white"),
    (r"(?i)\[debug\]", "bright_green"),
)


def _config_directory() -> Path:
    return Path.home() / ".config" / "ctail"


def _parse_rules(data: Any, source: Path) -> list[ColorRule]:
    if not isinstance(data, list):
        raise ValueError(f"{source}: 'rules' must be an array of tables")

    parsed: list[ColorRule] = []
    for index, item in enumerate(data, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"{source}: rule {index} must be a table")
        pattern = item.get("pattern")
        color = item.get("color")
        regex_enabled = item.get("regex", True)
        if not isinstance(pattern, str) or not pattern:
            raise ValueError(f"{source}: rule {index} needs a non-empty string 'pattern'")
        if not isinstance(color, str) or not color:
            raise ValueError(f"{source}: rule {index} needs a non-empty string 'color'")
        if not isinstance(regex_enabled, bool):
            raise ValueError(f"{source}: rule {index} 'regex' must be true or false")
        try:
            Style.parse(color)
            expression = pattern if regex_enabled else re.escape(pattern)
            compiled = re.compile(expression)
        except (ColorParseError, StyleSyntaxError, re.error) as error:
            raise ValueError(f"{source}: invalid rule {index}: {error}") from error
        parsed.append(ColorRule(pattern, color, compiled))
    return parsed


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as config_file:
            data = tomllib.load(config_file)
    except tomllib.TOMLDecodeError as error:
        raise ValueError(f"{path}: invalid TOML: {error}") from error
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected TOML tables")
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
            raise ValueError(f"{config_path}: 'show_unmatched' must be true or false")
        configured_rules = _parse_rules(config.get("rules", []), config_path)

    ad_hoc_rules: list[ColorRule] = []
    if rules_path.exists():
        ad_hoc_rules = _parse_rules(_read_toml(rules_path).get("rules", []), rules_path)

    defaults = [
        ColorRule(pattern, color, re.compile(pattern))
        for pattern, color in DEFAULT_RULES
    ]
    return show_unmatched, ad_hoc_rules + configured_rules + defaults, rules_path


def _render_line(line: str, rules: list[ColorRule], show_unmatched: bool) -> Text | None:
    matches: list[tuple[int, int, ColorRule]] = []
    cursor = 0
    while cursor < len(line):
        candidate: tuple[int, int, ColorRule, re.Match[str]] | None = None
        for priority, rule in enumerate(rules):
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


def _write_ad_hoc_rule(path: Path, pattern: str, color: str) -> None:
    existing: list[dict[str, Any]] = []
    if path.exists():
        existing = _read_toml(path).get("rules", [])
        if not isinstance(existing, list):
            raise ValueError(f"{path}: 'rules' must be an array of tables")
    rule_entries = [{"pattern": pattern, "color": color, "regex": False}, *existing]
    lines = [
        "# Ad-hoc literal matches created while ctail is running.",
        "# Edit or remove entries here; these are separate from ctail.toml.",
        "",
    ]
    for rule in rule_entries:
        if not isinstance(rule, dict):
            raise ValueError(f"{path}: each rule must be a table")
        rule_pattern = rule.get("pattern")
        rule_color = rule.get("color")
        regex_enabled = rule.get("regex", True)
        if not isinstance(rule_pattern, str) or not isinstance(rule_color, str):
            raise ValueError(f"{path}: each rule needs string 'pattern' and 'color' values")
        if not isinstance(regex_enabled, bool):
            raise ValueError(f"{path}: rule 'regex' must be true or false")
        lines.extend(
            (
                "[[rules]]",
                f"pattern = {json.dumps(rule_pattern, ensure_ascii=False)}",
                f"color = {json.dumps(rule_color, ensure_ascii=False)}",
                f"regex = {str(regex_enabled).lower()}",
                "",
            )
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


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
            rule = ColorRule(pattern, color, re.compile(re.escape(pattern)))
            _write_ad_hoc_rule(rules_path, pattern, color)
        except (ColorParseError, StyleSyntaxError, OSError, ValueError) as error:
            console.print(f"Could not add color rule: {error}", style="bold red")
            return
        rules.insert(0, rule)
        console.print(f"Added literal rule for {pattern!r}.", style="dim")
    except EOFError:
        console.print("Canceled.", style="dim")
    finally:
        active = termios.tcgetattr(keyboard_fd)
        active[3] &= ~(termios.ICANON | termios.ECHO)
        active[6][termios.VMIN] = 1
        active[6][termios.VTIME] = 0
        termios.tcsetattr(keyboard_fd, termios.TCSADRAIN, active)


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
            elif key in (b"c", b"C") and rules_path is not None and original_terminal is not None:
                _add_rule(console, rules, rules_path, keyboard_fd, original_terminal)
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
        description="Tail logs and colorize matching parts of each line."
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="disable colored output",
    )
    options, tail_arguments = parser.parse_known_args()

    console = Console(no_color=options.no_color or not sys.stdout.isatty())
    config_dir = _config_directory()
    process: subprocess.Popen[bytes] | None = None
    keyboard_fd: int | None = None
    original_terminal: list[Any] | None = None
    try:
        show_unmatched, rules, rules_path = _load_settings(config_dir)
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
            console.print("Space: pause/resume  c: add color rule  q: quit", style="dim")

        _consume_stream(
            source.fileno(),
            console,
            rules,
            show_unmatched,
            keyboard_fd,
            rules_path if keyboard_fd is not None else None,
            original_terminal,
        )
    except (OSError, RuntimeError, ValueError, tomllib.TOMLDecodeError) as error:
        parser.exit(2, f"ctail: error: {error}\n")
    finally:
        if keyboard_fd is not None and original_terminal is not None:
            termios.tcsetattr(keyboard_fd, termios.TCSADRAIN, original_terminal)
        if process is not None:
            if process.poll() is None:
                process.terminate()
            process.wait()
