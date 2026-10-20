"""Render Control Room systemd unit templates for a repository checkout."""

import argparse
from pathlib import Path
import sys


_REPO_ROOT_MARKER = "@REPO_ROOT@"


def render_unit(template_path: str | Path, repo_root: str | Path) -> str:
    """Render a unit template with a systemd-quoted checkout path."""
    template = Path(template_path).read_text(encoding="utf-8")
    if template.count(_REPO_ROOT_MARKER) != 1:
        raise ValueError("unit template must contain exactly one repository path marker")

    path = str(Path(repo_root).resolve())
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in path):
        raise ValueError("repository path must not contain control characters")
    escaped_path = (
        path.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("%", "%%")
    )
    return template.replace(_REPO_ROOT_MARKER, f'"{escaped_path}"')


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="render_units")
    parser.add_argument("template", help="unit template to render")
    parser.add_argument("repo_root", help="repository checkout path")
    args = parser.parse_args(argv)

    try:
        rendered = render_unit(args.template, args.repo_root)
    except (OSError, ValueError) as error:
        print(f"Unable to render unit template: {error}", file=sys.stderr)
        return 1

    sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    sys.exit(main())
