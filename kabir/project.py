"""Cargo-style project tooling for Kabir (2.0.0).

Creates new projects (``kabir new``), builds them to bytecode
(``kabir build``), and resolves a project's entry point (``kabir run``).

The manifest (``Kabir.toml``) is read with a hand-rolled minimal
parser for the exact format :func:`new_project` writes -- ``tomllib``
is Python 3.11+ and Kabir supports Python 3.8+, so it cannot be used.

The compiler and the type checker are imported lazily, inside
:func:`build_project`, so this module stays importable while the
rest of the 2.0.0 pipeline is still being built. Nothing in this
module prints: the CLI owns all output.
"""

import json
import re
from pathlib import Path

from .errors import KabirError

__all__ = ["new_project", "build_project", "run_project",
           "parse_manifest", "read_manifest"]

# Project names: start with a letter, then letters, digits, "-" or "_".
NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")

MANIFEST_FILENAME = "Kabir.toml"
MAIN_SOURCE = Path("src") / "main.kbr"
TARGET_DIR = "target"

DEFAULT_VERSION = "0.1.0"
DEFAULT_AUTHOR = "Afrain Kabir Rafid"

# Keys allowed in the [package] section of Kabir.toml.
_PACKAGE_KEYS = ("name", "version", "authors")

_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")
_SECTION_RE = re.compile(r"^\[([A-Za-z_][A-Za-z0-9_-]*)\]$")
_KEY_VALUE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*)\s*=\s*(.+)$")


def _manifest_error(message, filename="Kabir.toml", line=None):
    return KabirError(message, filename=filename, line=line)


def _strip_comment(line):
    """Remove a ``#`` comment that appears outside a quoted string."""
    in_str = False
    escaped = False
    for i, ch in enumerate(line):
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "#":
            return line[:i]
    return line


def _parse_value(raw, filename, lineno):
    """Parse one manifest value: a quoted string or a list of strings."""
    raw = raw.strip()
    if raw.startswith('"'):
        try:
            value = json.loads(raw)
        except ValueError:
            raise _manifest_error(
                "Invalid string value %r -- use double quotes, e.g. "
                'name = "my_app"' % raw, filename, lineno)
        if not isinstance(value, str):
            raise _manifest_error(
                "Invalid value %r -- expected a quoted string" % raw,
                filename, lineno)
        return value
    if raw.startswith("["):
        try:
            value = json.loads(raw)
        except ValueError:
            raise _manifest_error(
                "Invalid list value %r -- use double-quoted strings, "
                'e.g. authors = ["Afrain Kabir Rafid"]' % raw,
                filename, lineno)
        if not isinstance(value, list) or not all(
                isinstance(item, str) for item in value):
            raise _manifest_error(
                "Invalid list value %r -- expected a list of quoted "
                "strings" % raw, filename, lineno)
        return value
    raise _manifest_error(
        "Invalid value %r -- expected a quoted string or a list of "
        "quoted strings" % raw, filename, lineno)


def parse_manifest(text, filename="Kabir.toml"):
    """Parse the text of a ``Kabir.toml`` manifest.

    Returns a dict with ``name`` (str), ``version`` (str) and
    ``authors`` (list of str). ``version`` defaults to ``"0.1.0"``
    and ``authors`` to ``[]`` when they are absent. Raises
    :class:`KabirError` on any malformed manifest.
    """
    section = None
    seen_sections = set()
    package = {}
    for lineno, raw_line in enumerate(text.split("\n"), start=1):
        line = _strip_comment(raw_line).strip()
        if not line:
            continue
        match = _SECTION_RE.match(line)
        if match:
            section = match.group(1)
            if section in seen_sections:
                raise _manifest_error(
                    "Duplicate section [%s] in %s" % (section, filename),
                    filename, lineno)
            seen_sections.add(section)
            if section != "package":
                raise _manifest_error(
                    'Unknown section "[%s]" in %s -- only [package] is '
                    "supported" % (section, filename), filename, lineno)
            continue
        match = _KEY_VALUE_RE.match(line)
        if match is None:
            raise _manifest_error(
                "Cannot understand line %d in %s: %r -- expected "
                "'key = value' inside [package]"
                % (lineno, filename, raw_line.strip()),
                filename, lineno)
        if section is None:
            raise _manifest_error(
                "Key %r appears before any section in %s -- put it "
                "under [package]" % (match.group(1), filename),
                filename, lineno)
        key = match.group(1)
        if key not in _PACKAGE_KEYS:
            raise _manifest_error(
                'Unknown key "%s" in [package] -- only name, version '
                "and authors are supported" % key, filename, lineno)
        if key in package:
            raise _manifest_error(
                'Duplicate key "%s" in [package]' % key, filename, lineno)
        package[key] = _parse_value(match.group(2), filename, lineno)
    if "package" not in seen_sections:
        raise _manifest_error(
            "Missing [package] section in %s -- run 'kabir new' to "
            "create a project" % filename, filename)
    if "name" not in package:
        raise _manifest_error(
            'Missing "name" in [package] of %s' % filename, filename)
    for key in ("name", "version"):
        if key in package and not isinstance(package[key], str):
            raise _manifest_error(
                'Key "%s" in [package] of %s must be a quoted string'
                % (key, filename), filename)
    if "authors" in package and not isinstance(package["authors"], list):
        raise _manifest_error(
            'Key "authors" in [package] of %s must be a list of quoted '
            "strings" % filename, filename)
    name = package["name"]
    if not name or not NAME_RE.match(name):
        raise _manifest_error(
            'Invalid project name %r in %s -- names must start with a '
            'letter and contain only letters, digits, "-" and "_"'
            % (name, filename), filename)
    return {
        "name": name,
        "version": package.get("version", DEFAULT_VERSION),
        "authors": package.get("authors", []),
    }


