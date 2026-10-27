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
from typing import Any, BinaryIO, TextIO

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
    group_colors: tuple[str, ...] = ()
    profile_name: str | None = None


DEFAULT_RULES = (
    (r"(?i)\[(?:error|fatal)\]", "bold bright_red"),
    (r"(?i)\[(?:warn|warning)\]", "bold magenta"),
    (r"(?i)\[info\]", "bright_blue"),
    (r"(?i)\[debug\]", "bright_black"),
)


def _config_directory() -> Path:
    return Path.home() / ".config" / "ctail"


def _example_config_directory() -> Path:
    installed_config_dir = Path(__file__).resolve().parent
    filenames = ("ctail.toml", "rules.toml")
    if all((installed_config_dir / name).is_file() for name in filenames):
        return installed_config_dir

    source_config_dir = Path(__file__).resolve().parents[2] / "config" / "ctail"
    if all((source_config_dir / name).is_file() for name in filenames):
        return source_config_dir

    raise FileNotFoundError("Could not locate packaged ctail configuration examples")


def _initialize_config(config_dir: Path) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)

    example_dir = _example_config_directory()
    for name in ("ctail.toml", "rules.toml"):
        config_path = config_dir / name
        if config_path.exists():
            continue

        example = (example_dir / name).read_text(encoding="utf-8")
        try:
            with config_path.open("x", encoding="utf-8") as config_file:
                config_file.write(example)
        except FileExistsError:
            pass


def _split_colors(value: str) -> tuple[str, ...]:
    colors: list[str] = []
    start = 0
    parentheses = 0
    for index, character in enumerate(value):
        if character == "(":
            parentheses += 1
        elif character == ")":
            parentheses = max(0, parentheses - 1)
        elif character == "," and parentheses == 0:
            colors.append(value[start:index].strip())
            start = index + 1
    colors.append(value[start:].strip())
    return tuple(colors)


