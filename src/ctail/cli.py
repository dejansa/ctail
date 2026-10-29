from __future__ import annotations

import argparse
import glob
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
from importlib.metadata import version
from pathlib import Path
from typing import Any, BinaryIO, TextIO

import tomlkit
from rich.color import ANSI_COLOR_NAMES, Color, ColorParseError
from rich.cells import cell_len
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
    shared: bool = False


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
    filenames = ("ctail.toml",)
    if all((installed_config_dir / name).is_file() for name in filenames):
        return installed_config_dir

    source_config_dir = Path(__file__).resolve().parents[2] / "config" / "ctail"
    if all((source_config_dir / name).is_file() for name in filenames):
        return source_config_dir

    raise FileNotFoundError("Could not locate packaged ctail configuration examples")


def _initialize_config(config_dir: Path) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)

    example_dir = _example_config_directory()
    if (config_dir / "ctail.toml").exists():
        return
    for name in ("default.rules", "http-access.rules", "ctail.toml"):
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
        shared = item.get("shared", False)
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
                "pattern in a rules file"
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
        if not isinstance(shared, bool):
            raise TypeError(f"{source}: rule {index} 'shared' must be true or false")
        if shared and (profile_name is not None or builtin):
            raise ValueError(f"{source}: rule {index}: only top-level custom rules can be shared")
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
            name=name, group_colors=group_colors, profile_name=profile_name, shared=shared,
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
    for path in sorted(source.parent.glob("*.rules")):
        if path.name == "default.rules":
            continue
        name = path.stem
        if name in parsed:
            raise ValueError(f"{path}: profile {name!r} is also defined in {source}")
        file_data = _read_toml(path)
        if set(file_data) - {"rules"}:
            raise ValueError(f"{path}: rules files may only contain 'rules'")
        parsed[name] = _parse_rules(
            file_data.get("rules", []), path, allow_builtins=True, profile_name=name
        )
    return parsed


@dataclass(frozen=True)
class ProfilePath:
    profile: str
    pattern: str
    directory: bool


def _parse_profile_paths(
    data: dict[str, Any], source: Path, profiles: dict[str, list[ColorRule]]
) -> list[ProfilePath]:
    entries = data.get("profile_paths", [])
    if not isinstance(entries, list):
        raise TypeError(f"{source}: 'profile_paths' must be an array of tables")
    mappings: list[ProfilePath] = []
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            raise TypeError(f"{source}: profile_paths {index} must be a table")
        profile = entry.get("profile")
        if not isinstance(profile, str) or not profile:
            raise ValueError(f"{source}: profile_paths {index} needs a non-empty profile")
        if profile not in profiles:
            raise ValueError(f"{source}: profile_paths {index} references unknown profile {profile!r}")
        paths = entry.get("paths")
        if not isinstance(paths, list) or not paths:
            raise ValueError(f"{source}: profile_paths {index} needs a non-empty paths array")
        for pattern in paths:
            if not isinstance(pattern, str) or not pattern or "\0" in pattern:
                raise ValueError(f"{source}: profile_paths {index} paths must be non-empty strings")
            expanded = os.path.expanduser(pattern)
            if not os.path.isabs(expanded):
                raise ValueError(f"{source}: profile_paths {index} path must be absolute: {pattern!r}")
            normalized = os.path.normpath(expanded)
            try:
                Path("/").full_match(normalized, case_sensitive=True)
            except ValueError as error:
                raise ValueError(
                    f"{source}: profile_paths {index} invalid pattern {pattern!r}: {error}"
                ) from error
            mappings.append(ProfilePath(profile, normalized, pattern.endswith("/")))
    return mappings


def _matching_profile(filename: str, mappings: list[ProfilePath]) -> str | None:
    path = Path(os.path.abspath(filename))
    for mapping in mappings:
        candidate = path.parent if mapping.directory else path
        if candidate.full_match(mapping.pattern, case_sensitive=True):
            return mapping.profile
    return None


def _single_file_argument(arguments: list[str], *, less: bool = False) -> str | None:
    if less:
        files = arguments[1:] if arguments[:1] == ["--"] else arguments
        return files[0] if len(files) == 1 and not files[0].startswith("-") else None

    files: list[str] = []
    options = True
    index = 0
    no_value = {"-f", "-F", "-q", "-v", "-z", "--retry", "--quiet", "--silent",
                "--verbose", "--zero-terminated"}
    value_options = {"-n", "-c", "-s", "--lines", "--bytes", "--sleep-interval",
                     "--pid", "--max-unchanged-stats"}
    while index < len(arguments):
        arg = arguments[index]
        if options and arg == "--":
            options = False
        elif options and arg in no_value:
            pass
        elif options and arg in value_options:
            index += 1
            if index >= len(arguments):
                return None
        elif options and arg in {"--follow"}:
            pass
        elif options and (
            any(arg.startswith(name + "=") for name in value_options if name.startswith("--"))
            or arg.startswith("--follow=")
            or any(arg.startswith(name) and len(arg) > len(name) for name in ("-n", "-c", "-s"))
        ):
            pass
        elif options and arg.startswith("-"):
            return None
        else:
            files.append(arg)
        index += 1
    return files[0] if len(files) == 1 and files[0] != "-" else None


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
        _parse_profile_paths(config, config_path, configured_profiles)

    default_path = config_dir / "default.rules"
    if default_path.exists():
        if configured_rules:
            raise ValueError(f"{config_path}: default rules are also defined in {default_path}")
        data = _read_toml(default_path)
        if set(data) - {"rules"}:
            raise ValueError(f"{default_path}: rules files may only contain 'rules'")
        configured_rules = _parse_rules(data.get("rules", []), default_path, allow_builtins=True)

    shared_rules = [rule for rule in configured_rules if rule.shared]
    configured_rules = [rule for rule in configured_rules if not rule.shared]
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
    return (
        show_unmatched,
        shared_rules + configured_rules + defaults,
        config_path,
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
        allow_builtins=path.name == "ctail.toml" or path.suffix == ".rules",
        profile_name=profile_name if path.suffix == ".rules" else None,
    )
    if path.suffix == ".rules":
        if set(data) - {"rules"}:
            raise ValueError(f"{path}: rules files may only contain 'rules'")
        return document, _rule_entries(document, path, None)
    _parse_rule_profiles(data, path, allow_builtins=path.name == "ctail.toml")
    return document, _rule_entries(document, path, profile_name, create_profile=create_profile)


