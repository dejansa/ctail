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

Use `ctail --version` (or `ctail -v`) to show the installed package version.
The version is maintained in `pyproject.toml`.

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
`~/.config/ctail/` and copies in an example `ctail.toml`, `default.rules`,
and `http-access.rules` if the configuration does not already exist. Existing configuration is never replaced, including when
upgrading the tool. The starter files are maintained in the repository's
top-level `config/` folder. Add `~/.local/bin` to `PATH` if it is not already
there.

Settings live in `~/.config/ctail/ctail.toml`; coloring rules live in
TOML-formatted `.rules` files in the same directory. Rules are applied only
to the matching part of a line; regex rules are checked in file order, and
earlier rules take precedence when matches overlap. Unmatched lines are hidden
by default, as in the original Bash script. Set `show_unmatched = true` to
display them.

Use `--profile NAME` (or `-p NAME`) to select a named set of rules for a
particular log format. Define each profile in a file named `NAME.rules`.
Selecting a profile saves `active_profile = "NAME"` in that file; subsequent
runs use the saved profile unless another `--profile` is supplied. Without a
saved or explicit profile, `default.rules` is used. Selecting a profile
replaces the default-only rules. Built-in rules and rules in `default.rules`
marked `shared = true` remain active.

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

```

Each mapping references an existing profile file such as `asaklogs.rules`. A path ending in
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

For example, `http-access.rules` contains:

```toml
[[rules]]
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
lines are shown, **c** to add a color rule, **r** to open the
rules manager, **?** to show the interactive
command list, or **q** to quit. The
**f** toggle applies only to the current run; it does not change
`ctail.toml`. The help panel closes when any key is pressed. Rules created
with **c** are saved in the selected profile's `.rules` file. Without an active
named profile, new rules apply only to the default profile. Use the rules
manager to copy a custom rule to another named profile.

Press **p** to open the profile selector. The table shows each profile and its
configured path patterns; a dash means there are no associated paths.
**(default)** is the first option; use **Up/Down** and **Enter** to select a
profile, or **Escape** to cancel.
The **n** action shown below the profile list prompts for a name, saves a new
empty `NAME.rules` profile, and activates it as the saved profile. Selecting
**(default)** removes `active_profile` from the configuration. Adding a rule
with **c** while a named profile is active writes to that profile's file;
with **(default)** active, new interactive rules are default-only in `default.rules`.
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
by `*.rules` files. It accepts the short alias `-lsp`.

`--show-rules` without a profile lists the default rules in priority order:
shared rules, default rules from `default.rules`, and
built-in rules. With `--profile NAME`, it lists only rules declared for that
profile's `.rules` file; shared and built-in rules are omitted.
Each entry shows its source, enabled/disabled status, regex/literal mode,
optional name, pattern, and style rendered in its configured colors.

These listing commands exit without reading stdin or starting `tail`. Colors
are shown in a terminal; redirected output and `--no-color` produce plain text.

## Managing rules

While following a file, press **r** to open the rules manager. It lists the
current profile's user-defined rules in priority order, including shared
ad-hoc and disabled rules. Built-in coloring remains active but built-in rules
are not shown. To customize a built-in rule, add an override in the
configuration file as described below. Columns show status, source, profile,
type, name, pattern and style. Columns grow to fit their contents and wrap when
the available terminal width is limited.
The manager adjusts the visible rows to keep the selected rule in view.
Use **Up/Down** to select a rule:

- **a** opens the rule editor to add a rule.
- **e** opens the editor for the selected rule.
- **r** renames the selected rule. Edit its prefilled name with **Left/Right**
  and **Backspace**, then press **Enter** to save a non-empty name or **Escape**
  to cancel. Unnamed rules start with an empty field. Only the name changes.
- **d** toggles the selected rule's enabled/disabled state.
- **x** removes a rule after confirmation.
- **c** copies a custom rule to another named profile. The current profile is
  selected initially when it is a valid destination; the rule's source profile
  is not offered. Confirm the copy.
- An identical rule already in the destination is not copied again.
- **Escape** cancels a pending action or closes the manager.

The source rule remains unchanged. The copy is inserted first in the destination
profile, which can change match priority there. Copying a shared rule adds a
profile-specific copy without affecting its shared source. Log consumption pauses
while the manager is open and resumes afterward, retaining the previous
pause/resume state.

Changes apply immediately to subsequent log lines and persist across restarts.
Custom rules are changed in their `.rules` files; existing comments, other
rules, and unrelated settings are preserved. Failed saves are reported without
changing the running rule list.

### Adding a rule

Press **c** while following a log or **a** in the rules manager. Type the
pattern, then use **Left/Right** to select Foreground or Background.
Both colors start at `none`; **Up/Down** browse colors, with basic colors first.
Press **b** on Foreground to toggle bold, or **i** on Background to toggle
inverse. Style is shown for reference. Press **Enter** on either color to save,
or **Escape** to discard changes. Validation errors leave the dialog open.

New rules start in literal mode. Typing any of `. ^ $ * + ? { } [ ] \ | ( )`
automatically switches to regex mode. Press **Down** from Pattern to select
Mode, then **Enter** or **Left/Right** to toggle it manually; **Up** returns to
Foreground. Typing another regex metacharacter switches it back to regex.
With Pattern selected, press **Enter** to edit at a specific position;
**Left/Right** move the cursor, **Backspace** deletes, **Enter** accepts the
text, and **Escape** discards that text edit.

Rules are inserted at the beginning of the current profile's scope. They are
not shared automatically, including in the default profile. Use **c** in the
rules manager to copy a custom rule to another named profile.

### Editing a rule

