# Kabir Architecture — how the interpreter works

This document is for anyone who wants to read, debug, or hack on Kabir's implementation. It describes the code as it actually is in version 1.1.0 — file by file, in the order a program flows through it. If this document and the code ever disagree, the code wins; please fix the document.

## The big picture

Kabir is a **tree-walking interpreter**: it does not compile to bytecode or to another language, and it never `eval`s or `exec`s anything. Running a program is four stages:

```text
source text ──► Lexer ──► tokens ──► Parser ──► AST ──► Interpreter ──► output
   kabir/lexer.py      kabir/parser.py   kabir/ast_nodes.py   kabir/interpreter.py
```

- `kabir/lexer.py` (~206 lines) — source text → token stream.
- `kabir/parser.py` (~400 lines) — tokens → AST (recursive descent).
- `kabir/ast_nodes.py` (~167 lines) — one small class per AST node.
- `kabir/interpreter.py` (~668 lines) — walks the AST and runs it.
- `kabir/builtins.py` (~237 lines) — the 15 core built-in functions.
- `kabir/stdlib.py` (~265 lines) — the 12 standard-library functions (`random`, `math`) and the `STRING_METHODS` table.
- `kabir/errors.py` (~49 lines) — the `KabirError` type and its rendering.
- `kabir/cli.py` (~165 lines) — `run` / `repl` / `--help` / `--version`.
- `kabir/__init__.py` — version, author, and the public API (`Interpreter`, `parse`, `tokenize`, `KabirError`).
- `kabir/__main__.py` — the `python -m kabir` entry point (calls `kabir.cli:main`).

Everything is standard library only. There are no third-party dependencies, and no generated code — the lexer and parser are hand-written.

## 1. Lexer (`kabir/lexer.py`)

`Lexer(source, filename)` scans the source character by character and returns a list of `Token` objects. A `Token` has four fields: `kind`, `value`, `line`, `col` (1-based line and column, so errors can point at the exact spot).

### Token kinds

| Kind | Value | Examples |
| --- | --- | --- |
| `NUMBER` | Python `int` or `float` | `42` → `42`, `3.5` → `3.5` |
| `STRING` | List of `("lit", text)` / `("expr", source)` parts | `"a{x}b"` → `[("lit", "a"), ("expr", "x"), ("lit", "b")]` |
| `IDENT` | The name | `score`, `my_var` |
| `KEYWORD` | The keyword text | `let`, `if`, `true`, `and`, … |
| `OP` | The operator text | `+`, `**`, `==`, `+=`, … |
| `PUNCT` | The punctuation character | `(`, `)`, `{`, `}`, `[`, `]`, `,`, `;`, `:`, `.` |
| `NEWLINE` | `"\n"` | Statement separator |
| `EOF` | `None` | End of input |

Keywords are recognised in `_ident()`: an identifier whose text is in the `KEYWORDS` set (`let`, `func`, `return`, `if`, `elif`, `else`, `while`, `for`, `in`, `break`, `continue`, `true`, `false`, `null`, `and`, `or`, `not`, `import`) becomes a `KEYWORD` token; anything else is an `IDENT`. Identifiers start with a letter or `_` and continue with letters, digits, or `_`. (`import` joined the set in 1.1.0; it is the one reserved-word change of that release.)

### Numbers

`_number()` reads digits, then an optional `.` followed by digits. `42` becomes `int(42)`; `3.5` becomes `float(3.5)`. A `.` not followed by a digit is not part of the number — `3.` lexes as `NUMBER(3)` followed by `PUNCT(.)` (`.` joined `PUNCT` in 1.1.0 for string methods) and fails later in the parser, since a bare `.` after a number is not a valid expression. Before 1.1.0 it failed in the lexer with `Unexpected character '.'`. There is no exponent notation, no hex, no underscores in numbers.

### Strings and interpolation

