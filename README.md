# ctail

`ctail` tails log output and colors matching parts of each line using Rich.
It accepts the same arguments as the system `tail` command when reading from
the terminal, and can also color a piped stream:

```sh
ctail -f /var/log/myapp.log
cat application.log | ctail
```

Use `-l` or `--less` to colorize a complete file and browse it with the system
`less` pager instead of tailing it. Pager mode accepts file paths or piped input
and requires `less` to be installed:

```sh
ctail --less /var/log/myapp.log
cat application.log | ctail -l
```

## Install from GitLab

With SSH access to the GitLab repository and Python 3.14 or newer, install the
tool with either `uv` or `pipx`:

```sh
uv tool install 'git+ssh://git@gitlab.com/aviatnetworks/qa/tools/ctail.git'
# or
pipx install 'git+ssh://git@gitlab.com/aviatnetworks/qa/tools/ctail.git'
```

Both commands install the `ctail` command into the tool's executable path
(typically `~/.local/bin`). On the first run, `ctail` creates
`~/.config/ctail/` and copies in an example `ctail.toml` if one does not
already exist. Existing configuration is never replaced, including when
upgrading the tool. The starter files are maintained in the repository's
top-level `config/` folder. Add `~/.local/bin` to `PATH` if it is not already
there.

Configuration lives in `~/.config/ctail/ctail.toml`. Rules are applied only
to the matching part of a line; regex rules are checked in file order, and
earlier rules take precedence when matches overlap. Unmatched lines are hidden
by default, as in the original Bash script. Set `show_unmatched = true` to
display them.

Use `--profile NAME` (or `-p NAME`) to select a named set of rules for a
particular log format. Define profiles in `~/.config/ctail/ctail.toml`.
Selecting a profile saves `active_profile = "NAME"` in that file; subsequent
runs use the saved profile unless another `--profile` is supplied. Without a
saved or explicit profile, the top-level rules in `ctail.toml` are used.
Selecting a profile replaces those top-level rules. Built-in rules and
top-level rules in `rules.toml` remain active. Temporary rules can also be
grouped under matching `[profiles.NAME]` sections in `rules.toml`.

To select a profile automatically for a file, add ordered path mappings to
`ctail.toml`:

```toml
[[profile_paths]]
profile = "asaklogs"
paths = [
    "~/.local/share/asak2/logs/",
    "~/other-logs/asak-*.log",
]

[[profile_paths]]
profile = "http-access"
paths = ["/var/log/nginx/*.log"]

[profiles.asaklogs]
# Add [[profiles.asaklogs.rules]] entries here.
```

Each mapping references a profile defined in `ctail.toml`. A path ending in
`/` matches files directly in that directory, not in subdirectories. A path
without wildcards matches a single file; `*`, `?`, and character classes match
within one path component, while `**` matches across directory levels. For
example, `~/logs/**/*.log` includes `.log` files directly in `~/logs` and in
its subdirectories. Paths are case-sensitive, `~` is expanded, and paths must
be absolute after expansion. Matching normalizes `.` and `..` without resolving
symlinks or checking whether the file exists. Input filenames are matched as
provided to ctail, relative to the current working directory when necessary.

The first matching mapping wins. Selection precedence is: explicit
`--profile`, path mapping, saved `active_profile`, then default rules.
Automatic selection does not change `active_profile`. It applies to a single
identifiable file in either tail or `--less` mode; multiple files, piped input,
or ambiguous tail options fall back to the saved or default profile. Quote
patterns in the configuration; shell-expanded arguments that produce multiple
files do not select a profile automatically. Use `--profile` when automatic
detection is not possible. Listing commands do not match paths.

```toml
show_unmatched = true

[[rules]]
name = "Log timestamp"
pattern = '\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}'
color = "bright_black"

[[rules]]
name = "Errors and fatal messages"
pattern = '(?i)\[(?:error|fatal)\]'
color = "bold bright_red"

[[rules]]
name = "Debug messages"
pattern = '(?i)\[debug\]'
color = "bright_green"

[profiles.http-access]

[[profiles.http-access.rules]]
name = "HTTP request method"
pattern = '\b(?:GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)\b'
color = "bright_cyan"
```

`name` is an optional descriptive label for a rule. It is shown with the
pattern in `--show-rules` and the interactive rule selector. Existing rules
without a name remain valid.

Colors use Rich style names, including the standard and bright ANSI colors,
256-color names such as `orange3`, and combinations such as `bold magenta`.
Background colors use `on`: for example, `color = "bold white on red"` or
`color = "black on orange3"`. These styles also work in the interactive
color prompt. Custom colors can use `#rrggbb`, `rgb(r,g,b)`, or `color(0)`
through `color(255)`.
Set `regex = false` on a rule to match its pattern literally. If omitted,
`regex` defaults to `true`.

For regex rules with capture groups, `color` can contain comma-separated
styles. Each style colors the corresponding capture group from left to right;
an optional style after the group styles colors the rest of the regex match.
For example, `color = "bright_cyan, bold bright_red, bright_black"` colors
`status=` cyan, the captured status code red, and the rest of the matched text
black. With only one or two styles, groups without a corresponding style and
the rest of the match are left unchanged. A rule without capture groups keeps
the existing behavior of coloring the whole match with its single color.
Commas inside `rgb(r,g,b)` are treated as part of that color.

Built-in rules color `[ERROR]`/`[FATAL]`, `[WARN]`/`[WARNING]`, `[INFO]`, and
`[DEBUG]` (case-insensitively). Custom rules take precedence over built-ins.

