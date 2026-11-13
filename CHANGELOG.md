# Kabir Changelog

## 5.0.0 — 2026-11-13 — "The Next Big Leap" (all three leaps)

Commander Kakarot picked all three leaps for the next big release. No breaking change to existing programs.

- **Self-hosting compiler (Leap 1)** — the parser AND the compiler are now written in Kabir itself (`kabir/selfhost/parser.kbr`, `kabir/selfhost/compiler.kbr`, with the lexer embedded). The Kabir parser produces byte-identical ASTs to the Python parser (differential-tested on 41 grammar snippets + 11 error cases + the full 13-file corpus, including parsing itself), and the Kabir compiler produces byte-identical bytecode (26 snippets + 6 error cases + all 11 examples + `lexer.kbr`, `parser.kbr`, and `compiler.kbr` compiling itself in ~28 s). `kabir run --selfhost <file.kbr>` compiles with the Kabir compiler and runs on the VM. The Python implementation stays as the bootstrap compiler; the type checker and the VM are still Python-only. The Kabir compiler is much slower than Python's — correctness, not speed, is the point here.
- **Real registry (Leap 2)** — `kabir add` / `kabir publish` work end-to-end against a static JSON index (GitHub-hosted `kabir-registry` model, no server, no paid service, no credentials in Kabir). `add` resolves exact versions and `@latest` (numeric semver max), follows transitive dependencies (diamond dedup, cycle safety, conflict errors), verifies SHA-256, vendors into `vendor/<name>/<version>/`, and writes `Kabir.lock`. `publish` validates, builds a deterministic tarball, and stages the index update (`target/registry-stage/`) with the exact `git` commands to push — it pushes nothing. Index format and hosting steps are documented in `docs/REGISTRY.md`. The live index repo (`afrainkabirrafid/kabir-registry`) does not exist yet — creating it needs the user's approval; the default URL points at it. `add` vendors but does not yet wire packages into `import`.
- **True speed (Leap 3)** — measured, not promised. VM: inlined dispatch on integer opcodes with int fast paths, single-walk variable lookup, shared-env frames, scopeless blocks, bound string methods. fib(25): 3.23 s → 1.70 s (1.90×); primes to 10000: 2.12 s → 1.15 s (1.84×); 1M-iteration loop: 13.16 s → 6.34 s (2.08×). Native: cached/incremental compilation in `~/.kabir/cache/native` (keyed on source + runtime + compiler; `--no-cache` forces a rebuild) — repeat `kabir run --native` on fib(25): 1.44 s → 0.14 s wall clock (10.2×); native compile itself 1.32 s → 0.10 s warm (13.2×). The ~11× native loss on short programs is gone. First run on a fresh cache still costs ~1.4 s (one-time `rt.o` build). Startup-bound programs (hello, stdlib) did not move (~0.13 s) — that is Python startup + parse + type-check, not execution. Full tables in `~/workspace/kabir-benchmarks-5.0.0.md`.
- **Packaging fix (found in verification, not in the leaps)** — `kabir/native/rt.c` and `rt.h` are now shipped in the wheel/sdist (`package-data` + `MANIFEST.in`): a fresh `pip install` previously had no runtime files at all, so `kabir run --native` crashed with a raw `FileNotFoundError` traceback. The missing-files case now also fails with a friendly `KabirError` instead of a traceback (regression test in `tests/test_native_cache.py`). Same fix for the self-hosted compiler: the `.kbr` files moved from top-level `selfhost/` into the package as `kabir/selfhost/` (shipped as package data), so `kabir run --selfhost` works from a pip install, not just a source checkout — verified from a clean venv.
- **Tests** — 921 tests (849 in 4.1.0 + 72 new), including self-host parser/compiler (18), registry (26), native cache (11), VM speed (19), and CLI-5.0 (5) suites. One real bug caught by the suite during the build: the inlined CALL path let an async `_TaskSuspend` escape; fixed.

## 4.1.0 — 2026-11-12 — "Fearless & Everywhere" (combined 3.0.0 + 4.0.0 release)

The combined release, per the Commander's order (2026-11-12): everything from the 3.0.0 checkpoint below, plus the 4.0.0 waves (borrow checker, async/await, LSP + VS Code extension, WASM target, self-hosting step 1). No breaking change to existing programs.

### 3.0.0 — "Goes Native" (checkpoint, already landed)

Slot-based compiler, native compilation, a package registry, opt-in ownership, and a bigger standard library. No breaking change to existing programs.