Strings are lexed in `_string()`, and this is where Kabir's most unusual design lives: **a string token's value is not a plain string but a list of parts** — `("lit", text)` for literal text and `("expr", source)` for an interpolation expression. The lexer does the brace-matching for `{ ... }` itself, tracking nesting depth and whether it is inside a nested string, so an expression like `{len("abc")}` or `{"a": {"b": 1}}` lexes correctly.

Rules enforced here:

- **Double quotes only.** A `'` is a friendly error, not a string: *"Strings use double quotes, not single quotes"*.
- **No multi-line strings.** A newline before the closing quote is an *"Unterminated string"* error.
- **Five escapes only:** `\n`, `\t`, `\r`, `\"`, `\\`. Anything else is an *"Unknown escape sequence"* error.
- **Empty interpolation is an error** (`{}`), and an unterminated `{` is an error. The expression text is stored raw and parsed later, at run time, by the interpreter (see §4).

### Comments, whitespace, operators

- `#` starts a comment that runs to the end of the line (handled in the main `tokenize()` loop, before any other rule).
- Spaces, tabs, and carriage returns are skipped; newlines become `NEWLINE` tokens (they are statement separators, not skipped).
- Two-character operators are tried before one-character ones: `MULTI_OPS = ("**", "==", "!=", "<=", ">=", "+=", "-=")`, then `SINGLE_OPS = set("+-*/%=<>")`. Anything else is an *"Unexpected character"* error.

## 2. Parser (`kabir/parser.py`)

`parse(source, filename)` tokenizes and returns a `Program` node. The parser is a hand-written **recursive-descent** parser: one method per grammar rule, with a one-token lookahead (`self.current`) plus direct index peeks where a decision needs two tokens.

### Precedence ladder

Lowest to highest, exactly as the code reads:

```text
or  →  and  →  equality (== !=)  →  comparison (< <= > >=)
    →  additive (+ -)  →  multiplicative (* / %)
    →  unary (-, not)  →  power (**)
    →  postfix (call, index, member)  →  primary
```

Each level is a method (`_or`, `_and`, `_equality`, `_comparison`, `_additive`, `_multiplicative`, `_unary`, `_power`, `_postfix`, `_primary`). The binary levels loop left-associatively; `**` is the exception.

### The `**` wrinkle

`_power()` is **right-associative**: after parsing the left side with `_postfix()`, if it sees `**` it parses the right side with `_unary()` (not `_power()`), so `2 ** 3 ** 2` becomes `2 ** (3 ** 2)`. Calling `_unary()` for the right side also allows negative exponents (`2 ** -1`). A side effect of the ladder's shape is that unary minus binds *less* tightly than `**`: `-2 ** 2` parses as `- (2 ** 2)` = `-4`. That is documented behaviour in the language reference, not a bug to "fix" without also updating the docs and tests.

### Statements

`_statement()` dispatches on the current token:

- `let` → `_let()`: `let <name> = <expression>`. The `=` is required.
- `func` → `_func_decl_or_expr_stmt()`: a named declaration if an `IDENT` follows, otherwise an anonymous function expression. Note the same method is reused from `_primary()` for `func` expressions — one code path serves both.
- `return` → an optional expression follows (absent before a newline, `;`, `}`, or EOF).
- `if` → `_if()`: one or more `(condition, block)` branches plus an optional `else` block. Newlines between `}` and `elif`/`else` are allowed — the parser saves its position, skips newlines, and restores it if neither keyword follows.
- `while` → condition, then a block.
- `for` → `for <var> in <expression> <block>`.
- `break` / `continue` → bare statements (the loop check happens at run time, not here).
- `import` → `_import()`: the keyword plus a plain string-literal path, producing an `ImportStmt`. Two checks happen here, at parse time: an interpolated path is an error (*"import paths must be plain strings, not interpolated ones"* — the same rule as dict keys), and a path not ending in `.kbr` is an error (*'import "…" must name a .kbr file'*).
- `{` → **the dict-literal disambiguation**: the parser peeks at the next two tokens, and if they are a `STRING` followed by `:`, the `{` starts a dict literal expression (`{"a": 1}`), not a block. Otherwise it is a block statement. This is the only two-token lookahead in the parser, and it is why a statement can never be a bare dict literal with a non-string first key — dict keys are strings by definition.
- Anything else → `_expr_or_assign_stmt()`: parse an expression, and if `=`, `+=`, or `-=` follows, it must be an assignment whose target is a `Var` or an `Index` — anything else is an error (*"Only a variable or an index can be assigned to"*). Assignment is therefore a statement, never an expression.

