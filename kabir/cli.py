"""Command-line interface for Kabir.

Usage:
    kabir run file.kbr      Run a Kabir program
    kabir repl              Start the interactive REPL
    kabir                   Start the REPL (no arguments)
    kabir --help            Show help
    kabir --version         Print the version
"""

import os
import sys

from . import __version__
from .errors import KabirError
from .interpreter import Interpreter
from .parser import parse as parse_source
from . import ast_nodes as ast

HELP_TEXT = """Kabir %s -- a small programming language, built from scratch.

Usage:
  kabir run <file.kbr>   Run a Kabir program
  kabir repl             Start the interactive REPL
  kabir                  Start the REPL (no arguments)
  kabir --help           Show this help
  kabir --version        Print the version

Examples:
  kabir run examples/hello.kbr
  kabir run my_program.kbr
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


def repl():
    """Interactive REPL with brace-aware multi-line continuation."""
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
    if args[0].endswith(".kbr"):
        return run_file(args[0])
    return _fail("kabir: unknown command %r\n\n%s" % (args[0], HELP_TEXT))


if __name__ == "__main__":
    sys.exit(main())