- **Slot-based compiler (Wave 0)** — the bytecode format is now code dict v2: params and locals that are never captured by a nested function live in numbered slots (`LOAD_SLOT`/`STORE_SLOT`/`DEFINE_SLOT`); captured variables and top-level globals stay name-based. Honest finding: slots give **no** significant speedup (fib(25): slot VM 2.97 s vs old VM 2.70 s; per-instruction dispatch dominates, not variable lookup). Slots are the foundation for the native and WASM backends, not a speed feature.
- **Native compilation (Wave 1)** — `kabir build --native` / `kabir run --native` transpile Kabir to C99 and compile with the system `cc`. fib(25): 2.99 s (VM) → 0.007 s native; primes to 10000: 1.93 s → 0.008 s. Compile time (~1.3 s) dominates for short programs. Memory model is **native (arena)**, not refcounting — refcounting was cut deliberately (correct retain/release needs an ownership analysis that does not exist yet). Closures and `import` are rejected with clear compile-time errors.
- **Package registry (Wave 2)** — `kabir add <name>@<version>` fetches from a GitHub-based registry (`index.json`, `KABIR_REGISTRY` env var), verifies SHA-256, vendors into `vendor/<name>/<version>/`, and writes `Kabir.lock`. `kabir publish` builds a deterministic tarball and prints the registry-entry JSON; it pushes nothing. Exact versions only. The default registry URL does not exist yet, so `kabir add` refuses with a clear error until a real registry is set.
- **Ownership-lite (Wave 3)** — opt-in `let mut`: arrays/dicts move on assign, call, and return; use-after-move is a compile-time error; reassignment revives. Plain `let` is untouched. The immutable-by-default break was **not** taken (pending the user's explicit yes). Passing an owned value to `print`/`len` moves it too — strict for now, may be relaxed for known read-only built-ins later.
- **Standard library expansion (Wave 4)** — 9 new functions: `read_file`/`write_file`/`file_exists`, `json_parse`/`json_stringify`, `http_get`/`http_post` (10 s timeout), `now()`/`sleep()`. The checker now type-checks them per-name. Known quirk: a literal `{` cannot appear in a Kabir string (it starts interpolation), so JSON object text must come from `read_file`/`http_get`/`json_stringify`.
- **Tests** — 668 tests at the checkpoint (441 in 2.0.0 + 227 new), including slot, native, registry, ownership, stdlib-3, and CLI-3.0 suites.

### 4.0.0 waves

- **Borrow checker (Wave 0)** — `&x` shared borrows and `&mut x` mutable borrows, checked at compile time (NLL-lite: a borrow ends at its last use); at run time a borrow is the value, zero cost. Borrow types in annotations (`&number`, `&mut array`). No explicit lifetime parameters; borrows of `a[i]` borrow the whole container; no reborrowing; no dereference operator yet (a borrowed value cannot be used in arithmetic or where the plain type is expected); no interprocedural tracking beyond signatures.
- **Async/await + tasks and channels (Wave 1)** — `async func`, `await`, `spawn` (tasks), `chan` / `send` / `recv` (channels). Cooperative round-robin scheduler in the VM (1000-instruction quanta) with deadlock detection; `async`/`await` are real keywords. VM only — native async is out of scope (clear rejection); the interpreter/REPL rejects `async` with a clear error. Demo example `examples/async-demo.kbr` (two tasks + a channel).
- **LSP + VS Code extension (Wave 2)** — `kabir lsp`: LSP over stdio (JSON-RPC, `Content-Length` framing); diagnostics (parse + type check on open/change), completion, hover, go-to-definition, formatting via `kabir fmt`. VS Code extension in `editors/vscode/` (language id `kabir`, syntax highlighting, client launching `kabir lsp`, local-install README). Marketplace publishing is out of scope. The extension was not run inside real VS Code here.
- **WASM target (Wave 3)** — `kabir build --target wasm` produces `<name>.wasm` + an HTML harness. Direct WASM backend for the bytecode VM (no Emscripten); host imports `env.print_num` / `env.print_str` / `env.print_bool` / `env.print_nl` / `env.format_num`. Supported: numbers, bools, null, strings, arrays, loops, functions incl. recursion, `match`, core builtins — verified under Node v24 with VM-identical output (fibonacci, fizzbuzz). `import`, dicts, string methods, closures are rejected at build time; runtime errors trap without a message; numbers are f64 (exact to 2^53).
- **Self-hosting, step 1 (Wave 4)** — the lexer written in Kabir itself (`kabir/selfhost/lexer.kbr`), using `read_file` + string methods. Bootstrap check: token streams match the Python lexer exactly over the full test corpus (3,899 tokens, zero mismatches). The parser in Kabir was not written — a half-written parser would be dead weight; it stays a follow-up wave.
- **Tests** — 849 tests (441 in 2.0.0 + 408 new), including borrow (78), async (56), LSP (31), WASM (38), self-host (7), and CLI-4.0 (9) suites.

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