`_block()` expects `{`, skips separators, parses statements until `}`, and reports a missing `}` with the position of the opening brace.

### Expressions

- `_primary()` handles numbers, string tokens (becoming `StringLiteral` nodes holding the lexer's parts list), `true`/`false`/`null` (becoming `Literal` nodes), identifiers, parenthesised expressions, array literals, dict literals, and anonymous `func` expressions. Dict keys must be string tokens whose parts are a single literal part — an interpolated key is a parse error.
- `_postfix()` loops over `(` argument lists `)`, `[` index `]` suffixes, and `.` member suffixes, so `f(1)(2)`, `a[0][1]`, `f(x)[0]`, `"abc".upper()`, and `" a ".strip().upper()` all parse. A `.` suffix consumes the dot, expects an `IDENT` (the method name — keywords are not valid method names), and wraps the expression in a `Member` node. Argument lists and index brackets allow newlines inside them.
- Separators: `_skip_separators()` consumes `NEWLINE` and `;` tokens; the top-level `parse()` loop and `_block()` call it between statements, so newlines and semicolons are interchangeable as separators.

### Error reporting in the parser

`_fail()` raises `KabirError` via `error_at()` with the offending token's line/column and the source lines, so every parse error renders as `file:line:col` + source line + caret. Typical messages: *"Expected … but found …"*, *"Unexpected … — an expression was expected here"*, *"Expected '}' to close this block"*.

## 3. AST (`kabir/ast_nodes.py`)

Every node subclasses `Node`, which carries `line` and `col`. The tree is plain data — no methods, no visitor pattern; the interpreter dispatches on node class name (see §4).

Statements: `Program`, `Block`, `LetStmt`, `AssignStmt` (target + `=`/`+=`/`-=` + value), `ExprStmt`, `FuncDecl` (`name` may be `None` for anonymous functions), `ReturnStmt` (value may be `None`), `IfStmt` (a list of `(condition, Block)` branches plus an optional `else_body`), `WhileStmt`, `ForStmt`, `BreakStmt`, `ContinueStmt`, `ImportStmt` (`path` — the literal string from the `import` statement).

Expressions: `Literal`, `StringLiteral` (a `parts` list of `("lit", text)` / `("expr", source)`), `Var`, `ArrayLiteral`, `DictLiteral` (a list of `(key-string, value-node)` pairs), `BinaryOp`, `UnaryOp` (`-` or `not`), `Call`, `Index`, `Member` (`object` + `name` — the receiver expression and the method name after a `.`).

Because every node remembers where it came from, a runtime error can point at the exact expression that failed — the interpreter never has to guess.

## 4. Interpreter (`kabir/interpreter.py`)

`Interpreter(source, filename)` owns the global environment and runs programs. `run_source(source, filename)` lexes, parses, and runs in one call; `run(program)` runs an already-parsed program; the REPL calls `parse_source` itself and then `run_stmt` per statement so one bad line does not kill the session.

### Values

Kabir values *are* Python values, with a fixed mapping:

| Kabir value | Python value |
| --- | --- |
| number | `int` or `float` (never `bool` — booleans are checked first everywhere) |
| string | `str` |
| boolean | `bool` |
| null | `None` |
| array | `list` |
| dict | `dict` (string keys, insertion-ordered) |
| function | `KabirFunction` (user-defined) or `BuiltinFunction` (from `builtins.py`) |