def _rule_entries(
    document: TOMLDocument,
    path: Path,
    profile_name: str | None,
    *,
    create_profile: bool = False,
) -> AoT | Array:
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
    return entries


def _save_rule_document(path: Path, document: TOMLDocument) -> None:
    _parse_rules(
        document.unwrap().get("rules", []), path,
        allow_builtins=path.name == "ctail.toml" or path.suffix == ".rules",
        profile_name=path.stem if path.suffix == ".rules" and path.stem != "default" else None,
    )
    if path.suffix == ".rules" and set(document.unwrap()) - {"rules"}:
        raise ValueError(f"{path}: rules files may only contain 'rules'")
    profiles = (
        _parse_rule_profiles(document.unwrap(), path, allow_builtins=True)
        if path.name == "ctail.toml" else {}
    )
    if path.name == "ctail.toml":
        _parse_profile_paths(document.unwrap(), path, profiles)
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
    if (not profile_name or profile_name in {".", "..", "default"}
            or "/" in profile_name or "\\" in profile_name or "\0" in profile_name):
        raise ValueError("invalid or reserved profile name")
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

    if _uses_rule_files(path):
        profile_path = path.with_name(f"{profile_name}.rules")
        with profile_path.open("x", encoding="utf-8"):
            pass
        return

    if "profiles" not in document:
        document["profiles"] = tomlkit.table()
    document["profiles"][profile_name] = tomlkit.table()
    _save_rule_document(path, document)


def _uses_rule_files(config_path: Path) -> bool:
    if any(config_path.parent.glob("*.rules")):
        return True
    data = _read_toml(config_path) if config_path.exists() else {}
    return "rules" not in data and "profiles" not in data


def _scope_path(config_path: Path, profile_name: str | None) -> Path:
    config = _read_toml(config_path) if config_path.exists() else {}
    if profile_name is None and "rules" in config:
        return config_path
    if profile_name is not None and profile_name in config.get("profiles", {}):
        return config_path
    if not _uses_rule_files(config_path):
        return config_path
    return config_path.with_name(f"{profile_name or 'default'}.rules")


def _write_profile_path(path: Path, profile_name: str, pattern: str) -> None:
    try:
        document = tomlkit.parse(path.read_text(encoding="utf-8"))
    except ParseError as error:
        raise ValueError(f"{path}: invalid TOML: {error}") from error
    data = document.unwrap()
    profiles = _parse_rule_profiles(data, path, allow_builtins=True)
    _parse_profile_paths(data, path, profiles)
    if "profile_paths" not in document:
        document["profile_paths"] = tomlkit.aot()
    entries = document["profile_paths"]
    if not isinstance(entries, (AoT, Array)):
        raise TypeError(f"{path}: 'profile_paths' must be an array of tables")
    entry = tomlkit.inline_table() if isinstance(entries, Array) else tomlkit.table()
    entry.update({"profile": profile_name, "paths": [pattern]})
    entries.insert(0, entry)
    _save_rule_document(path, document)


def _write_ad_hoc_rule(
    path: Path,
    pattern: str,
    color: str,
    profile_name: str | None = None,
    *,
    regex_enabled: bool = False,
    shared: bool = True,
) -> None:
    path = _scope_path(path, profile_name)
    document, entries = _read_rule_document(
        path, profile_name, create_profile=profile_name is not None
    )
    entry = tomlkit.inline_table() if isinstance(entries, Array) else tomlkit.table()
    entry.update({"pattern": pattern, "color": color, "regex": regex_enabled, "disabled": False})
    if profile_name is None and shared:
        entry["shared"] = True
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
    scope = profile if rule.source == "built-in" and rule.source_index is None else rule.profile_name
    path = _scope_path(rules_path.with_name("ctail.toml"), scope) if rule.source == "built-in" else Path(rule.source)
    document, entries = _read_rule_document(path, scope)
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


def _edit_rule(
    rule: ColorRule,
    rules_path: Path,
    pattern: str,
    color: str,
    *,
    profile: str | None = None,
    regex_enabled: bool | None = None,
) -> None:
    _, current_rules, _ = _load_settings(rules_path.parent, profile)
    if rule not in current_rules:
        raise ValueError("Rules changed on disk; restart ctail before editing this rule.")
    if rule.source == "built-in" and pattern != rule.pattern:
        raise ValueError("Built-in patterns cannot be changed.")
    if rule.source == "built-in" and regex_enabled is False:
        raise ValueError("Built-in rules must use regex.")
    scope = profile if rule.source == "built-in" and rule.source_index is None else rule.profile_name
    path = _scope_path(rules_path.with_name("ctail.toml"), scope) if rule.source == "built-in" else Path(rule.source)
    document, entries = _read_rule_document(path, scope)
    if rule.source_index is None:
        if rule.source != "built-in":
            raise ValueError("Rule has no saved TOML entry.")
        entry = tomlkit.inline_table() if isinstance(entries, Array) else tomlkit.table()
        entry.update({"builtin": True, "pattern": rule.pattern, "color": color})
        entries.append(entry)
    else:
        entry = entries[rule.source_index]
        if rule.source != "built-in":
            entry["pattern"] = pattern
            if regex_enabled is not None and regex_enabled != rule.regex_enabled:
                entry["regex"] = regex_enabled
        entry["color"] = color
    _save_rule_document(path, document)


def _rename_rule(
    rule: ColorRule,
    rules_path: Path,
    name: str,
    *,
    profile: str | None = None,
) -> None:
    if not name.strip():
        raise ValueError("Rule name must not be empty.")
    _, current_rules, _ = _load_settings(rules_path.parent, profile)
    if rule not in current_rules:
        raise ValueError("Rules changed on disk; restart ctail before editing this rule.")
    scope = profile if rule.source == "built-in" and rule.source_index is None else rule.profile_name
    path = _scope_path(rules_path.with_name("ctail.toml"), scope) if rule.source == "built-in" else Path(rule.source)
    document, entries = _read_rule_document(path, scope)
    if rule.source_index is None:
        if rule.source != "built-in":
            raise ValueError("Rule has no saved TOML entry.")
        entry = tomlkit.inline_table() if isinstance(entries, Array) else tomlkit.table()
        entry.update({"builtin": True, "pattern": rule.pattern, "name": name})
        entries.append(entry)
    else:
        entries[rule.source_index]["name"] = name
    _save_rule_document(path, document)


