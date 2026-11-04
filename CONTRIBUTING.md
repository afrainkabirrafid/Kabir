# Contributing to Kabir

Thank you for wanting to help with Kabir! This guide covers how to set up, how to make a change, and what a good pull request looks like. Kabir is a small project on purpose — small, focused contributions are the best kind here.

## Getting started

Kabir needs **Python 3.8 or newer** (developed and tested on Python 3.12.3) and nothing else — no third-party dependencies, no build step.

```console
$ git clone https://github.com/afrainkabirrafid/kabir.git
$ cd kabir
$ python -m kabir run examples/hello.kbr
Hello, Rafid!
Welcome to Kabir version 1.0.0.
2 + 3 = 5
```

## Running the tests

The test suite is plain `unittest`, run by a small runner. Run it before you start work (to confirm a clean baseline) and again before you send a pull request:

```console
$ python tests/run_tests.py
Ran 90 tests in 0.579s

OK
```

The suite is split by layer — `tests/test_lexer.py`, `tests/test_parser.py`, `tests/test_interpreter.py`, `tests/test_builtins.py`, `tests/test_errors.py`, and `tests/test_examples.py` (which runs every file in `examples/`). If you change behaviour in one layer, the tests for that layer are the first place to look. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for how the layers fit together.

## How to contribute

1. **Fork** the repository and create a branch from `main` with a descriptive name, e.g. `fix-string-index-error` or `builtin-random`.
2. **Keep the change small and focused.** One pull request, one idea. A new built-in, a bug fix, a documentation improvement, or a new example are all good sizes. A new language feature plus a refactor plus a formatting change is three pull requests.
3. **Add tests with features.** New behaviour needs new tests in the matching `tests/test_*.py` file; a bug fix needs a test that fails before the fix and passes after it. If the change affects an example, update `tests/test_examples.py` expectations too.
4. **Update the docs.** If user-visible behaviour changes, update [docs/LANGUAGE.md](docs/LANGUAGE.md) (with a runnable example and its exact output — every code block there is verified by running it) and the [README](README.md) if the change belongs there. Internal changes go in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
5. **Run the full suite** (`python tests/run_tests.py`) and confirm all 90+ tests pass.
6. **Open the pull request** with a short description of what changed, why, and which tests cover it.

## Style

Kabir's style is deliberately plain, so the implementation stays readable for learners:

- **Standard library only.** Do not add third-party dependencies — not for the interpreter, not for the tests, not for tooling. If you think a dependency is unavoidable, open an issue first and make the case.
- **No `eval` / `exec`.** The interpreter must never execute dynamically generated Python code. This is a hard rule, not a preference.
- **Plain, explicit Python.** Short functions, clear names, no clever one-liners. Code that a reader can follow in one pass is better than code that is shorter. The existing files are the style guide — match them.
- **Friendly errors.** Every new error path must produce a Kabir error (`file:line:col` + source line + caret + plain message) through `errors.error_at` / `Interpreter.error`. Never let a Python exception or traceback reach the user for a language-level mistake. If you are unsure what a message should say, look at the existing messages in `kabir/builtins.py` and `kabir/interpreter.py` and match their tone: what went wrong, and what to do instead.
- **Keep the public surface small.** New syntax or built-ins should earn their place. Before adding a feature, check the [roadmap](README.md#roadmap) and consider opening an issue to discuss it — especially for anything that changes the grammar.
- **Docstrings on modules.** Each `kabir/*.py` module starts with a short docstring explaining what it does. Keep those accurate when you change a module.

## Adding a built-in

This is the most common contribution, and it needs no other wiring:

1. Write a function `(interp, node, args) -> value` in `kabir/builtins.py`.
2. Add one line to the `BUILTINS` dict.
3. Document it in [docs/LANGUAGE.md](docs/LANGUAGE.md) with a runnable example and exact output.
4. Add tests in `tests/test_builtins.py`, including the argument-count and wrong-type errors.

Follow the existing conventions: use `_need()` for argument counts, `_is_number()` / `_type_name()` for type checks, and `interp.error(node, …)` for failures.

## Adding syntax

A new construct touches four places, in order: a node class in `kabir/ast_nodes.py`, a parse rule in `kabir/parser.py`, an evaluator method (`_run_…` / `_compute_…`) in `kabir/interpreter.py`, and tests plus documentation. If the construct needs a new keyword, add it to `KEYWORDS` in `kabir/lexer.py` first. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md#9-where-to-change-things) for the map.

## Examples

New examples go in `examples/` with a header comment explaining what the file shows, and they are picked up automatically by `tests/test_examples.py`. Keep them short, correct, and runnable with no input where possible; interactive examples (like `examples/guessing-game.kbr`) should say so in their header comment.

## Reporting issues

Open an issue with:

- The Kabir version (`python -m kabir --version`) and your Python version.
- The smallest `.kbr` program that shows the problem.
- What you expected, and what actually happened (paste the full error output).

## License

By contributing, you agree that your contributions will be licensed under the MIT License, the same license as the project (see [LICENSE](LICENSE)).