When following a file in an interactive terminal, press **Space** to
pause/resume, **Enter** to print a blank line and visually separate log output,
**p** to select a coloring-rule profile, **f** to toggle whether unmatched
lines are shown, **c** to add a literal text/color rule, **r** to remove a
custom rule, **d** to enable/disable any rule, **?** to show the interactive
command list, or **q** to quit. The
**f** toggle applies only to the current run; it does not change
`ctail.toml`. The help panel closes when any key is pressed. Rules created
with **c** are saved in `~/.config/ctail/rules.toml`. With no active profile
they are shared; when a profile is active they are saved to that profile and
apply only to it.

Press **p** to open the profile selector. **(default)** is the first option;
use **Up/Down** and **Enter** to select a profile, or **Escape** to cancel.
The **n** action shown below the profile list prompts for a name, saves a new
empty profile in `ctail.toml`, and activates it as the saved profile. Selecting
**(default)** removes `active_profile` from the configuration. Adding a rule
with **c** while a named profile is active stores the rule under that profile
in `rules.toml`; with **(default)** active, new ad-hoc rules remain shared.
If a path mapping selected the profile at startup, the terminal shows the
selected profile, and changing or creating a profile with **p** affects only
the current run. The profile definition and its rules are still saved. For
other runs, profile selection retains its usual persistent behavior.

When following one identifiable file, highlight a named profile in the **p**
selector and press **a** to associate it with the current **f**ile or its
**d**irectory. Ctail shows the exact pattern and asks for confirmation before
saving it. The new mapping is inserted first, so it takes precedence over
existing mappings, and applies on the next launch. Canceling leaves the
configuration unchanged. This action does not change the active profile.
Use `--no-color` to disable colored output.

## Listing colors and rules

```sh
ctail -lsc                 # or --list-colors
ctail -shr                 # or --show-rules
ctail -lsp                 # or --list-profiles
ctail -lsc -shr            # show both and exit
ctail --show-rules --no-color
```

`--list-colors` prints every supported named color in that color, plus examples
of foreground/background styles and custom color syntax. It does not require
valid configuration files.

`--list-profiles` prints the named profiles configured in
`~/.config/ctail/ctail.toml`. It accepts the short alias `-lsp`.

`--show-rules` without a profile lists the default rules in priority order:
shared ad-hoc rules, top-level rules from `~/.config/ctail/ctail.toml`, and
built-in rules. With `--profile NAME`, it lists only rules declared for that
profile in either configuration file; shared and built-in rules are omitted.
Each entry shows its source, enabled/disabled status, regex/literal mode,
optional name, pattern, and style rendered in its configured colors.

These listing commands exit without reading stdin or starting `tail`. Colors
are shown in a terminal; redirected output and `--no-color` produce plain text.

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
remove a rule, **d** to enable/disable a rule, **?** to open the help panel,
and **q** to quit. In the selectors, check arrow-key navigation, **Escape**
cancellation, and **Enter** to apply;
restart the app or run `ctail --show-rules` to verify the saved state.
Interactive keys are available when following a file in a terminal;
they are not used when input is piped. Remove the sample file afterward with
`rm /tmp/ctail-demo.log`.

## TOML rule tables

The `[[rules]]` syntax is TOML's array-of-tables syntax: every occurrence
starts a new rule, so a configuration can define any number of rules with the
same `pattern` and `color` keys. In contrast, `[rules]` defines only one table
and cannot be repeated to add more rules.

The runtime configuration directory and both starter files are created
automatically on the first run if they are missing. The examples are maintained
in the repository's top-level `config/` folder. Interactive rules are saved in
the separate `~/.config/ctail/rules.toml` file without changing the main
configuration. To add ad-hoc rules manually, save the example below as
`~/.config/ctail/rules.toml`.

Example main configuration (`ctail.toml`):

```toml
show_unmatched = true

[[rules]]
name = "Log timestamp"
pattern = '\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}'
color = "bright_black"

[[rules]]
name = "Errors and fatal messages"
pattern = '(?i)\[(?:error|fatal)\]'
color = "bold bright_red"

[[rules]]
name = "Debug messages"
pattern = '(?i)\[debug\]'
color = "bright_green"

[profiles.nginx]

[[profiles.nginx.rules]]
name = "Nginx request method"
pattern = '\b(?:GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)\b'
color = "bright_cyan"
```

Example ad-hoc rules (`rules.toml`):

```toml
[[rules]]
name = "Request identifier"
pattern = "request_id="
color = "bright_cyan"
regex = false

[[rules]]
name = "Cache misses"
pattern = "cache-miss"
color = "orange3"
regex = false

[profiles.nginx]

[[profiles.nginx.rules]]
name = "Nginx upstream status"
pattern = "upstream_status="
color = "yellow"
regex = false
```

These sample ad-hoc rules match literal text. Rules added interactively are
also saved as literal matches in `rules.toml`. Top-level `[[rules]]` entries
are shared by every profile; rules under `[profiles.NAME]` apply only to the
matching profile in `ctail.toml`. Interactive rules are saved under the active named profile, and as shared rules
when no profile is active. The active profile is persistent, so after selecting
it once you can start ctail without `--profile`. To change it for a run and
persist that change, supply a different profile explicitly:

```sh
ctail --profile nginx -f /var/log/nginx/access.log
```

The profile name is the key beneath `[profiles]` in `ctail.toml`. Profiles
replace that file's top-level `[[rules]]`; shared and matching temporary rules,
built-in rules, and global settings such as `show_unmatched` still apply.
`--show-rules --profile nginx` displays the active rules and identifies
profile-specific entries.