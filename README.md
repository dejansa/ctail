# ctail

`ctail` tails log output and colors matching parts of each line using Rich.
It accepts the same arguments as the system `tail` command when reading from
the terminal, and can also color a piped stream:

```sh
ctail -f /var/log/myapp.log
cat application.log | ctail
```

Configuration lives in `~/.config/ctail/ctail.toml`. Rules are applied only
to the matching part of a line; regex rules are checked in file order, and
earlier rules take precedence when matches overlap. Unmatched lines are hidden
by default, as in the original Bash script. Set `show_unmatched = true` to
display them.

```toml
show_unmatched = true

[[rules]]
pattern = '\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}'
color = "bright_black"

[[rules]]
pattern = '(?i)\[(?:error|fatal)\]'
color = "bold bright_red"

[[rules]]
pattern = '(?i)\[debug\]'
color = "bright_green"
```

Colors use Rich style names, including the standard and bright ANSI colors,
256-color names such as `orange3`, and combinations such as `bold magenta`.
Background colors use `on`: for example, `color = "bold white on red"` or
`color = "black on orange3"`. These styles also work in the interactive
color prompt. Custom colors can use `#rrggbb`, `rgb(r,g,b)`, or `color(0)`
through `color(255)`.
Set `regex = false` on a rule to match its pattern literally. If omitted,
`regex` defaults to `true`.

Built-in rules color `[ERROR]`/`[FATAL]`, `[WARN]`/`[WARNING]`, `[INFO]`, and
`[DEBUG]` (case-insensitively). Custom rules take precedence over built-ins.

When following a file in an interactive terminal, press **Space** to
pause/resume, **c** to add a literal text/color rule, **r** to remove a custom
rule, **d** to enable/disable any rule, or **q** to quit. Rules
created with **c** are saved separately in
`~/.config/ctail/rules.toml`, and take precedence over the main configuration.
Use `--no-color` to disable colored output.

## Listing colors and rules

```sh
ctail -lsc                 # or --list-colors
ctail -shr                 # or --show-rules
ctail -lsc -shr            # show both and exit
ctail --show-rules --no-color
```

`--list-colors` prints every supported named color in that color, plus examples
of foreground/background styles and custom color syntax. It does not require
valid configuration files.

`--show-rules` lists all loaded rules in priority order: ad-hoc rules from
`~/.config/ctail/rules.toml`, user rules from `~/.config/ctail/ctail.toml`,
then built-in rules. Each entry shows its source, enabled/disabled status,
regex/literal mode, pattern, and style rendered in its configured colors.

Both commands exit without reading stdin or starting `tail`. Colors are shown
in a terminal; redirected output and `--no-color` produce plain text.

## Removing and disabling rules

While following a file, press **r** to list removable rules from both
`rules.toml` and `ctail.toml`. Use **Up/Down** to select a rule and **Enter**
to delete its TOML entry permanently. Built-in rules cannot be deleted.

Press **d** to list all rules, including built-ins and disabled rules.
Use **Up/Down** and **Enter** to toggle the selected rule's enabled/disabled
state. Press **Escape** in either selector to cancel without changing anything.
Log consumption pauses while a selector is open and resumes afterward, retaining
the previous pause/resume state.

Changes apply immediately to subsequent log lines and persist across restarts.
Custom rules are changed in their own source file; existing comments, other
rules, and unrelated settings are preserved. Failed saves are reported without
changing the running rule list.

To disable a custom rule manually, add `disabled = true` to its `[[rules]]`
entry in either configuration file:

```toml
[[rules]]
pattern = 'request_id=\w+'
color = "bright_cyan"
disabled = true
```

`disabled` defaults to `false`. A disabled rule is skipped; another enabled rule
can still match the same text.

Built-ins have no file entry initially. Toggling one creates an explicit
override in `ctail.toml`, identified by `builtin = true` and its exact built-in
regex pattern. For example, disable the built-in debug coloring with:

```toml
[[rules]]
builtin = true
pattern = '(?i)\[debug\]'
disabled = true
```

Set `disabled = false` to re-enable it. Built-in overrides are only allowed in
`ctail.toml` and do not create duplicate rules. A separate custom rule matching
the same text remains independent. Manual file edits take effect after
restarting `ctail`.

## Manual test

Install the project dependencies into its virtual environment, then pipe sample
log entries through the CLI:

```sh
uv sync
printf '%s\n' \
  '2026-10-22 11:00:00 [ERROR] database unavailable request_id=ab12' \
  '2026-10-22 11:00:01 [DEBUG] retrying request_id=ab12' \
  '2026-10-22 11:00:02 [INFO] connection restored' \
  'a line with no severity' \
  | uv run ctail
```

The timestamp and `request_id=` are colored by the sample rules, severity
labels by the built-in rules, and the final line is visible because
`show_unmatched = true`. `uv run` uses the project's `.venv`; no global Python
installation is needed.

To check file-following and the interactive controls, start a temporary log in
one terminal:

```sh
printf '%s\n' '2026-10-22 11:00:00 [INFO] ctail started' > /tmp/ctail-demo.log
uv run ctail -f /tmp/ctail-demo.log
```

In another terminal, append entries:

```sh
printf '%s\n' '2026-10-22 11:00:01 [ERROR] example failure' >> /tmp/ctail-demo.log
printf '%s\n' '2026-10-22 11:00:02 [DEBUG] example detail' >> /tmp/ctail-demo.log
```

While `ctail` is following the file, press **Space** to pause/resume, **c** to
enter a literal string and Rich color for a persisted ad-hoc rule, **r** to
remove a rule, **d** to enable/disable a rule, and **q** to quit. In the selectors,
check arrow-key navigation, **Escape** cancellation, and **Enter** to apply;
restart the app or run `ctail --show-rules` to verify the saved state.
Interactive keys are available when following a file in a terminal;
they are not used when input is piped. Remove the sample file afterward with
`rm /tmp/ctail-demo.log`.

## TOML rule tables

The `[[rules]]` syntax is TOML's array-of-tables syntax: every occurrence
starts a new rule, so a configuration can define any number of rules with the
same `pattern` and `color` keys. In contrast, `[rules]` defines only one table
and cannot be repeated to add more rules.

The runtime configuration directory is not created automatically. Create it
and save the examples below as `~/.config/ctail/ctail.toml` and
`~/.config/ctail/rules.toml`, respectively. The second file is separate so
interactive rules can be added without changing the main configuration.

Example main configuration (`ctail.toml`):

```toml
show_unmatched = true

[[rules]]
pattern = '\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}'
color = "bright_black"

[[rules]]
pattern = '(?i)\[(?:error|fatal)\]'
color = "bold bright_red"

[[rules]]
pattern = '(?i)\[debug\]'
color = "bright_green"
```

Example ad-hoc rules (`rules.toml`):

```toml
[[rules]]
pattern = "request_id="
color = "bright_cyan"
regex = false

[[rules]]
pattern = "cache-miss"
color = "orange3"
regex = false
```

These sample ad-hoc rules match literal text. Rules added interactively are
also saved as literal matches in `rules.toml`.