def _copy_ad_hoc_rule(
    rule: ColorRule,
    rules_path: Path,
    destination: str,
    *,
    profile: str | None = None,
) -> bool:
    if rule.source == "built-in":
        raise ValueError("Only custom rules can be copied.")
    if rule.source_index is None:
        raise ValueError("Rule has no saved TOML entry.")
    _, current_rules, _ = _load_settings(rules_path.parent, profile)
    if rule not in current_rules:
        raise ValueError("Rules changed on disk; restart ctail before copying this rule.")
    config_path = rules_path.with_name("ctail.toml")
    config = _read_toml(config_path) if config_path.exists() else {}
    profiles = _parse_rule_profiles(config, config_path, allow_builtins=True)
    if destination not in profiles:
        raise ValueError(f"Profile {destination!r} no longer exists.")
    if rule.profile_name == destination:
        return False

    source_path = Path(rule.source)
    target_path = _scope_path(config_path, destination)
    document, source = _read_rule_document(source_path, rule.profile_name)
    if source_path == target_path:
        target_document = document
        target = _rule_entries(document, source_path, destination)
    else:
        target_document, target = _read_rule_document(target_path, destination)
    source_entry = source[rule.source_index].unwrap()
    source_entry.pop("shared", None)
    if any(
        existing.unwrap() == source_entry
        for existing in target
    ):
        return False

    if isinstance(target, AoT):
        entry = tomlkit.table()
    else:
        entry = tomlkit.inline_table()
    entry.update(source_entry)
    target.insert(0, entry)
    _save_rule_document(target_path, target_document)
    return True


def _add_rule(
    console: Console,
    rules: list[ColorRule],
    rules_path: Path,
    keyboard_fd: int,
    original_terminal: list[Any],
    profile: str | None = None,
) -> None:
    del original_terminal
    with Live(
        console=console, screen=True, transient=True, auto_refresh=False,
        redirect_stdout=False, redirect_stderr=False,
    ) as live:
        message = _rule_editor(console, live, rules, rules_path, keyboard_fd, profile)
    console.print(message, style="bold red" if "could not reload" in message else "dim")


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
        if sequence in (b"[C", b"OC"):
            return "right"
        if sequence in (b"[D", b"OD"):
            return "left"
        return "cancel"
    if key in (b"\r", b"\n"):
        return "enter"
    if key in (b"\x7f", b"\x08"):
        return "backspace"
    if not key:
        return "cancel"
    first = key[0]
    length = 4 if 0xF0 <= first <= 0xF4 else 3 if 0xE0 <= first <= 0xEF else 2 if 0xC2 <= first <= 0xDF else 1
    while len(key) < length:
        part = os.read(keyboard_fd, 1)
        if not part:
            break
        key += part
    return key.decode("utf-8", errors="replace")


_BASIC_EDITOR_COLORS = ("none", "black", "white", "red", "green", "blue", "yellow", "magenta", "cyan")
_EDITOR_COLORS = ("none", *sorted({"default", *ANSI_COLOR_NAMES}))
_ADD_EDITOR_COLORS = (*_BASIC_EDITOR_COLORS, *(
    color for color in _EDITOR_COLORS if color not in _BASIC_EDITOR_COLORS
))
_EDITOR_COLUMN_WIDTH = max(cell_len(name) for name in _EDITOR_COLORS) + 1
_EDITOR_PATTERN_WIDTH = 55
_ADD_EDITOR_FIELDS = ("Pattern", "Foreground", "Background", "Mode")
_REGEX_METACHARACTERS = frozenset(r".^$*+?{}[]\|()")
_EDITOR_FIELDS = (
    "Pattern", "Foreground", "Background", "Bold", "Inverse", "Scope", "Save",
    "Cancel", "Mode", "Style",
)


@dataclass
class _RuleEditorState:
    pattern: str
    color: str
    scope: str | None
    rule: ColorRule | None
    field: int = 0
    editing: bool = False
    text: str = ""
    cursor: int = 0
    message: str = ""
    regex_enabled: bool = False


def _editor_style(color: str) -> Style | None:
    if len(_split_colors(color)) != 1:
        return None
    try:
        style = Style.parse(color)
    except (ColorParseError, StyleSyntaxError):
        return None
    simple = Style(color=style.color, bgcolor=style.bgcolor, bold=style.bold, reverse=style.reverse)
    return style if style == simple else None


def _editor_color_choices(color: str | None, *, adding: bool = False) -> list[str]:
    choices = list(_ADD_EDITOR_COLORS if adding else _EDITOR_COLORS)
    if color is not None and color not in choices:
        choices.insert(1, color)
    return choices


def _editor_change_style(
    state: _RuleEditorState, field: str, step: int, *, adding: bool = False
) -> None:
    style = _editor_style(state.color)
    if style is None:
        state.message = "Complex style: edit Style directly before using color controls."
        return
    foreground = style.color.name if style.color else None
    background = style.bgcolor.name if style.bgcolor else None
    bold, reverse = style.bold, style.reverse
    if field in ("Foreground", "Background"):
        current = foreground if field == "Foreground" else background
        choices = _editor_color_choices(current, adding=adding)
        index = choices.index(current or "none")
        name = choices[max(0, min(len(choices) - 1, index + step))]
        if field == "Foreground":
            foreground = None if name == "none" else name
        else:
            background = None if name == "none" else name
    elif field == "Bold":
        bold = None if bold else True
    else:
        reverse = None if reverse else True
    state.color = str(Style(color=foreground, bgcolor=background, bold=bold, reverse=reverse))


def _editor_text_window(text: str, cursor: int, width: int) -> str:
    value = text[:cursor] + "|" + text[cursor:]
    start = 0
    while cell_len(value[start:cursor + 1]) > max(1, width - 2):
        start += 1
    end = len(value)
    while end > cursor + 1 and cell_len(value[start:end]) > width - (start > 0) - 1:
        end -= 1
    return ("<" if start else "") + value[start:end] + (">" if end < len(value) else "")