`format_value()` is the single place that turns a value into printed text: `None` → `"null"`, booleans → `"true"`/`"false"`, floats that are whole numbers → their integer form (`5.0` → `"5"`), strings → themselves (no quotes — which is why a string *inside* an array or dict also prints unquoted: `[1, two]`), arrays → `"[a, b]"`, dicts → `'{"k": v}'` with keys always quoted, functions → `"<function name>"` / `"<builtin function name>"`. `print`, `str`, and string interpolation all go through `format_value`, so formatting is consistent everywhere.

### Dispatch

`run_stmt()` and `compute()` dispatch by class name: `getattr(self, "_run_" + type(node).__name__)` / `"_compute_" + ...`. Adding a new statement or expression type means adding a node class in `ast_nodes.py`, a parse rule in `parser.py`, and a `_run_…` / `_compute_…` method here — three places, no registry to update.

### Environments and scoping

```python
class Environment:
    def __init__(self, parent=None):
        self.vars = {}
        self.parent = parent
```

Scoping is a chain of `Environment` objects. `define` writes to the current environment; `lookup` and `has` walk up the chain; `assign` walks up and writes to the environment that already owns the name (returning `False` if no environment owns it — that is the "never defined" error). (There is also a `get` method returning a `(value, found)` pair; it is currently unused by the evaluator, which uses `lookup`/`has`/`assign`.)

The interpreter keeps an explicit environment stack (`self._stack`, starting with `self.globals`):

