# Kabir Changelog

## 2.0.0 — 2026-11-12 — the biggest update yet

Kabir becomes a compiled language, with opt-in types, pattern matching,
and project tooling. No breaking change to existing untyped programs.

- **Bytecode compiler + stack VM** — `kabir run` now type-checks,
  compiles to bytecode, and executes on a stack VM instead of
  tree-walking the AST. The old interpreter still powers the REPL and
  stays available as `kabir run --interp`. Shared runtime semantics
  (`kabir/semantics.py`) keep both runtimes identical.
- **Opt-in static types** — `let x: number = 5`,
  `func add(a: number, b: number) -> number { ... }`. The checker
  (`kabir/checker.py`) reports type errors at compile time with
  file/line info. Types: `number`, `string`, `bool`, `array`, `dict`,
  `function`, `any`. Unannotated code is unchecked and unchanged.
- **`match` expressions** — Rust-style pattern matching:
  `match x { 0 => "zero", n if n > 1 => "big", _ => "?" }`.
  Literal, binding, and wildcard patterns, optional `if` guards.
  Works as an expression and as a statement.
- **Cargo-style projects** — `kabir new <name>` scaffolds a project
  (`Kabir.toml`, `src/main.kbr`, `.gitignore`); `kabir build` compiles
  it to `target/<name>.kbc`; `kabir run <dir>` runs a project and
  `kabir run <file.kbc>` runs compiled bytecode directly.
- **Tests** — 441 tests (211 in 1.2.0 + 230 new), including VM/interpreter
  output equivalence for every example, type-checker rules, match,
  and project round-trips.

## 1.2.0 — 2026-11-12

- **Formatter** — `kabir fmt` (idempotent, `--check` mode, comments
  preserved, precedence-aware parentheses).
- **REPL history** — up/down-arrow recall, persisted to
  `~/.kabir_history`.
- Fixed the "a array" error message — it now says "an array".
- Published to PyPI as `kabir-lang` 1.2.0.

## 1.1.0 — 2026-11-09

- **`import`** — use one `.kbr` file from another (relative paths,
  module cache, cycle detection).
- **Standard library** — `random` (`randint`, `randrange`, `choice`,
  `shuffle`, `seed`), math (`floor`, `ceil`, `sin`, `cos`, `tan`,
  `log`, `exp`, `pi`, `e`).
- **String methods** — `upper`, `lower`, `strip`, `split`, `join`,
  `contains`, `starts_with`, `ends_with`, `replace`.
- Published to PyPI as `kabir-lang` 1.1.0.

## 1.0.0 — 2026-11-04

- The language itself: `let`, `func`, `if`/`elif`/`else`, `while`,
  `for`, `break`/`continue`, `return`, arrays, dicts, string
  interpolation, closures, recursion, 15 core built-ins
  (`print`, `input`, `len`, `str`, `num`, `type`, `range`, `push`,
  `pop`, `sqrt`, `pow`, `abs`, `keys`, `int`, `float`).
