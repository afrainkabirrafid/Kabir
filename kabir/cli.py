"""Command-line interface for Kabir.

Usage:
    kabir run file.kbr      Run a Kabir program
    kabir fmt file.kbr ...  Format .kbr files in place (--check to only check)
    kabir repl              Start the interactive REPL
    kabir                   Start the REPL (no arguments)
    kabir --help            Show help
    kabir --version         Print the version
"""

import os
import sys

from . import __version__
from .errors import KabirError
from .formatter import format_source
from .interpreter import Interpreter
from .parser import parse as parse_source
from . import ast_nodes as ast

HISTORY_FILE = os.path.expanduser("~/.kabir_history")
HISTORY_LENGTH = 1000

HELP_TEXT = """Kabir %s -- a small programming language, built from scratch.

Usage:
  kabir run <file.kbr>   Run a Kabir program
  kabir fmt <file.kbr>   Format .kbr files in place (see --check)
  kabir repl             Start the interactive REPL
  kabir                  Start the REPL (no arguments)
  kabir --help           Show this help
  kabir --version        Print the version

Examples:
  kabir run examples/hello.kbr
  kabir run my_program.kbr
  kabir fmt examples/hello.kbr
  kabir fmt --check examples/*.kbr
""" % __version__


def _fail(message, code=2):
    print(message, file=sys.stderr)
    return code


def run_file(path):
    """Run a .kbr file. Returns an exit code."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            source = handle.read()
    except FileNotFoundError:
        return _fail("kabir: cannot open %r -- no such file" % path)
    except OSError as exc:
        return _fail("kabir: cannot read %r -- %s" % (path, exc))
    interp = Interpreter(source, filename=path)
    try:
        interp.run_source(source, filename=path)
    except KabirError as err:
        print(err.render(), file=sys.stderr)
        return 1
    except RecursionError:
        print("%s: error: too many nested function calls -- check for "
              "infinite recursion" % path, file=sys.stderr)
        return 1
    return 0


def _braces_balanced(text):
    """Count braces outside strings/comments; >0 means still open."""
    depth = 0
    in_str = False
    i = 0
    while i < len(text):
        ch = text[i]
        if in_str:
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                in_str = False
        else:
            if ch == "#":
                break
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
        i += 1
    return depth


def fmt_files(paths, check=False):
    """Format .kbr files in place, or only report what would change.

    Returns an exit code: 0 when everything is fine (and, with
    --check, when nothing would change), 1 when a file would change
    (with --check) or when any file could not be read, parsed, or
    written. A file with a syntax error is reported and skipped, and
    is never written.
    """
    changed = []
    failed = False
    for path in paths:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                source = handle.read()
        except FileNotFoundError:
            print("kabir: cannot open %r -- no such file" % path,
                  file=sys.stderr)
            failed = True
            continue
        except OSError as exc:
            print("kabir: cannot read %r -- %s" % (path, exc),
                  file=sys.stderr)
            failed = True
            continue
        try:
            formatted = format_source(source, path)
        except KabirError as err:
            print(err.render(), file=sys.stderr)
            failed = True
            continue
        if formatted == source:
            continue
        changed.append(path)
        if check:
            continue
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(formatted)
        except OSError as exc:
            print("kabir: cannot write %r -- %s" % (path, exc),
                  file=sys.stderr)
            failed = True
    if check:
        for path in changed:
            print("kabir fmt: %s would change" % path)
    if changed and check:
        return 1
    return 1 if failed else 0


def _setup_history(path=HISTORY_FILE):
    """Turn on readline history for the REPL, if readline exists.

    Loads past lines from `path` so up/down arrows recall them across
    sessions. Never raises: without readline (or with an unreadable
    history file) the REPL simply runs without history. Returns True
    when history is active.
    """
    try:
        import readline
    except ImportError:
        return False
    try:
        readline.read_history_file(path)
    except OSError:
        pass
    try:
        readline.set_history_length(HISTORY_LENGTH)
    except Exception:
        pass
    return True


def _save_history(path=HISTORY_FILE):
    """Write the REPL history back to `path`. Never raises."""
    try:
        import readline
    except ImportError:
        return False
    try:
        readline.write_history_file(path)
    except OSError:
        return False
    return True


def repl():
    """Interactive REPL with brace-aware multi-line continuation."""
    _setup_history()
    print("Kabir %s -- type 'exit' or press Ctrl-D to leave."
          % __version__)
    interp = Interpreter("", filename="<repl>")
    buffer = ""
    while True:
        prompt = "kabir> " if not buffer else "... "
        try:
            line = input(prompt)
        except EOFError:
            print()
            break
        except KeyboardInterrupt:
            print()
            buffer = ""
            continue
        if not buffer and line.strip() in ("exit", "quit"):
            break
        buffer = (buffer + "\n" + line) if buffer else line
        if _braces_balanced(buffer) > 0:
            continue
        source = buffer
        buffer = ""
        if not source.strip():
            continue
        try:
            program = parse_source(source, "<repl>")
        except KabirError as err:
            print(err.render(), file=sys.stderr)
            continue
        interp.source = source
        interp.lines = source.split("\n")
        interp.filename = "<repl>"
        for stmt in program.statements:
            try:
                result = interp.run_stmt(stmt)
            except KabirError as err:
                print(err.render(), file=sys.stderr)
                break
            except RecursionError:
                print("<repl>: error: too many nested function calls",
                      file=sys.stderr)
                break
            if isinstance(stmt, ast.ExprStmt):
                print(interp.format_value(result))
    _save_history()
    return 0


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        return repl()
    if args[0] in ("-h", "--help", "help"):
        print(HELP_TEXT, end="")
        return 0
    if args[0] in ("-v", "--version", "version"):
        print("kabir %s" % __version__)
        return 0
    if args[0] == "run":
        if len(args) < 2:
            return _fail("kabir: 'run' needs a file -- "
                         "try: kabir run examples/hello.kbr")
        if len(args) > 2:
            return _fail("kabir: too many arguments for 'run' -- "
                         "give exactly one file")
        return run_file(args[1])
    if args[0] == "repl":
        if len(args) > 1:
            return _fail("kabir: 'repl' takes no arguments")
        return repl()
    if args[0] == "fmt":
        check = False
        paths = []
        for arg in args[1:]:
            if arg == "--check":
                check = True
            elif arg.startswith("-"):
                return _fail("kabir: unknown option %r for 'fmt' -- "
                             "only --check is supported" % arg)
            else:
                paths.append(arg)
        if not paths:
            return _fail("kabir: 'fmt' needs at least one .kbr file -- "
                         "try: kabir fmt examples/hello.kbr")
        return fmt_files(paths, check=check)
    if args[0].endswith(".kbr"):
        return run_file(args[0])
    return _fail("kabir: unknown command %r\n\n%s" % (args[0], HELP_TEXT))


if __name__ == "__main__":
    sys.exit(main())