Select **e** in the rules manager. Use **Left/Right**
to select Pattern, Foreground or Background. For colors, **Up/Down** browse
the named colors, including `default` and `none`; nearby choices are shown
above and below the selection. Press **Down** from Pattern or **Right** from
Background to select Bold; **Up** from Bold returns to Background.
Use **Up/Down** to select Bold, Inverse, Scope, Save, Cancel, Mode or Style.
Use **Left/Right** or **Enter** to toggle Bold, Inverse or Mode. A preview shows the
current style.
Turning either attribute off removes it from the style; explicitly entered
negative attributes are otherwise preserved.
The controls and keyboard instructions are displayed on separate lines.
Very short terminals use a compact layout.
The Pattern column is 44 terminal cells wide when space permits; on narrower
terminals the columns shrink, and the pattern view keeps the cursor visible.

With Pattern selected, type directly and use **Backspace** to remove the last
character. **Left/Right** still move between fields. For editing at a specific
position, press **Enter** on Pattern or Style to enter text-edit mode. In that mode,
**Left/Right** move the cursor, **Backspace** deletes, **Enter** accepts the
text, and **Escape** discards that text edit. Directly typed pattern changes
remain in the editor until saved or canceled. Outside text-edit mode,
**Escape** cancels the entire editor. Select Save and press **Enter** to
validate and save; errors leave the editor open.

Editing retains the selected rule's existing
file, scope, priority, name, enabled state and mode unless you change Mode.

The Style field accepts the full Rich style syntax, including custom colors and
comma-separated capture-group styles. Styles with attributes beyond the
foreground, background, bold and inverse controls, or multiple capture-group
styles, remain unchanged unless edited in Style. The visual color and
attribute controls are unavailable for those complex styles; edit Style
directly to change them. Built-in rules are not available in the manager. To
change one, add an override in the `.rules` file for the current profile or
default rules.

To disable a custom rule manually, add `disabled = true` to its `[[rules]]`
entry in its `.rules` file:

```toml
[[rules]]
pattern = 'request_id=\w+'
color = "bright_cyan"
disabled = true
```

`disabled` defaults to `false`. A disabled rule is skipped; another enabled rule
can still match the same text.

Built-ins have no file entry initially. Toggling one creates an explicit
override in the active `.rules` file, identified by `builtin = true` and its exact built-in
regex pattern. For example, disable the built-in debug coloring with:

```toml
[[rules]]
builtin = true
pattern = '(?i)\[debug\]'
disabled = true
```

Set `disabled = false` to re-enable it. Built-in overrides are only allowed in
`.rules` files and do not create duplicate rules. A separate custom rule matching
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
open the rule editor for a persisted ad-hoc rule, **r** to open
the rules manager, **?** to open the help panel, and **q** to quit. In the
manager, check arrow-key navigation, **a** to add, **e** to edit, **d** to
toggle, **x** to remove, **c** to copy a rule to another profile, confirmation and
**Escape** cancellation. In the editor, check color browsing, the preview,
automatic regex mode, keyboard shortcuts, saving and cancellation;
restart the app or run `ctail --show-rules` to verify the saved state.
Interactive keys are available when following a file in a terminal;
they are not used when input is piped. Remove the sample file afterward with
`rm /tmp/ctail-demo.log`.

## TOML rule tables

The `[[rules]]` syntax is TOML's array-of-tables syntax: every occurrence
starts a new rule, so a configuration can define any number of rules with the
same `pattern` and `color` keys. In contrast, `[rules]` defines only one table
and cannot be repeated to add more rules.

The runtime configuration directory and starter files are created
automatically on the first run if they are missing. The examples are maintained
in the repository's top-level `config/` folder. Existing installations are not
automatically migrated. Back up `ctail.toml`, then move its top-level `[[rules]]`
entries to `default.rules` and each `[profiles.NAME]` profile's rules to
`NAME.rules`, changing each `[[profiles.NAME.rules]]` header to `[[rules]]`.
Preserve the order and `shared` flags, including built-in overrides. Leave
settings and `[[profile_paths]]` in `ctail.toml`. Empty profiles need an empty
`NAME.rules` file. Legacy embedded rules remain readable, but a profile or
default rules cannot be defined in both formats. The older `rules.toml` file
is not read; migrate its rules separately.

Example main configuration (`ctail.toml`):

```toml
show_unmatched = true
active_profile = "nginx"

[[profile_paths]]
profile = "nginx"
paths = ["/var/log/nginx/*.log"]
```

Example `default.rules`:

```toml
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

[[rules]]
name = "Request identifier"
pattern = "request_id="
color = "bright_cyan"
regex = false
shared = true
```

Example `nginx.rules`:

```toml
[[rules]]
name = "Nginx request method"
pattern = '\b(?:GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)\b'
color = "bright_cyan"
```

Rules added interactively start in literal mode and switch to regex mode when
a regex metacharacter is typed; Mode can also be toggled manually.
Entries in `default.rules` apply to the default profile unless marked
`shared = true`, in which case they apply to every profile and take priority
over profile-specific rules. Entries in `NAME.rules` apply only to the
matching profile. Interactive rules are added to the active profile, or to the
default profile when no named profile is active; use the rules manager to make
them shared. The active profile is persistent, so after selecting
it once you can start ctail without `--profile`. To change it for a run and
persist that change, supply a different profile explicitly:

```sh
ctail --profile nginx -f /var/log/nginx/access.log
```

The profile name is the filename before `.rules`. Profiles
replace the default-only rules; shared rules, built-in
rules, and global settings such as `show_unmatched` still apply.
`--show-rules --profile nginx` displays the profile-specific rules.