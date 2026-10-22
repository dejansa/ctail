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
Set `regex = false` on a rule to match its pattern literally. If omitted,
`regex` defaults to `true`.

Built-in rules color `[ERROR]`/`[FATAL]`, `[WARN]`/`[WARNING]`, `[INFO]`, and
`[DEBUG]` (case-insensitively). Custom rules take precedence over built-ins.

When following a file in an interactive terminal, press **Space** to
pause/resume, **c** to add a literal text/color rule, or **q** to quit. Rules
created with **c** are saved separately in
`~/.config/ctail/rules.toml`, and take precedence over the main configuration.
Use `--no-color` to disable colored output.

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
enter a literal string and Rich color for a persisted ad-hoc rule, and **q** to
quit. Interactive keys are available when following a file in a terminal;
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