- **Blocks** (`run_block`) push a fresh child environment, run the statements, and pop it in a `finally` — so a `let` inside a block never leaks, and an exception still restores the stack.
- **`for` loops** create one fresh child environment per iteration holding just the loop variable, then run the body as a block (which pushes *another* child environment for the body's own `let`s).
- **Function calls** create a child of the function's *captured* environment (`callee.closure_env`), bind the parameters, and run the body in it. This is what makes closures work: the environment a function closes over is the one that was current when the function was *defined*, not when it is called.
- **The globals** hold the 15 core built-ins (installed once in `__init__` from `build_builtins()`), the 12 standard-library functions (from `build_stdlib()`), and the `pi` / `e` constants. A program-level `let` with the same name as a built-in shadows it in the global environment — by design.

### Control flow via signals

`return`, `break`, and `continue` are implemented as private exceptions, not as return values:

- `_ReturnSignal(value)` — raised by `_run_ReturnStmt`, caught in `call()`; it also carries the "return outside a function" check, via a `function_depth` counter.
- `_BreakSignal` / `_ContinueSignal` — raised by `_run_BreakStmt` / `_run_ContinueStmt`, caught by the loop runners; a `loop_depth` counter rejects them outside loops.

This keeps the evaluator's normal path free of "did that statement return?" bookkeeping. Recursion depth is capped separately: `call()` refuses to run past `MAX_CALL_DEPTH = 500` nested calls, and the CLI additionally catches Python's own `RecursionError` (the tree-walker itself recurses on deeply nested expressions).

### Operators and truthiness

- `is_truthy()`: `None`/`False` are falsy; numbers are falsy iff `0`; strings, arrays, and dicts are falsy iff empty; everything else (including functions) is truthy. Note that `bool` is checked before `int` throughout, because in Python `isinstance(True, int)` is `True` — forgetting that check would make `true + 1` equal `2`.
- `and`/`or` short-circuit in `_compute_BinaryOp` *before* evaluating the right side, and return operand values, not booleans.
- `kabir_equal()`: booleans only equal booleans; `None` only equals `None`; numbers compare numerically (`1 == 1.0`); lists and dicts compare element-wise/key-wise, recursively; functions compare by identity.
- `_binary()`: `+` accepts number+number, string+string, list+list; `-`, `*`, `/`, `%`, `**` need numbers. Division is special: two `int`s that divide exactly return an `int` (`6 / 3` → `2`); otherwise `/` returns a float. Division (and `%`) by zero is a friendly error, and `**` overflow is caught (`OverflowError`/`ZeroDivisionError`) and turned into *"That power is too large to calculate"*.
- Ordering comparisons accept number+number or string+string only.
- Indexing: arrays and strings accept integer indexes (negative indexes wrap from the end); dicts accept string keys and report a missing key together with the keys that exist. Index *assignment* (`_assign_index`) mirrors the read rules, and also supports negative array indexes.

### Interpolation at run time

`_compute_StringLiteral` walks the parts list. For an `("expr", source)` part it re-parses the expression text with `_parse_source` and evaluates it (`_compute_interpolation`), after re-pointing the inner nodes' line/column at the string's own location (`_reline`) so an error inside `{ ... }` points at the string, not at line 1 of the fragment. Exactly one expression statement is allowed inside the braces.

### Imports and the module cache

`_run_ImportStmt()` hands the statement's path to `_load_module(path, node)`, which does the whole import:

- **Resolution.** Relative to the importing file's directory (`os.path.dirname(os.path.abspath(self.filename))`); when there is no importing file — the REPL (`"<repl>"`), `"<stdin>"`, tests — relative to the process working directory. The canonical key is `os.path.realpath(os.path.abspath(resolved))`, so `./utils.kbr`, `utils.kbr`, and `sub/../utils.kbr` are one module.
- **Cycle check.** `self._importing` is the stack of canonical paths currently being executed. If the resolved path is already on it, `_load_module` raises `KabirError` at the importing statement with `Import cycle detected: <chain>`, where the chain is the basenames of the stack plus the repeated file, joined with ` -> `. The entry file is loaded by `run_source`, not by `_load_module`, so it is never on the stack: a cycle through the entry file is detected one re-entry later and the chain starts with the first imported file (see the language reference for the exact example).
- **Cache.** `self.modules` maps canonical path → exported-bindings dict. On a hit, the cached names are `define()`d into the current environment and no file I/O happens. The cache is per-`Interpreter` (created in `__init__`, next to `self.globals`) and is filled only after the module executes successfully — a file that raises is never cached.
- **Reading.** The file is read as UTF-8, exactly as `cli.run_file()` does. `FileNotFoundError`, `UnicodeDecodeError`, and other `OSError`s are converted to `KabirError` at the `import` statement (`Cannot import "…" -- …`), so no traceback can leak — `cli.py` catches only `KabirError` and `RecursionError`. A syntax error inside the imported file propagates unchanged (it already carries the imported file's location).
- **Execution.** The module runs in a fresh `Environment` whose parent is the interpreter's `globals` (so it sees all built-ins but not the importer's locals), via the same `run_stmt` loop as `run()`. Before executing, `_load_module` saves the current file state with `_save_file_state()` and sets `filename` / `source` / `lines` to the imported file's, resets `function_depth` and `loop_depth` to 0 (so a top-level `return` in the module errors instead of returning from an importing function), pushes the path onto `self._importing`, and pushes the module env; everything is restored in a `finally` (`_pop_env`, `_importing.pop()`, `_restore_file_state`). Without the save/restore, an error on the line *after* an import would point at the imported file's lines. After a successful run, the exports are a snapshot of the module env's own `vars` dict (which, because the parent is `globals`, holds only the module's own bindings) — that dict is cached and copied into the importer's current environment.

### String methods (`_compute_Member`)

`_compute_Member(expr)` evaluates the receiver. If it is a `str` and the name is in the `STRING_METHODS` table (imported from `kabir/stdlib.py`), it returns a bound method as an existing `BuiltinFunction` — a closure capturing the receiver — so `Call(Member(…), args)` works with zero changes to `call()`, and the bound method is first-class: it can be stored (`let u = "abc".upper`), passed around, and printed (`"<builtin function upper>"`). If the receiver is a `str` but the name is not in the table, the error names the method (`No method '.frobnicate' on a string …`). A dict receiver errors with an explicit pointer at brackets (`Cannot use .name on a dict -- write dict["name"] instead …`); any other receiver type errors naming the type (`Cannot use .upper on a number -- only strings have methods`). The string-only restriction is enforced here, at run time — the parser accepts `expr.name` for any receiver.