def _render_editor_table(
    console: Console, state: _RuleEditorState, *, adding: bool, rows: int = 5
) -> Table:
    field = (_ADD_EDITOR_FIELDS if adding else _EDITOR_FIELDS)[state.field]
    style = _editor_style(state.color)
    foreground = style.color.name if style and style.color else "none"
    background = style.bgcolor.name if style and style.bgcolor else "none"
    table = Table(title="Edit rule" if state.rule else "Add rule", box=None, padding=(0, 0))
    available_width = max(3, console.width)
    min_color_width = min(10, max(1, (available_width - min(22, available_width // 2)) // 2))
    pattern_column_width = min(_EDITOR_PATTERN_WIDTH, available_width - 2 * min_color_width)
    color_width = min(_EDITOR_COLUMN_WIDTH, (available_width - pattern_column_width) // 2)
    pattern_width = pattern_column_width - 1
    for heading, width in (
        ("Pattern", pattern_column_width),
        ("Foreground", color_width),
        ("Background", color_width),
    ):
        table.add_column(heading, width=width, overflow="ellipsis", no_wrap=True)
    height = rows
    center = height // 2
    fg_choices = _editor_color_choices(None if foreground == "none" else foreground, adding=adding)
    bg_choices = _editor_color_choices(None if background == "none" else background, adding=adding)
    fg_index, bg_index = fg_choices.index(foreground), bg_choices.index(background)
    for row in range(height):
        offset = row - center
        cells = []
        pattern = state.pattern
        if state.editing and field == "Pattern":
            pattern = _editor_text_window(state.text, state.cursor, pattern_width)
        elif field == "Pattern":
            pattern = _editor_text_window(state.pattern, len(state.pattern), pattern_width)
        cell = Text(">" if field == "Pattern" and offset == 0 else " ", style="bold")
        cell.append(pattern if offset == 0 else "")
        cells.append(cell)
        for choices, index, active, is_background in (
            (fg_choices, fg_index, field == "Foreground", False),
            (bg_choices, bg_index, field == "Background", True),
        ):
            start = max(0, min(index - center, len(choices) - height))
            position = start + row if adding else index + offset
            name = choices[position] if 0 <= position < len(choices) else ""
            selected = position == index if adding else offset == 0
            cell = Text(">" if active and selected else " ", style="bold")
            sample_style = None
            if name and name != "none":
                if is_background:
                    red, green, blue = Color.parse(name).get_truecolor(foreground=False)
                    contrast = "black" if red * 299 + green * 587 + blue * 114 >= 128000 else "white"
                    sample_style = f"{contrast} on {name}"
                elif name != "default":
                    red, green, blue = Color.parse(name).get_truecolor()
                    contrast = "black" if red * 299 + green * 587 + blue * 114 >= 128000 else "white"
                    sample_style = f"{name} on {contrast}"
                else:
                    sample_style = name
            cell.append(name, style=sample_style)
            cells.append(cell)
        table.add_row(*cells)
    return table


def _fit_editor_view(
    console: Console, state: _RuleEditorState, *, adding: bool,
    controls: list[Text], mode: Text, style: Text, preview: Text,
    instructions: tuple[str, ...], message: str,
) -> Group:
    rows = 5
    title = True
    show_table = True
    help_lines = list(instructions)

    def render() -> Group:
        table = _render_editor_table(console, state, adding=adding, rows=rows)
        if not title:
            table.title = None
        return Group(
            *([table] if show_table else []), *controls, mode, style, preview, Text(""),
            *(Text(line, style="dim") for line in help_lines),
            *([Text(message, style="yellow")] if message else []),
        )

    def fits(view: Group) -> bool:
        return len(console.render_lines(view, options=console.options, pad=False)) <= console.size.height

    view = render()
    while rows > 1 and not fits(view):
        rows -= 1
        view = render()
    if not adding and console.size.height <= 8 and not fits(view):
        title = False
        view = render()
    while help_lines and not fits(view):
        help_lines.pop()
        view = render()
    if message and not fits(view):
        show_table = False
        view = render()
    return view


def _render_rule_editor(console: Console, state: _RuleEditorState, profile: str | None) -> Group:
    field = _EDITOR_FIELDS[state.field]
    style = _editor_style(state.color)
    compact = console.size.height < 20
    scope = state.scope or "All profiles (shared)"
    if state.rule is not None:
        scope = state.rule.profile_name or ("shared" if state.rule.shared else "default")
    values = {
        "Bold": ("on" if style.bold else "off") if style is not None else "-",
        "Inverse": ("on" if style.reverse else "off") if style is not None else "-",
        "Style": (
            _editor_text_window(state.text, state.cursor, max(10, console.width // 3))
            if state.editing and field == "Style" else state.color
        ),
        "Scope": scope,
        "Mode": "regex" if state.regex_enabled else "literal",
        "Save": "",
        "Cancel": "",
    }
    def control(name: str) -> Text:
        result = Text(">" if field == name else " ", style="bold")
        label = f"{name}: {values[name]}" if values[name] else name
        result.append(label, style="reverse" if field == name else None)
        return result

    controls = [control(name) for name in ("Bold", "Inverse", "Scope", "Save", "Cancel")]
    try:
        preview_style = _split_colors(state.color)[0]
        Style.parse(preview_style)
        preview = Text(state.pattern or "Preview", style=preview_style, no_wrap=True)
    except (ColorParseError, StyleSyntaxError):
        preview = Text("Invalid style", style="bold red")
    instructions = (
        ("Editing text", "Left/Right: move", "Backspace: delete", "Enter: accept", "Esc: cancel")
        if state.editing else (
            "Type pattern; Enter: edit/select; Esc: cancel",
            "Columns: Left/Right; colors: Up/Down",
            "Options: Up/Down; values: Left/Right",
            "From Pattern press Down, or from Background press Right, to select Bold",
            "From Bold press Up to return to Background",
        )
    )
    notice = "Complex style: use Style to edit it." if style is None else ""
    if state.rule and state.rule.source == "built-in":
        notice = "Built-in pattern and regex mode are read-only. " + notice
    message = state.message or notice
    if compact:
        compact_controls = Text()
        for item in controls:
            if compact_controls:
                compact_controls.append("  ")
            compact_controls.append_text(item)
        compact_instructions = (
            ("Editing: Left/Right move; Backspace deletes; Enter accepts; Esc cancels", "")
            if state.editing else (
                "L/R: columns or values; U/D: colors or options; Enter: select; Esc: cancel",
                "Pattern + Down or Background + Right: select Bold; Bold + Up: Background",
            )
        )
        controls = [compact_controls]
        instructions = compact_instructions
    else:
        controls = [Text(""), *controls]
    return _fit_editor_view(
        console, state, adding=False, controls=controls,
        mode=control("Mode"), style=control("Style"), preview=Text("Preview: ") + preview,
        instructions=instructions, message=message,
    )


def _render_add_rule_editor(console: Console, state: _RuleEditorState) -> Group:
    field = _ADD_EDITOR_FIELDS[state.field]
    compact = console.size.height < 12
    mode = Text(">" if field == "Mode" else " ", style="bold")
    mode.append(f"Mode: {'regex' if state.regex_enabled else 'literal'}",
                style="reverse" if field == "Mode" else None)
    style = Text(f" Style: {state.color}")
    preview = Text(state.pattern or "Preview", style=state.color, no_wrap=True)
    if state.editing and compact:
        instructions = (
            "Editing: Left/Right move; Backspace deletes",
            "Enter: accept; Esc: discard text",
        )
    elif state.editing:
        instructions = (
            "Editing pattern: Left/Right move; Backspace deletes",
            "Enter: accept text; Esc: discard text",
        )
    elif compact:
        instructions = (
            "Pattern Down: mode; Esc: back; arrows move",
            "FG b:bold BG i:inverse Enter:toggle/save",
        )
    else:
        instructions = (
            "Arrows: fields/colors; Pattern Down: mode; Mode Enter: toggle; Up: colors",
            "Foreground b: bold; Background i: inverse; Color Enter: save; Esc: discard",
        )
    if compact and state.message:
        instructions = ()
    return _fit_editor_view(
        console, state, adding=True, controls=[], mode=mode, style=style,
        preview=Text("Preview: ") + preview, instructions=instructions, message=state.message,
    )


def _save_editor_rule(
    state: _RuleEditorState, rules: list[ColorRule], rules_path: Path, profile: str | None
) -> str | None:
    rule = state.rule
    try:
        _load_settings(rules_path.parent, profile)
        if rule is None:
            _write_ad_hoc_rule(
                rules_path, state.pattern, state.color, profile,
                regex_enabled=state.regex_enabled, shared=False,
            )
        else:
            _edit_rule(
                rule, rules_path, state.pattern, state.color,
                profile=profile, regex_enabled=state.regex_enabled,
            )
    except (OSError, TypeError, ValueError) as error:
        state.message = f"Could not save rule: {error}"
        return None
    try:
        _, loaded_rules, _ = _load_settings(rules_path.parent, profile)
    except (OSError, TypeError, ValueError) as error:
        return f"Rule saved but could not reload: {error}"
    rules[:] = loaded_rules
    return "Edited rule." if rule else "Added rule."


def _add_rule_editor(
    console: Console, live: Live, rules: list[ColorRule], rules_path: Path,
    keyboard_fd: int, profile: str | None,
) -> str:
    state = _RuleEditorState("", "none", profile, None)
    original_mode = False
    while True:
        live.update(_render_add_rule_editor(console, state), refresh=True)
        key = _read_rule_key(keyboard_fd)
        field = _ADD_EDITOR_FIELDS[state.field]
        if state.editing:
            if key == "cancel":
                state.regex_enabled = original_mode
                state.editing = False
            elif key == "enter":
                state.pattern = state.text
                state.editing = False
            elif key == "left":
                state.cursor = max(0, state.cursor - 1)
            elif key == "right":
                state.cursor = min(len(state.text), state.cursor + 1)
            elif key == "backspace" and state.cursor:
                state.text = state.text[:state.cursor - 1] + state.text[state.cursor:]
                state.cursor -= 1
            elif len(key) == 1 and key.isprintable():
                state.text = state.text[:state.cursor] + key + state.text[state.cursor:]
                state.cursor += 1
                if key in _REGEX_METACHARACTERS:
                    state.regex_enabled = True
            continue
        state.message = ""
        if key == "cancel":
            return "Canceled."
        if field == "Pattern":
            if key == "backspace":
                state.pattern = state.pattern[:-1]
            elif len(key) == 1 and key.isprintable():
                state.pattern += key
                if key in _REGEX_METACHARACTERS:
                    state.regex_enabled = True
            elif key == "enter":
                state.text, state.cursor = state.pattern, len(state.pattern)
                original_mode = state.regex_enabled
                state.editing = True
            elif key == "down":
                state.field = 3
            elif key == "right":
                state.field = 1
        elif field in ("Foreground", "Background"):
            if key in ("up", "down"):
                _editor_change_style(state, field, -1 if key == "up" else 1, adding=True)
            elif key in ("left", "right"):
                state.field = max(0, min(3, state.field + (-1 if key == "left" else 1)))
            elif key == "b" and field == "Foreground":
                _editor_change_style(state, "Bold", 1)
            elif key == "i" and field == "Background":
                _editor_change_style(state, "Inverse", 1)
            elif key == "enter":
                result = _save_editor_rule(state, rules, rules_path, profile)
                if result is not None:
                    return result
        elif field == "Mode":
            if key in ("enter", "left", "right"):
                state.regex_enabled = not state.regex_enabled
            elif key == "up":
                state.field = 1


def _rule_editor(
    console: Console,
    live: Live,
    rules: list[ColorRule],
    rules_path: Path,
    keyboard_fd: int,
    profile: str | None,
    rule: ColorRule | None = None,
) -> str:
    if rule is None:
        return _add_rule_editor(console, live, rules, rules_path, keyboard_fd, profile)
    state = _RuleEditorState(
        rule.pattern if rule else "", rule.color if rule else "yellow",
        rule.profile_name if rule else profile, rule,
        regex_enabled=rule.regex_enabled if rule else False,
    )
    while True:
        live.update(_render_rule_editor(console, state, profile), refresh=True)
        key = _read_rule_key(keyboard_fd)
        field = _EDITOR_FIELDS[state.field]
        if state.editing:
            if key == "cancel":
                state.editing = False
            elif key == "enter":
                if field == "Pattern":
                    state.pattern = state.text
                else:
                    state.color = state.text
                state.editing = False
            elif key == "left":
                state.cursor = max(0, state.cursor - 1)
            elif key == "right":
                state.cursor = min(len(state.text), state.cursor + 1)
            elif key == "backspace" and state.cursor:
                state.text = state.text[:state.cursor - 1] + state.text[state.cursor:]
                state.cursor -= 1
            elif len(key) == 1 and key.isprintable():
                state.text = state.text[:state.cursor] + key + state.text[state.cursor:]
                state.cursor += 1
            continue
        state.message = ""
        if key == "cancel":
            return "Canceled."
        if field == "Pattern" and (key == "backspace" or (len(key) == 1 and key.isprintable())):
            if rule is not None and rule.source == "built-in":
                state.message = "Built-in patterns cannot be changed."
            elif key == "backspace":
                state.pattern = state.pattern[:-1]
            else:
                state.pattern += key
            continue
        if key in ("left", "right"):
            if state.field <= 2:
                state.field = max(0, min(3, state.field + (-1 if key == "left" else 1)))
            elif field in ("Bold", "Inverse"):
                _editor_change_style(state, field, -1 if key == "left" else 1)
            elif field == "Scope" and rule is None and profile is not None:
                state.scope = None if state.scope is not None else profile
            elif field == "Mode":
                if rule is not None and rule.source == "built-in":
                    state.message = "Built-in rules must use regex."
                else:
                    state.regex_enabled = not state.regex_enabled
        elif key in ("up", "down"):
            if field in ("Foreground", "Background"):
                _editor_change_style(state, field, -1 if key == "up" else 1)
            elif field == "Pattern" and key == "down":
                state.field = 3
            elif state.field >= 3:
                state.field = max(2, min(len(_EDITOR_FIELDS) - 1, state.field + (-1 if key == "up" else 1)))
        elif key == "enter":
            if field == "Cancel":
                return "Canceled."
            if field in ("Pattern", "Style"):
                if field == "Pattern" and rule is not None and rule.source == "built-in":
                    state.message = "Built-in patterns cannot be changed."
                    continue
                state.text = state.pattern if field == "Pattern" else state.color
                state.cursor = len(state.text)
                state.editing = True
            elif field in ("Bold", "Inverse"):
                _editor_change_style(state, field, 1)
            elif field == "Scope" and rule is None and profile is not None:
                state.scope = None if state.scope is not None else profile
            elif field == "Mode":
                if rule is not None and rule.source == "built-in":
                    state.message = "Built-in rules must use regex."
                else:
                    state.regex_enabled = not state.regex_enabled
            elif field == "Save":
                result = _save_editor_rule(state, rules, rules_path, profile)
                if result is not None:
                    return result


def _select_profile(
    console: Console,
    config_path: Path,
    keyboard_fd: int,
    active_profile: str | None = None,
    source_file: str | None = None,
) -> tuple[bool | str, str | None] | None:
    config = _read_toml(config_path) if config_path.exists() else {}
    profiles = _parse_rule_profiles(config, config_path, allow_builtins=True)
    _parse_profile_paths(config, config_path, profiles)
    choices: list[str | None] = [None, *sorted(profiles)]
    paths_by_profile: dict[str, list[str]] = {name: [] for name in profiles}
    for mapping in config.get("profile_paths", []):
        paths_by_profile[mapping["profile"]].extend(mapping["paths"])
    selected = choices.index(active_profile) if active_profile in choices else 0
    message = ""

    def render() -> Group:
        def make_table(start: int, end: int) -> Table:
            table = Table(title="Select profile", box=None)
            table.add_column("Profile")
            table.add_column("Associated paths")
            for index in range(start, end):
                profile = choices[index]
                name = Text("(default)" if profile is None else profile)
                if profile == active_profile:
                    name.append(" (active)", style="bold green")
                paths = Text("\n".join(paths_by_profile[profile]) if profile else "-")
                if not paths:
                    paths.append("-")
                if index == selected:
                    name.stylize("reverse")
                    paths.stylize("reverse")
                table.add_row(name, paths)
            return table

        start, end = selected, selected + 1
        max_table_height = max(1, console.size.height - 4)
        while start > 0:
            candidate = make_table(start - 1, end)
            if len(console.render_lines(candidate, pad=False)) > max_table_height:
                break
            start -= 1
        while end < len(choices):
            candidate = make_table(start, end + 1)
            if len(console.render_lines(candidate, pad=False)) > max_table_height:
                break
            end += 1
        table = make_table(start, end)
        actions = "n: Add a new profile"
        if source_file is not None:
            actions += "\na: Associate selected profile with current file or directory"
        return Group(
            table,
            Text(""),
            Text(actions),
            Text(message, style="yellow"),
            Text("Up/Down: select", style="dim"),
            Text("Enter: apply", style="dim"),
            Text("Esc: cancel", style="dim"),
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
            if key in ("a", "A") and source_file is not None:
                if choices[selected] is not None:
                    return "associate", choices[selected]
                message = "Select a named profile to create an association."
            if key == "up":
                selected = max(0, selected - 1)
            elif key == "down":
                selected = min(len(choices) - 1, selected + 1)
            live.update(render(), refresh=True)


def _associate_profile(
    console: Console, config_path: Path, keyboard_fd: int, filename: str, profile: str
) -> None:
    console.print("Associate profile with: f: this file  d: its directory  Escape: cancel")
    choice = _read_rule_key(keyboard_fd).lower()
    if choice not in ("f", "d"):
        console.print("Association canceled.", style="dim")
        return
    absolute = os.path.abspath(filename)
    pattern = glob.escape(absolute if choice == "f" else os.path.dirname(absolute))
    if choice == "d":
        pattern = pattern.rstrip("/") + "/"
    console.print(Text(
        f"Associate profile {profile!r} with pattern {pattern!r}? "
        "This takes priority over existing mappings. y: confirm  any other key: cancel"
    ))
    if _read_rule_key(keyboard_fd).lower() != "y":
        console.print("Association canceled.", style="dim")
        return
    try:
        _write_profile_path(config_path, profile, pattern)
    except (OSError, TypeError, ValueError) as error:
        console.print(f"Could not save profile association: {error}", style="bold red")
        return
    console.print("Association saved; it will apply on the next launch.", style="dim")


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
    *,
    source_file: str | None = None,
    session_only: bool = False,
) -> str | None:
    config_path = rules_path.with_name("ctail.toml")
    while True:
        try:
            selection = _select_profile(
                console, config_path, keyboard_fd, profile, source_file
            )
        except (OSError, TypeError, ValueError) as error:
            console.print(f"Could not list profiles: {error}", style="bold red")
            return profile
        if selection is None:
            return profile

        add_new, selected_profile = selection
        if add_new == "associate":
            assert source_file is not None and selected_profile is not None
            _associate_profile(console, config_path, keyboard_fd, source_file, selected_profile)
            continue
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
            if not session_only:
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
            if not session_only:
                _write_active_profile(config_path, selected_profile)
        except (OSError, TypeError, ValueError) as error:
            console.print(f"Could not select profile: {error}", style="bold red")
            return profile
        rules[:] = loaded_rules
        label = selected_profile if selected_profile is not None else "(default)"
        console.print(f"Selected profile {label!r}.", style="dim")
        return selected_profile


def _render_rule_manager(
    console: Console, rules: list[ColorRule], selected: int, message: str
) -> Group:
    rules = [rule for rule in rules if rule.source != "built-in"]
    headings = ("", "Status", "Source", "Profile", "Type", "Name", "Pattern", "Style")
    rows = [
        (
            ">" if index == selected else " ",
            "disabled" if rule.disabled else "enabled",
            Path(rule.source).name,
            rule.profile_name or ("shared" if rule.shared else "-"),
            "regex" if rule.regex_enabled else "literal",
            rule.name or "-",
            repr(rule.pattern),
            rule.color,
        )
        for index, rule in enumerate(rules)
    ]
    widths = [
        max(cell_len(heading), *(cell_len(row[index]) for row in rows)) + 1
        if rows else cell_len(heading) + 1
        for index, heading in enumerate(headings)
    ]

    def make_table(start: int, end: int) -> Table:
        table = Table(title="Rules manager", box=None, padding=(0, 0, 0, 1))
        for heading, width in zip(headings, widths):
            flexible = heading in {"Source", "Profile", "Name", "Pattern", "Style"}
            table.add_column(
                heading.ljust(width),
                max_width=width, no_wrap=not flexible, overflow="fold",
            )
        for index in range(start, end):
            rule = rules[index]
            style = rule.group_colors[0] if rule.group_colors else rule.color
            cells = [Text(value, style=style if cell in (6, 7) else None)
                        for cell, value in enumerate(rows[index])]
            if index == selected:
                for cell in cells:
                    cell.stylize("reverse")
            table.add_row(*cells)
        return table

    position = f"{selected + 1}/{len(rules)}" if rules else "0/0"
    footer = [
        Text(""),
        Text("'Up/Down': select  'a': add  'e': edit  'd': enable/disable"),
        Text("'r': rename  'x': remove  'c': copy-to  'Esc': close"),
        Text(
            message or ("No custom rules; default coloring remains active." if not rules else ""),
            style="yellow" if message else "dim",
        ),
        Text(position, style="dim"),
    ]
    footer_height = sum(
        len(console.render_lines(item, options=console.options, pad=False))
        for item in footer
    )
    start = selected if rules else 0
    end = start + (1 if rules else 0)
    table = make_table(start, end)
    table_height = max(3, console.size.height - footer_height)

    def fits(candidate: Table) -> bool:
        return len(console.render_lines(candidate, options=console.options, pad=False)) <= table_height

    while start > 0:
        candidate = make_table(start - 1, end)
        if not fits(candidate):
            break
        start -= 1
        table = candidate
    while end < len(rules):
        candidate = make_table(start, end + 1)
        if not fits(candidate):
            break
        end += 1
        table = candidate
    return Group(table, *footer)


def _manage_rules(
    console: Console, rules: list[ColorRule], rules_path: Path,
    keyboard_fd: int, *, profile: str | None = None,
) -> None:
    selected = 0
    message = ""
    mode = "list"
    destinations: list[str] = []
    destination_index = 0
    rename_name = ""
    cursor = 0

    def listed_rules() -> list[ColorRule]:
        return [rule for rule in rules if rule.source != "built-in"]

    def render() -> Group:
        if mode == "rename":
            return Group(
                Text("Rename rule", style="bold"),
                Text("Name: " + _editor_text_window(rename_name, cursor, max(1, console.width - 6))),
                Text("Left/Right: move  Backspace: delete", style="dim"),
                Text("Enter: save  Esc: cancel", style="dim"),
                Text(message, style="yellow"),
            )
        if mode == "copy":
            table = Table(title="Copy rule to", box=None, show_header=False)
            table.add_column("Destination")
            notice = Text(message, style="bold yellow")
            notice_height = len(console.render_lines(notice, pad=False)) if message else 0
            page_size = max(1, console.size.height - 5 - notice_height)
            start = (destination_index // page_size) * page_size
            for index in range(start, min(start + page_size, len(destinations))):
                name = destinations[index]
                row = Text(f"{'> ' if index == destination_index else '  '}{name}")
                if index == destination_index:
                    row.stylize("reverse")
                table.add_row(row)
            return Group(
                *([notice] if message else []),
                table, Text("Up/Down: select  Enter: choose  Esc: cancel", style="dim"),
                Text(f"{destination_index + 1}/{len(destinations)}", style="dim"),
            )
        return _render_rule_manager(console, rules, selected, message)

    with Live(
        render(), console=console, screen=True, transient=True,
        auto_refresh=False, redirect_stdout=False, redirect_stderr=False,
    ) as live:
        while True:
            key = _read_rule_key(keyboard_fd)
            if key == "cancel":
                if mode == "list":
                    return
                mode, message = "list", "Canceled."
            elif mode == "rename":
                message = ""
                if key == "left":
                    cursor = max(0, cursor - 1)
                elif key == "right":
                    cursor = min(len(rename_name), cursor + 1)
                elif key == "backspace" and cursor:
                    rename_name = rename_name[:cursor - 1] + rename_name[cursor:]
                    cursor -= 1
                elif key == "enter":
                    try:
                        _rename_rule(
                            listed_rules()[selected], rules_path, rename_name, profile=profile
                        )
                    except (OSError, TypeError, ValueError) as error:
                        message = f"Could not rename rule: {error}"
                    else:
                        try:
                            _, loaded_rules, _ = _load_settings(rules_path.parent, profile)
                        except (OSError, TypeError, ValueError) as error:
                            mode, message = "list", f"Rule saved but could not reload: {error}"
                        else:
                            rules[:] = loaded_rules
                            selected = min(selected, max(0, len(listed_rules()) - 1))
                            mode, message = "list", "Renamed rule."
                elif len(key) == 1 and key.isprintable():
                    rename_name = rename_name[:cursor] + key + rename_name[cursor:]
                    cursor += 1
            elif mode == "copy":
                if key == "up":
                    destination_index = max(0, destination_index - 1)
                    message = ""
                elif key == "down":
                    destination_index = min(len(destinations) - 1, destination_index + 1)
                    message = ""
                elif key == "enter":
                    destination = destinations[destination_index]
                    mode, message = (
                        "confirm_copy", f"Copy rule to {destination}? y: confirm  n: cancel"
                    )
            elif mode in ("confirm_delete", "confirm_copy"):
                if key.lower() == "n":
                    mode, message = "list", "Canceled."
                elif key.lower() == "y":
                    rule = listed_rules()[selected]
                    action = mode
                    copied = True
                    try:
                        if action == "confirm_delete":
                            _change_rule(rule, rules_path, delete=True, profile=profile)
                        else:
                            copied = _copy_ad_hoc_rule(
                                rule, rules_path, destinations[destination_index], profile=profile
                            )
                    except (OSError, TypeError, ValueError) as error:
                        mode, message = "list", f"Could not change color rule: {error}"
                    else:
                        if action == "confirm_copy" and not copied:
                            mode, message = "list", "An identical rule already exists there."
                            live.update(render(), refresh=True)
                            continue
                        try:
                            _, loaded_rules, _ = _load_settings(rules_path.parent, profile)
                        except (OSError, TypeError, ValueError) as error:
                            mode, message = "list", f"Rule saved but could not reload: {error}"
                            live.update(render(), refresh=True)
                            continue
                        rules[:] = loaded_rules
                        selected = min(selected, max(0, len(listed_rules()) - 1))
                        mode = "list"
                        message = "Deleted rule." if action == "confirm_delete" else "Copied rule."
            elif key == "up":
                selected = max(0, selected - 1)
            elif key == "down":
                selected = min(len(listed_rules()) - 1, selected + 1) if listed_rules() else 0
            elif key.lower() == "a":
                message = _rule_editor(console, live, rules, rules_path, keyboard_fd, profile)
                selected = 0
            elif listed_rules() and key.lower() == "e":
                message = _rule_editor(
                    console, live, rules, rules_path, keyboard_fd, profile, listed_rules()[selected]
                )
                selected = min(selected, max(0, len(listed_rules()) - 1))
            elif listed_rules() and key.lower() == "r":
                rename_name = listed_rules()[selected].name or ""
                cursor = len(rename_name)
                mode, message = "rename", ""
            elif listed_rules() and key.lower() == "d":
                rule = listed_rules()[selected]
                try:
                    _change_rule(rule, rules_path, delete=False, profile=profile)
                except (OSError, TypeError, ValueError) as error:
                    message = f"Could not change color rule: {error}"
                else:
                    try:
                        _, loaded_rules, _ = _load_settings(rules_path.parent, profile)
                    except (OSError, TypeError, ValueError) as error:
                        message = f"Rule saved but could not reload: {error}"
                    else:
                        rules[:] = loaded_rules
                        message = "Enabled rule." if rule.disabled else "Disabled rule."
            elif listed_rules() and key.lower() == "x":
                mode, message = "confirm_delete", "Delete selected rule? y: confirm  n: cancel"
            elif listed_rules() and key.lower() == "c":
                rule = listed_rules()[selected]
                try:
                    config_path = rules_path.with_name("ctail.toml")
                    config = _read_toml(config_path) if config_path.exists() else {}
                    profiles = _parse_rule_profiles(config, config_path, allow_builtins=True)
                except (OSError, TypeError, ValueError) as error:
                    message = f"Could not list profiles: {error}"
                else:
                    destinations = [
                        name for name in sorted(profiles)
                        if name != rule.profile_name
                    ]
                    if not destinations:
                        message = "No other profiles are available."
                    else:
                        destination_index = (
                            destinations.index(profile) if profile in destinations else 0
                        )
                        mode, message = "copy", ""
            live.update(render(), refresh=True)


def _show_interactive_help(console: Console, keyboard_fd: int) -> None:
    commands = Table(show_header=True, header_style="bold")
    commands.add_column("Key", style="cyan", no_wrap=True)
    commands.add_column("Command")
    commands.add_row("Space", "Pause or resume log output")
    commands.add_row("p", "Select a coloring-rule profile")
    commands.add_row("c", "Add a color rule")
    commands.add_row("f", "Toggle display of unmatched lines")
    commands.add_row("r", "Open the rules manager")
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
    *,
    source_file: str | None = None,
    session_only_profile: bool = False,
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
                    active_profile, source_file=source_file, session_only=session_only_profile,
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
            elif key in (b"r", b"R") and rules_path is not None:
                _manage_rules(
                    console, rules, rules_path, keyboard_fd, profile=active_profile,
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
            "  c      Add a color rule\n"
            "  r      Open the rules manager\n"
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
        help="use a named coloring-rule profile",
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
    parser.add_argument(
        "-v", "--version",
        action="store_true",
        help="show the version and exit",
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
        config = _read_toml(config_path) if config_path.exists() else {}
        configured_profiles = _parse_rule_profiles(config, config_path, allow_builtins=True)
        mappings = _parse_profile_paths(config, config_path, configured_profiles)
        has_file_source = sys.stdin.isatty() and bool(tail_arguments)
        source_file = None
        if not options.show_rules and not options.version:
            if options.less:
                source_file = _single_file_argument(tail_arguments, less=True)
            elif has_file_source:
                source_file = _single_file_argument(tail_arguments)
        matched_profile = (
            _matching_profile(source_file, mappings) if source_file is not None else None
        )
        automatic_profile = options.profile is None and matched_profile is not None
        active_profile = (
            options.profile
            if options.profile is not None
            else matched_profile if automatic_profile
            else _configured_active_profile(config, config_path)
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
        if options.version:
            console.print(f"ctail version {version('ctail')}")
            return
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
                "Enter: blank line  Space: pause/resume  p: profile  c: add rule  "
                "r: rules manager  ?: help  q: quit", style="dim",
            )
            if automatic_profile:
                console.print(Text(
                    f"Profile {active_profile!r} selected by file path.", style="dim"
                ))

        _consume_stream(
            source.fileno(),
            console,
            rules,
            show_unmatched,
            keyboard_fd,
            rules_path if keyboard_fd is not None else None,
            original_terminal,
            active_profile,
            source_file=source_file,
            session_only_profile=automatic_profile,
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