def read_manifest(path):
    """Read and parse the manifest at *path* (a Kabir.toml file).

    Raises :class:`KabirError` when the file is missing or malformed.
    """
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise KabirError(
            "No %s found -- run 'kabir new <name>' to create a project"
            % MANIFEST_FILENAME, filename=str(path))
    except OSError as exc:
        raise KabirError("Cannot read %s -- %s" % (path, exc),
                         filename=str(path))
    return parse_manifest(text, filename=str(path))


def _check_name(name):
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise KabirError(
            "Invalid project name %r -- names must start with a letter "
            'and contain only letters, digits, "-" and "_"'
            % (name,), filename="<project>")


def new_project(name, cwd="."):
    """Create a new Kabir project named *name* inside *cwd*.

    Creates ``<cwd>/<name>/`` with ``Kabir.toml``, ``src/main.kbr``
    and ``.gitignore``. Raises :class:`KabirError` for an invalid
    name or when the directory already exists. Returns the created
    project directory as a :class:`pathlib.Path`.
    """
    _check_name(name)
    project_dir = Path(cwd) / name
    if project_dir.exists():
        raise KabirError('Project "%s" already exists' % name,
                         filename=str(project_dir))
    src_dir = project_dir / "src"
    try:
        src_dir.mkdir(parents=True)
    except OSError as exc:
        raise KabirError("Cannot create project %r -- %s" % (name, exc),
                         filename=str(project_dir))
    manifest = (
        "[package]\n"
        'name = "%s"\n'
        'version = "%s"\n'
        'authors = ["%s"]\n'
        % (name, DEFAULT_VERSION, DEFAULT_AUTHOR)
    )
    main_source = (
        "func main() {\n"
        '    print("Hello from %s!")\n'
        "}\n"
        "\n"
        "main()\n"
        % name
    )
    (project_dir / MANIFEST_FILENAME).write_text(manifest, encoding="utf-8")
    (src_dir / "main.kbr").write_text(main_source, encoding="utf-8")
    (project_dir / ".gitignore").write_text("target/\n", encoding="utf-8")
    return project_dir


def run_project(path):
    """Resolve ``src/main.kbr`` for the project at *path*.

    Returns the entry-point file as a :class:`pathlib.Path`.
    Raises :class:`KabirError` when the project directory or its
    ``src/main.kbr`` does not exist.
    """
    project_dir = Path(path)
    if not project_dir.is_dir():
        raise KabirError(
            'Project directory "%s" does not exist' % project_dir,
            filename=str(project_dir))
    main_path = project_dir / MAIN_SOURCE
    if not main_path.is_file():
        raise KabirError(
            "No src/main.kbr found in %s -- run 'kabir new <name>' to "
            "create a project" % project_dir, filename=str(main_path))
    return main_path


def build_project(path="."):
    """Build the project at *path* to bytecode.

    Reads ``Kabir.toml``, compiles ``src/main.kbr`` (parse -> check
    -> compile, with imports resolved relative to ``src/``) and
    writes ``target/<name>.kbc``. Returns the output file as a
    :class:`pathlib.Path`. Prints nothing.

    The checker and compiler are imported lazily, at call time, so
    this module stays importable while they are still being built.
    """
    project_dir = Path(path).resolve()
    if not project_dir.is_dir():
        raise KabirError(
            'Project directory "%s" does not exist' % project_dir,
            filename=str(project_dir))
    manifest = read_manifest(project_dir / MANIFEST_FILENAME)
    main_path = run_project(project_dir)
    try:
        source = main_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise KabirError("Cannot read %s -- %s" % (main_path, exc),
                         filename=str(main_path))

    # Lazy imports: kabir/checker.py and kabir/compile.py are built
    # by parallel workers and may not exist yet.
    try:
        from .checker import check_program
    except ImportError:
        raise KabirError(
            "Cannot build: the type checker (kabir/checker.py) is not "
            "available yet -- the 2.0.0 compiler pipeline is still "
            "being built", filename=str(main_path))
    try:
        from .compile import compile_program, save_bytecode
    except ImportError:
        raise KabirError(
            "Cannot build: the bytecode compiler (kabir/compile.py) "
            "is not available yet -- the 2.0.0 compiler pipeline is "
            "still being built", filename=str(main_path))

    main_filename = str(main_path.resolve())
    lines = source.split("\n")
    from .parser import parse
    program = parse(source, main_filename)
    check_program(program, filename=main_filename, lines=lines)
    code = compile_program(program, main_filename, lines)

    target_dir = project_dir / TARGET_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    output_path = target_dir / ("%s.kbc" % manifest["name"])
    save_bytecode(code, str(output_path))
    return output_path