### Errors

`Interpreter.error(node, message)` raises `KabirError` at the node's line/column. All user-facing errors go through this path (or the lexer's/parser's equivalent), so a Kabir program can never produce a raw Python traceback for a language-level mistake.

## 5. Built-ins (`kabir/builtins.py`)

Each built-in is a plain Python function `(interp, node, args) -> value`, wrapped in a `BuiltinFunction(name, impl)` and registered in the `BUILTINS` dict; `build_builtins()` returns a fresh `{name: BuiltinFunction}` mapping for each interpreter. To add a built-in, write one function, add one line to `BUILTINS`, document it in `docs/LANGUAGE.md`, and add tests in `tests/test_builtins.py` — no other wiring exists.

Conventions inside the file:

- `_need(interp, node, args, name, count)` enforces an exact argument count with a uniform message.
- `_is_number(v)` is `int`/`float` excluding `bool`; `_type_name(v)` returns the Kabir type name (`"number"`, `"string"`, …) used by both `type()` and error messages.
- Errors are raised through `interp.error(node, …)` so they carry the call site's location.
- `print` joins `format_value` of each argument with spaces and returns `None`; `input` wraps Python's `input()` and turns `EOFError`/`KeyboardInterrupt` into Kabir errors; `range` requires whole numbers and takes 1 or 2 arguments (no step); `push` mutates and returns the array; `pop` errors on an empty array; `num`/`int`/`float` parse strings (stripping whitespace) and refuse booleans (`int`/`float`) — `int("3.9")` goes through `float` first and truncates to `3`; `sqrt`/`pow`/`abs` delegate to `math` / Python operators; `keys` returns `list(dict.keys())`, i.e. insertion order.

The only import is `math` (for `sqrt`). Everything else is plain Python.

## Standard library (`kabir/stdlib.py`)

The standard library lives in its own file so `builtins.py` stays the core. It follows the exact same pattern — plain Python functions `(interp, node, args) -> value`, wrapped in `BuiltinFunction`, collected in a `STDLIB` dict, exposed by `build_stdlib()` — and reuses the helpers from `builtins.py` (`_need`, `_is_number`, `_type_name`) rather than duplicating them. `Interpreter.__init__` merges `build_stdlib()` into the globals right after `build_builtins()`; `stdlib.py` does not import the interpreter (no circular import).

- **The 12 functions:** `randint`, `randrange`, `choice`, `shuffle`, `seed` (backed by Python's `random`), and `floor`, `ceil`, `sin`, `cos`, `tan`, `log`, `exp` (backed by Python's `math`). `sqrt`/`pow`/`abs` stay in `builtins.py` — they were already global, documented, and tested there, and moving them would be churn with no user-visible benefit.
- **RNG placement:** one `random.Random()` instance per `Interpreter` (`self._random`, created in `__init__`). Every random function goes through `interp._random`, so two interpreters — and therefore two tests — are independent. A module-level RNG would let one test's `seed()` change another's sequence.
- **`pi` / `e`:** defined in `__init__` as globals (`self.globals.define("pi", math.pi)`), shadowable by `let` like any built-in name.
- **String methods:** the `STRING_METHODS` table lives here too — `{"upper": _str_upper, …}`, nine plain functions `(interp, node, receiver, args) -> value` following the same conventions. The interpreter imports the table; see §4 for how `_compute_Member` dispatches on it.

## 6. Errors (`kabir/errors.py`)

`KabirError` carries `message`, `filename`, `line`, `col`, and `source_line`. `render()` produces the canonical format:

```text
file:line:col: message
  <source line>
  <spaces>^
```

`error_at(message, filename, lines, line, col)` builds one from a list of source lines. Lexer, parser, and interpreter all build errors through this helper, so the format is identical no matter where the error comes from. `__str__` is `render()`, so printing the exception prints the friendly form.

