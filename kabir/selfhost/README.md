# Self-hosting (Kabir 5.0.0, Leap 1)

Kabir compiles itself. The Python implementation in `kabir/` stays
the bootstrap compiler; these files are the Kabir implementation.

> 6.0.0 note: these sources were migrated for immutable-by-default —
> every reassigned binding is now `let mut` (113 conversions), and
> three function bodies (`substr` in `lexer.kbr` / `compiler.kbr`,
> `comp_emit` in `compiler.kbr`) were rewritten to stop assigning to
> parameters, which are immutable. The migration changes no bytecode:
> the compiler ignores the `mut` marker.

## Files

| File | What it is |
|---|---|
| `lexer.kbr` | The lexer in Kabir (4.0.0). Source path on stdin, prints one token per line. |
| `parser.kbr` | The parser in Kabir (5.0.0). Token JSON file path on stdin, prints the AST as one JSON object. |
| `compiler.kbr` | The compiler in Kabir (5.0.0). Source path on stdin, prints the main code dict as one JSON object. |
| `assemble_compiler.py` | Regenerates the embedded lexer + parser copies inside `compiler.kbr`. |
| `../kabir/selfhost.py` | Python bootstrap driver: canonical AST form, token JSON, subprocess runners. |
| `../tests/test_selfhost.py` | Differential tests for all three. |

## parser.kbr

A recursive-descent parser over a token stream, mirroring
`kabir/parser.py` exactly (same precedence, same AST shape).
It does not lex: feed it tokens with `kabir/selfhost.py`
(`tokens_to_json`), the same format `lexer.kbr` prints.

```console
$ python3 -c "from kabir.selfhost import tokens_to_json; print(tokens_to_json(open('examples/hello.kbr').read()))" > /tmp/hello.tokens.json
$ echo /tmp/hello.tokens.json | python3 -m kabir run kabir/selfhost/parser.kbr
```

On a parse error it prints one line instead:
`ERROR<TAB><message><TAB><line><TAB><col>`.

## Canonical AST

Both parsers serialize to the same JSON: every node is
`{"type": <Name>, "line": <int>, "col": <int>, ...fields}` with
keys in a fixed order (see `ast_to_dict` in `kabir/selfhost.py`).
Literal values stay JSON `int` / `float` / `string` / `bool` /
`null`, so `2` vs `2.0` still differ; absent optional parts are
`null`. `Block` carries `end_line` (its closing brace's line),
because the compiler uses it for `EXIT_SCOPE` lines. The test
comparison is a plain string comparison of `json.dumps` output --
`json_stringify` in Kabir is the same call, so the two byte
streams must agree exactly.

## compiler.kbr

A self-contained front end + bytecode compiler. It embeds a copy
of the lexer (functions renamed `lex_*`) and a copy of the parser,
then compiles the AST to the code dicts `kabir/compile.py` emits
(code dict v2: `name`, `params`, `instrs`, `consts`, `lines`,
`cols`, `locals`, `filename`, `is_async`), including slot
allocation, captured-name analysis, jump patching, `match` arms,
`spawn`, string interpolation (re-lexed and re-parsed inline),
and `async`/`await` checks. Its output is byte-identical to
`compile_source()` -- including for `lexer.kbr`, `parser.kbr`,
and `compiler.kbr` itself.

```console
$ echo examples/hello.kbr | python3 -m kabir run kabir/selfhost/compiler.kbr
```

`compiler.kbr` is assembled, not hand-copied: after editing
`lexer.kbr` or `parser.kbr`, run this to refresh the copies
(the compiler section, from the `# --- compiler` marker on,
is preserved verbatim):

```console
$ python3 kabir/selfhost/assemble_compiler.py
```

## What is NOT self-hosted (yet)

- The type checker (`kabir/checker.py`) and the VM (`kabir/vm.py`)
  are still Python only. `kabir run` still parses, checks, and
  compiles with Python, then runs the bytecode in the Python VM.
  The Kabir compiler's output is verified identical and runnable
  (`VM().run_code(...)` in `tests/test_selfhost.py`), but no
  `kabir run` flag yet wires it in as the compiler.
- The lexer + parser copies inside `compiler.kbr` are refreshed
  by `assemble_compiler.py`; they are not imported (a Kabir
  `import` would execute the modules' top-level `input()` /
  `print()` main code).
- Compile speed: the Kabir compiler is much slower than the
  Python one (compiling `compiler.kbr` itself takes ~43s vs well
  under a second in Python). That is the known VM speed ceiling,
  not a correctness gap.
