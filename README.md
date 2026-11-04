# Kabir

![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)
![Python](https://img.shields.io/badge/Python-3.8%2B-blue.svg)
![Tests](https://img.shields.io/badge/tests-90%20passing-brightgreen.svg)

**Kabir is a small, friendly programming language, built from scratch in Python — no dependencies, no magic, just a lexer, a parser, and a tree-walking interpreter you can read in an afternoon.**

## What is Kabir?

Kabir is a complete little language: variables, numbers, strings with interpolation, arrays, dicts, `if`/`elif`/`else`, `while` and `for` loops, first-class functions with closures and recursion, and 15 built-in functions — all in about 1,700 lines of plain, dependency-free Python. Programs live in `.kbr` files and run with `python -m kabir run`.

It is a **tree-walking interpreter**: source code is lexed into tokens, parsed into an abstract syntax tree (AST), and the interpreter walks that tree and runs it directly. There is no JIT compiler and no native compilation — Kabir trades speed for clarity, so the whole implementation is easy to read, learn from, and hack on.

## Why Kabir exists

Kabir is named to honour the **Kabir family** of its creator, **Afrain Kabir Rafid**. It started as a from-scratch language-building project: no parser generators, no existing language renamed, no `eval`/`exec` — every token, grammar rule, and runtime behaviour was written by hand.

## Quickstart

You need **Python 3.8 or newer** (the code avoids newer-only features; it was developed and tested on Python 3.12.3 — Python 3.8 itself was not run here). There is nothing to install: Kabir uses only the Python standard library.

```console
$ git clone https://github.com/afrainkabirrafid/kabir.git
$ cd kabir
$ python -m kabir run examples/hello.kbr
Hello, Rafid!
Welcome to Kabir version 1.0.0.
2 + 3 = 5
```

That's it — clone, run. (See [Usage](#usage) for the `kabir` command itself.)

## Hello, World

Save this as `hello.kbr`:

```kabir
let name = "Rafid"
let language = "Kabir"

print("Hello, {name}!")
print("Welcome to {language} version 1.0.0.")
print("2 + 3 = {2 + 3}")
```

Run it:

```console
$ python -m kabir run hello.kbr
Hello, Rafid!
Welcome to Kabir version 1.0.0.
2 + 3 = 5
```

## Usage

All commands work both as `python -m kabir ...` (zero-install, from a fresh clone) and as `kabir ...` once the launcher or the package is on your `PATH` (see below).

| Command | What it does |
| --- | --- |
| `python -m kabir run <file.kbr>` | Run a Kabir program |
| `python -m kabir <file.kbr>` | Same — a bare `.kbr` argument is run directly |
| `python -m kabir repl` | Start the interactive REPL |
| `python -m kabir` | Start the REPL (no arguments) |
| `python -m kabir --help` | Show help (`-h` and `help` work too) |
| `python -m kabir --version` | Print the version (`-v` and `version` work too) |

```console
$ python -m kabir --version
kabir 1.0.0
```

### The `kabir` command

The repository ships an executable launcher at `bin/kabir`:

```console
$ ./bin/kabir --version
kabir 1.0.0
$ ./bin/kabir run examples/fizzbuzz.kbr
```

It lives in `bin/` for a real reason: a repo-root file named `kabir` cannot coexist with the `kabir/` package directory (same path), so the launcher is `bin/kabir`, not `./kabir`. If you install the package with pip (see `pyproject.toml`), the `kabir` command is installed on your `PATH` automatically.

### The REPL

```console
$ python -m kabir repl
Kabir 1.0.0 -- type 'exit' or press Ctrl-D to leave.
kabir> let x = 5
kabir> x + 1
6
kabir> "hi {x}"
hi 5
kabir> exit
```

Expression statements print their value; other statements print nothing. Multi-line input (for example a `func` definition) is supported — the REPL waits with a `... ` prompt until the braces balance. Type `exit` or `quit`, or press Ctrl-D, to leave.

### Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Program ran successfully (or `--help` / `--version` was shown) |
| `1` | A Kabir error (lex, parse, or runtime) or runaway recursion |
| `2` | A usage error — unknown command, missing file argument, unreadable file |

## A tour of the language

```kabir
# Comments start with # and run to the end of the line.

let name = "Rafid"
let scores = [64, 34, 25, 12, 22, 11, 90]

# Strings interpolate any expression inside { ... }.
print("Hello, {name}! You have {len(scores)} scores.")

# if / elif / else, while, and for over arrays, strings, dicts, or range().
for score in scores {
    if score >= 50 {
        print("{score} is a pass")
    } elif score >= 40 {
        print("{score} is close")
    } else {
        print("{score} needs work")
    }
}

let total = 0
for n in scores { total += n }
print("Average: {total / len(scores)}")

# Functions are first-class values; closures capture their surroundings.
func make_adder(n) {
    return func(x) { return x + n }
}
let add5 = make_adder(5)
print(add5(10))   # 15

# Dicts have string keys.
let user = {"name": "Rafid", "language": "Kabir"}
print(user["name"])
print(keys(user))
```

Expected output:

```text
Hello, Rafid! You have 7 scores.
64 is a pass
34 needs work
25 needs work
12 needs work
22 needs work
11 needs work
90 is a pass
Average: 36.857142857142854
15
Rafid
[name, language]
```

A few things worth noticing:

- **One number type.** There is no separate `int`/`float`: `5.0` prints as `5`, `7 / 2` is `3.5`, and `6 / 3` is `2` (an exact integer division stays an integer).
- **Truthiness is broad.** `0`, `""`, `[]`, `{}`, `false`, and `null` are falsy; everything else — including non-empty strings, arrays, and functions — is truthy.
- **`and` / `or` return values, not booleans.** `false or "fallback"` is `"fallback"`; `true and "yes"` is `"yes"`. They short-circuit: the right side is only evaluated when needed.
- **Negative indexes work.** `[10, 20, 30][-1]` is `30`, and `"abc"[-1]` is `"c"`.
- **Errors are friendly.** A mistake gives you `file:line:col`, the offending source line, and a caret — never a Python traceback. See [Errors](#errors) below.

The full reference — every construct, every built-in, each with a runnable example and its exact output — is in [docs/LANGUAGE.md](docs/LANGUAGE.md).

## Examples

All eight examples live in [`examples/`](examples/) and are covered by the test suite.

| File | What it shows |
| --- | --- |
| [`examples/hello.kbr`](examples/hello.kbr) | First program: variables and string interpolation |
| [`examples/fizzbuzz.kbr`](examples/fizzbuzz.kbr) | `for` over `range()`, `if`/`elif`/`else`, `%` |
| [`examples/fibonacci.kbr`](examples/fibonacci.kbr) | Recursion — `fib(n)`, first 15 numbers |
| [`examples/primes.kbr`](examples/primes.kbr) | `while` loop, helper function, primes up to 100 |
| [`examples/sorting.kbr`](examples/sorting.kbr) | Bubble sort, index assignment, arrays as values |
| [`examples/guessing-game.kbr`](examples/guessing-game.kbr) | Interactive loop with `input()` (secret is fixed at `42` — there is no random built-in yet) |
| [`examples/todo.kbr`](examples/todo.kbr) | In-memory todo CLI: `add`, `list`, `done`, `quit` |
| [`examples/calculator.kbr`](examples/calculator.kbr) | Interactive calculator; accepts `2+3` and `2 + 3`, handles division by zero |

Try the interactive ones by piping input, for example:

```console
$ printf '2 + 3\n10 / 4\nquit\n' | python -m kabir run examples/calculator.kbr
Calculator -- type like '2 + 3', or 'quit' to stop.
calc> 2 + 3 = 5
calc> 10 / 4 = 2.5
calc> Goodbye!
```

## Built-ins

Kabir ships 15 built-in functions, available in every program with no import:

| Function | Purpose |
| --- | --- |
| `print(...)` | Print values, space-separated, with a newline |
| `input(prompt?)` | Read a line from the user (prompt is optional) |
| `len(x)` | Length of a string, array, or dict |
| `str(x)` | Convert a value to a string |
| `num(x)` | Parse a string (or pass a number through) as a number |
| `type(x)` | Type name: `"number"`, `"string"`, `"boolean"`, `"null"`, `"array"`, `"dict"`, `"function"` |
| `range(n)` / `range(a, b)` | List of whole numbers: `0..n-1`, or `a..b-1` |
| `push(array, value)` | Append to an array (returns the array) |
| `pop(array)` | Remove and return the last item |
| `sqrt(x)` | Square root |
| `pow(x, y)` | `x` to the power of `y` |
| `abs(x)` | Absolute value |
| `keys(dict)` | List of a dict's keys, in insertion order |
| `int(x)` | Convert to an integer (truncates) |
| `float(x)` | Convert to a float |

Each one is documented with a runnable example in [docs/LANGUAGE.md](docs/LANGUAGE.md#built-in-functions).

## Errors

Kabir errors always look like this — location, source line, caret, plain message:

```text
/tmp/demo.kbr:1:9: Undefined variable 'y' -- it was never defined with 'let'. Check the spelling
  let x = y
          ^
```

```text
/tmp/demo.kbr:1:9: Division by zero -- the number after '/' cannot be 0
  print(1 / 0)
          ^
```

Strings even get a hint when you use the wrong quotes:

```text
/tmp/demo.kbr:1:7: Strings use double quotes, not single quotes -- write "hello" instead of 'hello'
  print('hi')
        ^
```

## Architecture

In plain words, running a program goes through four stages:

1. **Lexer** (`kabir/lexer.py`) — turns source text into tokens (`NUMBER`, `STRING`, `IDENT`, `KEYWORD`, `OP`, `PUNCT`, `NEWLINE`, `EOF`). Strings are tokenised into literal/interpolation parts here.
2. **Parser** (`kabir/parser.py`) — a hand-written recursive-descent parser that turns tokens into an AST, following a precedence ladder from `or` down to calls and indexing. `**` is right-associative; a `{` followed by a string key and `:` is read as a dict literal, not a block.
3. **AST** (`kabir/ast_nodes.py`) — one small class per node type (`LetStmt`, `IfStmt`, `BinaryOp`, …), each carrying its source line and column so errors can point at the exact spot.
4. **Interpreter** (`kabir/interpreter.py`) — walks the AST directly. Lexical scoping is a chain of `Environment` objects; closures capture their defining environment; `return`/`break`/`continue` are implemented as internal signals. Built-ins live in `kabir/builtins.py` and are registered into the global environment at startup.

The full internals guide — token types, the precedence ladder, environment/scoping rules, evaluator dispatch, error rendering, and the CLI/REPL flow — is in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Tests

Zero dependencies here too — the suite is plain `unittest`, run by a small runner:

```console
$ python tests/run_tests.py
Ran 90 tests in 0.579s

OK
```

The 90 tests cover the lexer, parser, interpreter, built-ins, errors, and every example in `examples/`. Run them before sending a pull request (see [CONTRIBUTING.md](CONTRIBUTING.md)).

## Honest limits

Kabir 1.0.0 is a small language, and it is honest about what it is not:

- **No JIT, no native compilation.** It is a tree-walking interpreter; it is fast enough for scripts and learning, not for number-crunching.
- **No `import`.** A program is a single `.kbr` file; there is no way to load another `.kbr` file yet.
- **No package manager, no standard-library modules.** The 15 built-ins are the whole library.
- **No file I/O, no random numbers, no dates.** The only way in or out is `print` and `input`.
- **Recursion depth is capped** at 500 nested calls, and there is no tail-call optimisation.
- **One number type** means no separate integer/float semantics; a power that overflows float range fails with a friendly error, while integer powers use arbitrary-precision integers.

## Roadmap

Roughly in order, and open to discussion:

1. **`import` for `.kbr` files** — load and reuse code across files.
2. **Standard-library modules** — starting with `random` (so the guessing game gets a real secret), then string helpers and math.
3. **A formatter** (`kabir fmt`) for canonical `.kbr` style.
4. **Pip packaging** — `pyproject.toml` is already in the repo; publishing to PyPI so `pip install kabir` gives you the `kabir` command.
5. **More of the language** — `else` on loops is not planned; instead: string methods, a `switch`-style construct only if it earns its place, and better REPL history.

Ideas and pull requests are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md).

## Contributing

Fork the repo, make a small focused change, add tests for any new behaviour, and run the suite (`python tests/run_tests.py`). The full guide — style rules (stdlib only, no `eval`/`exec`), how to add a built-in, and what a good PR looks like — is in [CONTRIBUTING.md](CONTRIBUTING.md).

## License

Kabir is released under the MIT License. See [LICENSE](LICENSE).

## Author

Created by **Afrain Kabir Rafid** — named to honour his Kabir family.