def _parse_rules(
    data: Any,
    source: Path,
    *,
    allow_builtins: bool = False,
    profile_name: str | None = None,
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
        colors = _split_colors(color)
        if any(not item for item in colors):
            raise ValueError(
                f"{source}: rule {index} 'color' must not contain an empty color"
            )
        if not isinstance(disabled, bool):
            raise TypeError(f"{source}: rule {index} 'disabled' must be true or false")
        if not isinstance(regex_enabled, bool):
            raise TypeError(f"{source}: rule {index} 'regex' must be true or false")
        if builtin and not regex_enabled:
            raise ValueError(f"{source}: rule {index}: built-in patterns must use regex")
        try:
            for item in colors:
                Style.parse(item)
            expression = pattern if regex_enabled else re.escape(pattern)
            compiled = re.compile(expression)
        except (ColorParseError, StyleSyntaxError, re.error) as error:
            raise ValueError(f"{source}: invalid rule {index}: {error}") from error
        group_colors: tuple[str, ...] = ()
        if compiled.groups:
            if len(colors) > compiled.groups + 1:
                raise ValueError(
                    f"{source}: rule {index} has {compiled.groups} capture groups; "
                    f"'color' accepts at most {compiled.groups + 1} colors"
                )
            group_colors = colors
        elif len(colors) > 1:
            raise ValueError(
                f"{source}: rule {index} has no capture groups; "
                "'color' must contain a single color"
            )
        parsed.append(ColorRule(
            pattern, color, compiled, regex_enabled,
            "built-in" if builtin else str(source), disabled, index - 1,
            name=name, group_colors=group_colors, profile_name=profile_name,
        ))
    return parsed


def _parse_rule_profiles(
    data: dict[str, Any],
    source: Path,
    *,
    allow_builtins: bool = False,
) -> dict[str, list[ColorRule]]:
    profiles = data.get("profiles", {})
    if not isinstance(profiles, dict):
        raise TypeError(f"{source}: 'profiles' must be a table")

    parsed: dict[str, list[ColorRule]] = {}
    for name, profile in profiles.items():
        if not isinstance(name, str) or not name:
            raise ValueError(f"{source}: profile names must be non-empty strings")
        if not isinstance(profile, dict):
            raise TypeError(f"{source}: profile {name!r} must be a table")
        parsed[name] = _parse_rules(
            profile.get("rules", []),
            source,
            allow_builtins=allow_builtins,
            profile_name=name,
        )
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


def _configured_active_profile(data: dict[str, Any], source: Path) -> str | None:
    profile = data.get("active_profile")
    if profile is not None and (not isinstance(profile, str) or not profile):
        raise TypeError(f"{source}: 'active_profile' must be a non-empty string")
    return profile


def _read_active_profile(config_path: Path) -> str | None:
    if not config_path.exists():
        return None
    config = _read_toml(config_path)
    return _configured_active_profile(config, config_path)


def _load_settings(
    config_dir: Path, profile: str | None = None
) -> tuple[bool, list[ColorRule], Path]:
    config_path = config_dir / "ctail.toml"
    rules_path = config_dir / "rules.toml"
    show_unmatched = False
    configured_rules: list[ColorRule] = []
    configured_profiles: dict[str, list[ColorRule]] = {}

    if config_path.exists():
        config = _read_toml(config_path)
        show_unmatched = config.get("show_unmatched", False)
        if not isinstance(show_unmatched, bool):
            raise TypeError(f"{config_path}: 'show_unmatched' must be true or false")
        configured_rules = _parse_rules(
            config.get("rules", []), config_path, allow_builtins=True
        )
        configured_profiles = _parse_rule_profiles(
            config, config_path, allow_builtins=True
        )

    ad_hoc_rules: list[ColorRule] = []
    profile_ad_hoc_rules: dict[str, list[ColorRule]] = {}
    if rules_path.exists():
        rules_data = _read_toml(rules_path)
        ad_hoc_rules = _parse_rules(rules_data.get("rules", []), rules_path)
        profile_ad_hoc_rules = _parse_rule_profiles(rules_data, rules_path)
        unknown_profiles = sorted(set(profile_ad_hoc_rules) - set(configured_profiles))
        if unknown_profiles:
            names = ", ".join(repr(name) for name in unknown_profiles)
            raise ValueError(
                f"{rules_path}: temporary rules reference profiles not defined "
                f"in {config_path}: {names}"
            )

    if profile is not None:
        if profile not in configured_profiles:
            available = ", ".join(sorted(configured_profiles)) or "none"
            raise ValueError(
                f"{config_path}: unknown profile {profile!r}; "
                f"available profiles: {available}"
            )
        configured_rules = configured_profiles[profile]

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
    scoped_ad_hoc_rules = profile_ad_hoc_rules.get(profile, []) if profile else []
    return (
        show_unmatched,
        ad_hoc_rules + scoped_ad_hoc_rules + configured_rules + defaults,
        rules_path,
    )


def _list_profiles(console: Console, config_path: Path) -> None:
    config = _read_toml(config_path) if config_path.exists() else {}
    profiles = _parse_rule_profiles(config, config_path, allow_builtins=True)
    if not profiles:
        console.print("No named profiles configured.")
        return
    console.print("Configured profiles:")
    for name in sorted(profiles):
        console.print(f"  {name}")


def _list_colors(console: Console) -> None:
    console.print("Named colors (foreground or background):")
    for name in sorted({"default", *ANSI_COLOR_NAMES}):
        console.print(Text(name, style=name))
    console.print(
        'Styles: bold magenta, white on red, black on orange3\n'
        'Custom colors: #rrggbb, rgb(r,g,b), color(0) through color(255)'
    )


def _show_rules(
    console: Console, rules: list[ColorRule], profile: str | None = None
) -> None:
    if profile is not None:
        rules = [rule for rule in rules if rule.profile_name == profile]
    title = (
        f"Coloring rules for profile {profile!r} (highest priority first)"
        if profile is not None
        else "Coloring rules (highest priority first)"
    )
    table = Table(title=title)
    for heading in ("Priority", "Source", "Status", "Mode", "Name", "Pattern", "Style"):
        table.add_column(heading)
    for priority, rule in enumerate(rules, start=1):
        source = rule.source
        if rule.profile_name is not None:
            source = f"{source} (profile: {rule.profile_name})"
        table.add_row(
            str(priority),
            Text(source),
            "disabled" if rule.disabled else "enabled",
            "regex" if rule.regex_enabled else "literal",
            rule.name or "-",
            Text(rule.pattern),
            Text(
                rule.color,
                style=rule.group_colors[0] if rule.group_colors else rule.color,
            ),
        )
    console.print(table)


def _render_line(line: str, rules: list[ColorRule], show_unmatched: bool) -> Text | None:
    matches: list[tuple[int, int, ColorRule, re.Match[str]]] = []
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
        matches.append((start, match.end(), rule, match))
        cursor = match.end()

    if not matches and not show_unmatched:
        return None

    rendered = Text()
    cursor = 0
    for start, end, rule, match in matches:
        rendered.append(line[cursor:start])
        if rule.group_colors:
            base_color = (
                rule.group_colors[rule.regex.groups]
                if len(rule.group_colors) > rule.regex.groups
                else None
            )
            rendered.append(line[start:end], style=base_color)
            for group_index, group_color in enumerate(
                rule.group_colors[:rule.regex.groups], start=1
            ):
                group_start, group_end = match.span(group_index)
                if group_start >= 0 and group_end > group_start:
                    rendered.stylize(group_color, group_start, group_end)
        else:
            rendered.append(line[start:end], style=rule.color)
        cursor = end
    rendered.append(line[cursor:])
    return rendered


def _read_rule_document(
    path: Path,
    profile_name: str | None = None,
    *,
    create_profile: bool = False,
) -> tuple[TOMLDocument, AoT | Array]:
    if path.exists():
        try:
            document = tomlkit.parse(path.read_text(encoding="utf-8"))
        except ParseError as error:
            raise ValueError(f"{path}: invalid TOML: {error}") from error
    else:
        document = tomlkit.document()
    data = document.unwrap()
    _parse_rules(
        data.get("rules", []), path,
        allow_builtins=path.name == "ctail.toml",
    )
    _parse_rule_profiles(
        data, path, allow_builtins=path.name == "ctail.toml"
    )
    if profile_name is None:
        if "rules" not in document:
            document["rules"] = tomlkit.aot()
        entries = document["rules"]
    else:
        profiles = document.get("profiles")
        if profiles is None and create_profile:
            document["profiles"] = tomlkit.table()
            profiles = document["profiles"]
        if profiles is None or profile_name not in profiles:
            if not create_profile:
                raise ValueError(f"{path}: profile {profile_name!r} no longer exists")
            profiles[profile_name] = tomlkit.table()
        profile = profiles[profile_name]
        if "rules" not in profile:
            profile["rules"] = tomlkit.aot()
        entries = profile["rules"]
    if not isinstance(entries, (AoT, Array)):
        section = f"profile {profile_name!r} " if profile_name is not None else ""
        raise TypeError(f"{path}: {section}'rules' must be an array of tables")
    return document, entries


def _save_rule_document(path: Path, document: TOMLDocument) -> None:
    _parse_rules(
        document.unwrap().get("rules", []), path,
        allow_builtins=path.name == "ctail.toml",
    )
    profiles = _parse_rule_profiles(
        document.unwrap(), path, allow_builtins=path.name == "ctail.toml"
    )
    active_profile = _configured_active_profile(document.unwrap(), path)
    if active_profile is not None and active_profile not in profiles:
        raise ValueError(
            f"{path}: active profile {active_profile!r} is not configured"
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


def _write_active_profile(path: Path, profile_name: str | None) -> None:
    if path.exists():
        try:
            document = tomlkit.parse(path.read_text(encoding="utf-8"))
        except ParseError as error:
            raise ValueError(f"{path}: invalid TOML: {error}") from error
    else:
        document = tomlkit.document()

    _configured_active_profile(document.unwrap(), path)
    if profile_name is None:
        document.pop("active_profile", None)
    else:
        document["active_profile"] = profile_name
    _save_rule_document(path, document)


def _write_profile(path: Path, profile_name: str) -> None:
    if not profile_name:
        raise ValueError("profile name must not be empty")
    if path.exists():
        try:
            document = tomlkit.parse(path.read_text(encoding="utf-8"))
        except ParseError as error:
            raise ValueError(f"{path}: invalid TOML: {error}") from error
    else:
        document = tomlkit.document()

    data = document.unwrap()
    _parse_rules(data.get("rules", []), path, allow_builtins=True)
    profiles = _parse_rule_profiles(data, path, allow_builtins=True)
    if profile_name in profiles:
        raise ValueError(f"{path}: profile {profile_name!r} already exists")

    if "profiles" not in document:
        document["profiles"] = tomlkit.table()
    document["profiles"][profile_name] = tomlkit.table()
    _save_rule_document(path, document)


def _write_ad_hoc_rule(
    path: Path,
    pattern: str,
    color: str,
    profile_name: str | None = None,
) -> None:
    document, entries = _read_rule_document(
        path, profile_name, create_profile=profile_name is not None
    )
    entry = tomlkit.inline_table() if isinstance(entries, Array) else tomlkit.table()
    entry.update({"pattern": pattern, "color": color, "regex": False, "disabled": False})
    entries.insert(0, entry)
    _save_rule_document(path, document)


def _change_rule(
    rule: ColorRule,
    rules_path: Path,
    *,
    delete: bool,
    profile: str | None = None,
) -> None:
    if delete and rule.source == "built-in":
        raise ValueError("Built-in rules cannot be deleted; disable them instead.")
    _, current_rules, _ = _load_settings(rules_path.parent, profile)
    if rule not in current_rules:
        raise ValueError("Rules changed on disk; restart ctail before editing this rule.")
    path = rules_path.with_name("ctail.toml") if rule.source == "built-in" else Path(rule.source)
    document, entries = _read_rule_document(path, rule.profile_name)
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
    profile: str | None = None,
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
            _write_ad_hoc_rule(rules_path, pattern, color, profile)
            _, loaded_rules, _ = _load_settings(rules_path.parent, profile)
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
    return "cancel" if not key else key.decode("utf-8", errors="replace")


def _select_profile(
    console: Console,
    config_path: Path,
    keyboard_fd: int,
    active_profile: str | None = None,
) -> tuple[bool, str | None] | None:
    config = _read_toml(config_path) if config_path.exists() else {}
    choices: list[str | None] = [None, *sorted(
        _parse_rule_profiles(config, config_path, allow_builtins=True)
    )]
    selected = 0

    def render() -> Group:
        table = Table(title="Select profile", show_header=False, box=None)
        table.add_column("Profile")
        page_size = max(1, console.size.height - 8)
        start = (selected // page_size) * page_size
        for index in range(start, min(start + page_size, len(choices))):
            name = "(default)" if choices[index] is None else choices[index]
            row = Text(name)
            if choices[index] == active_profile:
                row.append(" (active)", style="bold green")
            if index == selected:
                row.stylize("reverse")
            table.add_row(row)
        return Group(
            table,
            Text("n  Add a new profile", style="dim"),
            Text("Up/Down: select  Enter: apply  Escape: cancel", style="dim"),
            Text(f"{selected + 1}/{len(choices)}", style="dim"),
        )

    with Live(
        render(), console=console, screen=True, transient=True,
        auto_refresh=False, redirect_stdout=False, redirect_stderr=False,
    ) as live:
        while True:
            key = _read_rule_key(keyboard_fd)
            if key == "cancel":
                return None
            if key == "enter":
                return False, choices[selected]
            if key in ("n", "N"):
                return True, None
            if key == "up":
                selected = max(0, selected - 1)
            elif key == "down":
                selected = min(len(choices) - 1, selected + 1)
            live.update(render(), refresh=True)


def _prompt_profile_name(
    console: Console,
    keyboard_fd: int,
    original_terminal: list[Any],
) -> str | None:
    termios.tcsetattr(keyboard_fd, termios.TCSADRAIN, original_terminal)
    try:
        name = input("New profile name (blank cancels): ").strip()
        return name or None
    except EOFError:
        console.print("Canceled.", style="dim")
        return None
    finally:
        active = termios.tcgetattr(keyboard_fd)
        active[3] &= ~(termios.ICANON | termios.ECHO)
        active[6][termios.VMIN] = 1
        active[6][termios.VTIME] = 0
        termios.tcsetattr(keyboard_fd, termios.TCSADRAIN, active)


def _manage_profiles(
    console: Console,
    rules: list[ColorRule],
    rules_path: Path,
    keyboard_fd: int,
    original_terminal: list[Any],
    profile: str | None,
) -> str | None:
    config_path = rules_path.with_name("ctail.toml")
    while True:
        try:
            selection = _select_profile(
                console, config_path, keyboard_fd, profile
            )
        except (OSError, TypeError, ValueError) as error:
            console.print(f"Could not list profiles: {error}", style="bold red")
            return profile
        if selection is None:
            return profile

        add_new, selected_profile = selection
        if add_new:
            name = _prompt_profile_name(console, keyboard_fd, original_terminal)
            if name is None:
                console.print("Canceled.", style="dim")
                continue
            try:
                _write_profile(config_path, name)
            except (OSError, TypeError, ValueError) as error:
                console.print(f"Could not add profile: {error}", style="bold red")
                continue
            try:
                _, loaded_rules, _ = _load_settings(rules_path.parent, name)
            except (OSError, TypeError, ValueError) as error:
                console.print(
                    f"Profile {name!r} was saved but could not be activated: {error}",
                    style="bold red",
                )
                return profile
            try:
                _write_active_profile(config_path, name)
            except (OSError, TypeError, ValueError) as error:
                console.print(
                    f"Profile {name!r} was created but could not be activated: {error}",
                    style="bold red",
                )
                return profile
            rules[:] = loaded_rules
            console.print(f"Added and selected profile {name!r}.", style="dim")
            return name

        try:
            _, loaded_rules, _ = _load_settings(
                rules_path.parent, selected_profile
            )
            _write_active_profile(config_path, selected_profile)
        except (OSError, TypeError, ValueError) as error:
            console.print(f"Could not select profile: {error}", style="bold red")
            return profile
        rules[:] = loaded_rules
        label = selected_profile if selected_profile is not None else "(default)"
        console.print(f"Selected profile {label!r}.", style="dim")
        return selected_profile


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
            if rule.profile_name is not None:
                source = f"{source} ({rule.profile_name})"
            row = Text(
                f"{'> ' if index == selected else '  '}"
                f"[{'disabled' if rule.disabled else 'enabled'}] {source} "
                f"({'regex' if rule.regex_enabled else 'literal'}): "
            )
            if rule.name:
                row.append(f"{rule.name}: ")
            row.append(
                repr(rule.pattern),
                style=rule.group_colors[0] if rule.group_colors else rule.color,
            )
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
    keyboard_fd: int, *, delete: bool, profile: str | None = None,
) -> None:
    rule = _select_rule(console, rules, keyboard_fd, delete=delete)
    if rule is None:
        console.print("Canceled.", style="dim")
        return
    try:
        _change_rule(rule, rules_path, delete=delete, profile=profile)
        _, loaded_rules, _ = _load_settings(rules_path.parent, profile)
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
    commands.add_row("p", "Select a coloring-rule profile")
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
    profile: str | None = None,
) -> bool:
    buffer = bytearray()
    paused = False
    running = True
    active_profile = profile
    while running:
        read_fds = [source_fd] if not paused else []
        if keyboard_fd is not None:
            read_fds.append(keyboard_fd)
        if not read_fds:
            continue
        ready, _, _ = select.select(read_fds, [], [], 0.25)

        if keyboard_fd is not None and keyboard_fd in ready:
            key = os.read(keyboard_fd, 1)
            if key in (b"\r", b"\n"):
                console.print()
            elif key == b" ":
                paused = not paused
                console.print("Paused." if paused else "Resumed.", style="dim")
            elif key in (b"p", b"P") and rules_path is not None and original_terminal is not None:
                active_profile = _manage_profiles(
                    console, rules, rules_path, keyboard_fd, original_terminal,
                    active_profile,
                )
            elif key in (b"f", b"F"):
                show_unmatched = not show_unmatched
                state = "shown" if show_unmatched else "hidden"
                console.print(f"Unmatched lines {state}.", style="dim")
            elif key in (b"c", b"C") and rules_path is not None and original_terminal is not None:
                _add_rule(
                    console, rules, rules_path, keyboard_fd, original_terminal,
                    active_profile,
                )
            elif key in (b"r", b"R", b"d", b"D") and rules_path is not None:
                _manage_rules(
                    console, rules, rules_path, keyboard_fd,
                    delete=key in (b"r", b"R"), profile=active_profile,
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


def _less_process() -> subprocess.Popen[str]:
    less = shutil.which("less")
    if less is None:
        raise RuntimeError("could not find the 'less' executable")
    try:
        process = subprocess.Popen(
            [less, "-R"],
            stdin=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
    except OSError as error:
        raise RuntimeError(f"could not start less: {error}") from error
    if process.stdin is None:
        process.terminate()
        raise RuntimeError("less did not provide an input stream")
    return process


def _colorize_stream(
    source: BinaryIO,
    console: Console,
    rules: list[ColorRule],
    show_unmatched: bool,
) -> None:
    for raw_line in source:
        rendered = _render_line(raw_line.decode("utf-8", errors="replace"), rules, show_unmatched)
        if rendered is not None:
            console.print(rendered, end="", soft_wrap=True)


def _close_pager_input(stream: TextIO) -> None:
    try:
        stream.close()
    except BrokenPipeError:
        pass


def _show_with_less(
    arguments: list[str],
    console: Console,
    rules: list[ColorRule],
    show_unmatched: bool,
) -> None:
    file_arguments = arguments[1:] if arguments[:1] == ["--"] else arguments
    if any(argument.startswith("-") for argument in file_arguments):
        raise ValueError("--less accepts file paths, not tail options")
    if not file_arguments and sys.stdin.isatty():
        raise ValueError("provide a file or piped input when using --less")

    process = _less_process()
    assert process.stdin is not None
    pager_console = Console(
        file=process.stdin,
        color_system=console.color_system,
        force_terminal=console.is_terminal,
        width=console.width,
    )
    try:
        if file_arguments:
            for filename in file_arguments:
                with Path(filename).open("rb") as source:
                    _colorize_stream(source, pager_console, rules, show_unmatched)
        else:
            _colorize_stream(sys.stdin.buffer, pager_console, rules, show_unmatched)
    except BrokenPipeError:
        pass
    except OSError:
        process.terminate()
        _close_pager_input(process.stdin)
        process.wait()
        raise
    _close_pager_input(process.stdin)
    return_code = process.wait()
    if return_code:
        raise RuntimeError(f"less exited with status {return_code}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Colorize log output and either tail it or page through it.",
        epilog=(
            "Interactive file-following keys (when stdin and stdout are terminals):\n"
            "  Enter  Print a blank line to separate log output\n"
            "  Space  Pause or resume log output\n"
            "  p      Select a coloring-rule profile (n adds one)\n"
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
        "-p", "--profile",
        metavar="NAME",
        help="use a named coloring-rule profile from ctail.toml",
    )
    parser.add_argument(
        "-l", "--less",
        action="store_true",
        help="show the full file in less instead of tailing it",
    )
    parser.add_argument(
        "-lsc", "--list-colors",
        action="store_true",
        help="list available named colors and exit",
    )
    parser.add_argument(
        "-lsp", "--list-profiles",
        action="store_true",
        help="list configured coloring-rule profiles and exit",
    )
    parser.add_argument(
        "-shr", "--show-rules",
        action="store_true",
        help="show coloring rules, optionally scoped to a profile, and exit",
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
        _initialize_config(config_dir)
        if options.list_colors:
            _list_colors(console)
            if not options.show_rules and not options.list_profiles:
                return
        if options.list_profiles:
            _list_profiles(console, config_dir / "ctail.toml")
            if not options.show_rules:
                return
        config_path = config_dir / "ctail.toml"
        active_profile = (
            options.profile
            if options.profile is not None
            else _read_active_profile(config_path)
        )
        show_unmatched, rules, rules_path = _load_settings(config_dir, active_profile)
        if options.profile is not None and not options.show_rules:
            _write_active_profile(config_path, active_profile)
        if options.show_rules:
            _show_rules(console, rules, active_profile)
            return
        if options.less:
            _show_with_less(tail_arguments, console, rules, show_unmatched)
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
                "Enter: blank line  Space: pause/resume  p: profile  c: add rule  r: remove rule  "
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
            active_profile,
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
