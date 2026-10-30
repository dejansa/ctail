# Project guidelines

## Project setup

- This project requires Python 3.14 or newer and uses `uv` with a project-local
  `.venv`. Run `uv sync` after changing dependencies.
- The Python CLI is in `src/ctail/`; the Bash implementation in `bash/` is a
  reference and should not be changed unless the task includes it.

## Configuration

- User configuration and all coloring rules live in `~/.config/ctail/ctail.toml`.
- Use TOML `[[rules]]` entries for individual coloring rules. See the examples
  and manual testing steps in [README.md](README.md).
- Do not overwrite or modify a user's existing files in `~/.config/ctail/`
  unless explicitly requested.

## Build and validation

- Run `uv sync` to install project dependencies into `.venv`.
- Run the CLI with `uv run ctail`; use the manual smoke and interactive
  instructions in [README.md](README.md) when validating CLI behavior.
- Run `uv run ruff check .` for linting; use `uv run ruff check --fix .` to
  apply Ruff's safe automatic fixes, then rerun the check.
- Keep changes focused, preserve the Bash version's tailing behavior, and
  update the README when usage or configuration changes.
