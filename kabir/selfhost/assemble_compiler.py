"""Regenerate the embedded lexer + parser sections of compiler.kbr.

kabir/selfhost/compiler.kbr is a self-contained compiler: it embeds
a copy of the self-hosted lexer (kabir/selfhost/lexer.kbr, functions
renamed with a ``lex_`` prefix) and a copy of the self-hosted parser
(kabir/selfhost/parser.kbr), followed by the compiler section
(everything from the ``# --- compiler`` marker to the end of the file).

Run from the repo root:  python3 kabir/selfhost/assemble_compiler.py

It rebuilds only the embedded sections: the compiler section is
preserved verbatim, so editing the compiler never requires touching
this script, and editing lexer.kbr / parser.kbr is followed by
re-running this script to keep the copies in sync. The script
refuses to run if the compiler section marker is missing.
"""

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SELFHOST = os.path.join(ROOT, "selfhost")
MARKER = "# --- compiler (mirrors kabir/compile.py)"

HEADER = """# { anchor -- the open-brace character is character 2 of this file.
# kabir/selfhost/compiler.kbr -- the Kabir compiler, written in Kabir itself.
#
# A self-contained front end + bytecode compiler: it embeds a copy of
# the self-hosted lexer (kabir/selfhost/lexer.kbr, functions renamed lex_*)
# and a copy of the self-hosted parser (kabir/selfhost/parser.kbr), then
# compiles the AST to the same bytecode dicts kabir/compile.py emits
# (code dict v2). The canonical sources are lexer.kbr and parser.kbr;
# refresh the copies with: python3 kabir/selfhost/assemble_compiler.py
#
# Usage: kabir run kabir/selfhost/compiler.kbr, then type the path of the
# .kbr source file on stdin (one line). One line of JSON is printed:
# the main code dict (same shape as compile_source() returns).
# On a lex / parse / compile error a single line is printed instead:
# ERROR<TAB><message><TAB><line><TAB><col>
#
# Kabir strings cannot contain a literal open brace (it always
# starts interpolation), so the brace character is read from this
# very file: it is character 2 of line 1 above.

let brace_open = ""
"""


def _rename(line):
    for old, new in [("tokenize", "lex_tokenize"),
                     ("new_state", "lex_new_state"),
                     ("fail", "lex_fail"),
                     ("peek", "lex_peek"),
                     ("advance", "lex_advance")]:
        line = re.sub(r"\b" + old + r"\(", new + "(", line)
    return line


def main():
    compiler_path = os.path.join(SELFHOST, "compiler.kbr")
    with open(compiler_path, encoding="utf-8") as handle:
        current = handle.read()
    idx = current.index(MARKER)
    comp_section = current[idx:]

    with open(os.path.join(SELFHOST, "lexer.kbr"),
              encoding="utf-8") as handle:
        lex = handle.read().split("\n")
    start = next(i for i, line in enumerate(lex)
                 if line.startswith("let keywords"))
    end = next(i for i, line in enumerate(lex)
               if line.startswith("func num_text"))
    lex_span = [_rename(line) for line in lex[start:end]]
    for line in lex_span:
        assert not re.search(
            r"\b(new_state|fail|peek|advance|tokenize)\(", line), line

    with open(os.path.join(SELFHOST, "parser.kbr"),
              encoding="utf-8") as handle:
        par = handle.read().split("\n")
    pstart = next(i for i, line in enumerate(par)
                  if line.startswith("func in_list"))
    pend = next(i for i, line in enumerate(par)
                if line.startswith("func find_self_path"))
    par_span = par[pstart:pend]
    assert "find_self_path" not in "\n".join(par_span)

    out = (HEADER + "\n".join(lex_span) + "\n"
           + "\n".join(par_span).rstrip("\n") + "\n\n" + comp_section)
    with open(compiler_path, "w", encoding="utf-8") as handle:
        handle.write(out)
    print("regenerated %s (%d lines)"
          % (compiler_path, len(out.split("\n"))))


if __name__ == "__main__":
    main()