## 7. CLI and REPL (`kabir/cli.py`)

`main(argv=None)` is the entry point (also the `[project.scripts]` target — see `pyproject.toml`). Dispatch, in order:

1. No arguments → `repl()`.
2. `-h` / `--help` / `help` → print `HELP_TEXT`, exit `0`.
3. `-v` / `--version` / `version` → print `kabir <version>`, exit `0`.
4. `run <file>` → `run_file(path)`; exactly one file, else a usage error (exit `2`).
5. `repl` → `repl()`; no extra arguments.
6. An argument ending in `.kbr` → `run_file` directly (a convenience shortcut not shown in `--help`).
7. Anything else → usage error (exit `2`), help text printed to stderr.

`run_file()` reads the file as UTF-8, runs it through `Interpreter.run_source`, and on `KabirError` prints `err.render()` to stderr and returns `1`. It also catches `RecursionError` (deep expression nesting) and reports it as the recursion error. A missing or unreadable file is a usage error (exit `2`), not a Kabir error.

`repl()` prints the banner, then loops on `input()` with prompt `kabir> ` (or `... ` while a multi-line construct is open). Multi-line input is brace-aware: `_braces_balanced()` counts `{`/`}` outside strings and comments, and the REPL keeps reading until the depth returns to zero — that is how you type a `func` definition across lines. `exit`/`quit` (typed on an empty buffer), Ctrl-D (`EOFError`), and Ctrl-C (`KeyboardInterrupt`, which clears the buffer) all leave cleanly. Each parsed statement is run with `run_stmt` against one long-lived `Interpreter`, so variables persist between lines; if a statement is an `ExprStmt`, its value is printed with `format_value` — that is why typing `x + 1` in the REPL prints `6`.

## 8. Testing

`python tests/run_tests.py` discovers `tests/test_*.py` with plain `unittest` (verbosity 2) and exits non-zero on any failure. The 176 tests are split by layer, mirroring this document:

| File | Covers |
| --- | --- |
| `tests/test_lexer.py` | Tokens, numbers, strings, interpolation, errors |
| `tests/test_parser.py` | Precedence, statements, dict disambiguation, errors |
| `tests/test_interpreter.py` | Values, scoping, closures, loops, recursion, operators |
| `tests/test_builtins.py` | All 15 core built-ins, argument and type errors |
| `tests/test_stdlib.py` | The 12 standard-library functions, `pi`/`e`, RNG independence |
| `tests/test_string_methods.py` | All 9 string methods, chaining, method values, member errors |
| `tests/test_import.py` | `import`, module cache, cycles, import errors, the import example |
| `tests/test_errors.py` | Error format (location, source line, caret) |
| `tests/test_examples.py` | Every file in `examples/` runs and produces its expected output |

There is no test framework dependency and no mocking — tests drive the real `Interpreter` and compare real output.

## 9. Where to change things

| You want to… | Touch… |
| --- | --- |
| Add a keyword | `KEYWORDS` in `lexer.py`, a parse rule in `parser.py`, a node in `ast_nodes.py`, an evaluator method in `interpreter.py`, docs, tests |
| Add a built-in | One function + one `BUILTINS` line in `builtins.py`, docs, tests |
| Add a standard-library function | One function + one `STDLIB` line in `stdlib.py`, docs in the Standard library section, tests in `tests/test_stdlib.py` |
| Add a string method | One function + one `STRING_METHODS` entry in `stdlib.py`, docs, tests in `tests/test_string_methods.py` |
| Add `import` | A new statement type end-to-end (lexer keyword, AST node, parser rule, interpreter `_load_module` + module cache + file stack), docs, tests in `tests/test_import.py` |
| Add an operator | Lexer op tables, a parser precedence level, `_binary()`/`_compute_UnaryOp`, docs, tests |
| Change error wording | The message string where it is raised — rendering is centralised in `errors.py` |
| Change number formatting | `format_value()` in `interpreter.py` — it is the single formatting path |
