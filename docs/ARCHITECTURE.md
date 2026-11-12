# Kabir Architecture — how the compiler, VM, and interpreter work

This document is for anyone who wants to read, debug, or hack on Kabir's implementation. It describes the code as it actually is in version 2.0.0 — file by file, in the order a program flows through it. If this document and the code ever disagree, the code wins; please fix the document.

## The big picture

Kabir is a **compiled language**: programs are type-checked, compiled
to bytecode, and run on a stack virtual machine. It never `eval`s or
`exec`s anything. Running a program is six stages:

```text
source ──► Lexer ──► tokens ──► Parser ──► AST ──► Checker ──► Compiler
  kabir/lexer.py      kabir/parser.py   kabir/ast_nodes.py  kabir/checker.py
                                                        kabir/compile.py
  ──► bytecode ──► VM ──► output
          kabir/vm.py
```

- `kabir/lexer.py` (~210 lines) — source text → token stream.
- `kabir/parser.py` (~430 lines) — tokens → AST (recursive descent).
- `kabir/ast_nodes.py` (~180 lines) — one small class per AST node.
- `kabir/checker.py` (~590 lines) — opt-in static type checking; raises
  `KabirError` on the first type error, passes untyped code unchanged.
- `kabir/compile.py` (~600 lines) — AST → bytecode (a plain-dict code
  object, JSON-serialisable as `.kbc`).
- `kabir/vm.py` (~650 lines) — a stack-based VM that executes bytecode.
- `kabir/semantics.py` — the shared runtime semantics (`is_truthy`,
  `format_value`, `kabir_equal`, `binary_op`) used by both the VM and
  the interpreter, so behaviour cannot drift between them.
- `kabir/interpreter.py` (~674 lines) — the original tree-walking
  interpreter. No longer the `run` path (that is the VM now); it still
  powers the REPL and stays as the reference implementation behind
  `kabir run --interp`.
- `kabir/builtins.py` (~237 lines) — the 15 core built-in functions.
- `kabir/stdlib.py` (~265 lines) — the 12 standard-library functions (`random`, `math`) and the `STRING_METHODS` table.
- `kabir/errors.py` (~49 lines) — the `KabirError` type and its rendering.
- `kabir/formatter.py` (~499 lines) — the canonical formatter (`kabir fmt`): parses the file, then re-prints the AST with deterministic spacing, indentation and blank lines; comments are extracted from the source first and re-attached by line number.
- `kabir/project.py` (~330 lines) — Cargo-style projects: `kabir new`
  scaffolding, `Kabir.toml` parsing, `kabir build` to `target/<name>.kbc`.
- `kabir/cli.py` (~330 lines) — `run` / `new` / `build` / `fmt` / `repl` / `--help` / `--version`.
- `kabir/__init__.py` — version, author, and the public API (`Interpreter`, `parse`, `tokenize`, `KabirError`).
- `kabir/__main__.py` — the `python -m kabir` entry point (calls `kabir.cli:main`).

Everything is standard library only. There are no third-party dependencies, and no generated code — the lexer, parser, compiler, and VM are hand-written.

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

`_block()` expects `{`, skips separators, parses statements until `}`, and reports a missing `}` with the position of the opening brace. It also records the closing brace's line on the block as `end_line` — the interpreter ignores it, but the formatter uses it to place comments that sit inside the block or on the `}` line.

### Expressions

- `_primary()` handles numbers, string tokens (becoming `StringLiteral` nodes holding the lexer's parts list), `true`/`false`/`null` (becoming `Literal` nodes), identifiers, parenthesised expressions, array literals, dict literals, and anonymous `func` expressions. Dict keys must be string tokens whose parts are a single literal part — an interpolated key is a parse error.
- `_postfix()` loops over `(` argument lists `)`, `[` index `]` suffixes, and `.` member suffixes, so `f(1)(2)`, `a[0][1]`, `f(x)[0]`, `"abc".upper()`, and `" a ".strip().upper()` all parse. A `.` suffix consumes the dot, expects an `IDENT` (the method name — keywords are not valid method names), and wraps the expression in a `Member` node. Argument lists and index brackets allow newlines inside them.
- Separators: `_skip_separators()` consumes `NEWLINE` and `;` tokens; the top-level `parse()` loop and `_block()` call it between statements, so newlines and semicolons are interchangeable as separators.

### Error reporting in the parser

`_fail()` raises `KabirError` via `error_at()` with the offending token's line/column and the source lines, so every parse error renders as `file:line:col` + source line + caret. Typical messages: *"Expected … but found …"*, *"Unexpected … — an expression was expected here"*, *"Expected '}' to close this block"*.

## 3. AST (`kabir/ast_nodes.py`)

Every node subclasses `Node`, which carries `line` and `col`. The tree is plain data — no methods, no visitor pattern; the interpreter dispatches on node class name (see §4).

Statements: `Program`, `Block`, `LetStmt` (with an optional `type_ann`), `AssignStmt` (target + `=`/`+=`/`-=` + value), `ExprStmt`, `FuncDecl` (`name` may be `None` for anonymous functions; optional `param_types` and `return_type`), `ReturnStmt` (value may be `None`), `IfStmt` (a list of `(condition, Block)` branches plus an optional `else_body`), `WhileStmt`, `ForStmt`, `BreakStmt`, `ContinueStmt`, `ImportStmt` (`path` — the literal string from the `import` statement), `MatchExpr` (a scrutinee plus a list of `MatchArm`s — pattern, optional guard, body expression). Patterns are `Literal`, `Var` (binding), or `Wildcard` (`_`). Type annotations are `TypeAnn` nodes holding the type name as a string.

Expressions: `Literal`, `StringLiteral` (a `parts` list of `("lit", text)` / `("expr", source)`), `Var`, `ArrayLiteral`, `DictLiteral` (a list of `(key-string, value-node)` pairs), `BinaryOp`, `UnaryOp` (`-` or `not`), `Call`, `Index`, `Member` (`object` + `name` — the receiver expression and the method name after a `.`).

Because every node remembers where it came from, a runtime error can point at the exact expression that failed — the interpreter never has to guess.

## 4. Interpreter (`kabir/interpreter.py`)

`Interpreter(source, filename)` owns the global environment and runs programs. `run_source(source, filename)` lexes, parses, and runs in one call; `run(program)` runs an already-parsed program; the REPL calls `parse_source` itself and then `run_stmt` per statement so one bad line does not kill the session.

Since 2.0.0 the interpreter is no longer the `kabir run` path — that is
the compiler + VM (§6–§7) — but it still powers the REPL and stays as
the reference implementation, reachable with `kabir run --interp`. Its
operator, truthiness, and formatting logic now live in
`kabir/semantics.py` (§8); the methods below delegate to it.

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

## 5. Type checker (`kabir/checker.py`)

`check_program(program, filename, lines)` walks the AST once, before
compilation, and raises `KabirError` on the first type error. Types are
plain strings (`"number"`, `"string"`, `"bool"`, `"array"`, `"dict"`,
`"function"`, `"any"`, `"null"`).

- **Scopes.** A stack of `dict`s maps names to types. `let x: T` records
  `T`; an unannotated `let` records the inferred type of its value;
  function parameters record their annotations (`"any"` when absent).
  A pre-pass over top-level function declarations registers their
  signatures first, so forward and mutually recursive calls type-check.
- **Inference.** Best effort, `"any"` when unknown: literals; `+` on
  number+number → `"number"`, string+string → `"string"`,
  array+array → `"array"`; comparisons → `"bool"`; `and`/`or` → `"any"`;
  unary `-` → `"number"`, `not` → `"bool"`; indexing a string →
  `"string"`, an array/dict → `"any"`; calls use the builtin/stdlib
  signature table (or the callee's annotation) when known, else `"any"`;
  `match` → the arm bodies' type when they all agree, else `"any"`.
- **Rules.** A rule fires only when the involved types are statically
  known (not `"any"`): annotated `let` / arguments / returns must
  match; arithmetic and comparison operands must have the right kinds;
  `if`/`while` conditions and `match` guards must be `"bool"`; `for`
  iterables must be array/string/dict; a `match` literal pattern whose
  type differs from the scrutinee's is an error; unknown type names in
  annotations are an error. `any` accepts everything in `let`, param,
  and return positions.
- **Type-only.** The checker never reports undefined variables, wrong
  argument counts, or anything that needs run-time knowledge — those
  stay runtime errors.

## 6. Bytecode compiler (`kabir/compile.py`)

`compile_program(program, filename, lines)` walks the AST and emits a
code dict:

```python
{"name": str, "params": [str], "instrs": [[op, arg], ...],
 "consts": [...], "lines": [int, ...], "filename": str}
```

`instrs[i]` is `[op, arg]` (jump targets are absolute instruction
indices, patched after the block is emitted); `lines[i]` is the source
line of that instruction, so VM errors point at the right place.
`consts` holds only scalars (`int`/`float`/`str`/`bool`/`None`) and
nested function code dicts, which appear as `{"__code__": <code dict>}`.
`compile_source(source, filename)` is parse + compile in one call.
`save_bytecode(code, path)` / `load_bytecode(path)` write and read the
`.kbc` format: JSON `{"magic": "KBC1", "version": 1, "main": <code>}`.

The instruction set (see `kabir/compile.py` for the exact list):
`LOAD_CONST`, `LOAD_NULL/TRUE/FALSE`, `LOAD_VAR`, `DEFINE_VAR`,
`STORE_VAR`, `POP`, `DUP`, `BUILD_LIST`, `BUILD_DICT`, `BUILD_STRING`,
`ADD/SUB/MUL/DIV/MOD/POW`, `NEG`, `NOT`, `EQ/NE/LT/LE/GT/GE`, `JUMP`,
`JUMP_IF_FALSE/TRUE`, `CALL`, `RETURN`, `MAKE_FUNCTION`, `IMPORT`,
`GET_INDEX`, `SET_INDEX`, `GET_MEMBER`, `ENTER_SCOPE`, `EXIT_SCOPE`,
`GET_ITER`, `FOR_ITER`, `MATCH_FAIL`.

Notable compilation choices:

- **Variables are names, not slots.** `LOAD_VAR`/`STORE_VAR` resolve
  through the `Environment` chain at run time, exactly like the
  interpreter — so closures, shadowing, and block scoping behave
  identically, and the compiler needs no scope analysis.
- **Blocks push environments.** `ENTER_SCOPE`/`EXIT_SCOPE` bracket
  every block, mirroring the interpreter's per-block `Environment`;
  a `let` inside a block never leaks out of it.
- **`and`/`or` short-circuit** with `DUP` + a conditional jump, so
  the right side is never evaluated when the left decides the result.
- **`match` compiles inline.** The scrutinee is evaluated once and
  stays on the stack; each arm `DUP`s it, tests its pattern
  (`LOAD_CONST` + `EQ` for literals; `DEFINE_VAR` in a fresh per-arm
  scope for bindings), jumps to the next arm on failure, pops the
  scrutinee on success, evaluates the body, and jumps to the end.
  After the last arm, `MATCH_FAIL` raises the no-match error.
- **Loops.** `for` uses `GET_ITER` (snapshot list) + `FOR_ITER`
  (push next item or jump out). `break`/`continue` are compiled to
  jumps with scope cleanup (exit scopes, pop the iterator); the
  compiler keeps a loop stack and patches the targets when the loop
  ends. `break`/`continue` outside a loop and `return` outside a
  function are **compile-time** errors with the interpreter's exact
  messages.
- **Functions.** Each `FuncDecl` compiles to its own code dict, stored
  in the parent's constants; `MAKE_FUNCTION` turns it into a closure
  capturing the current environment at run time.
- **Imports.** The `IMPORT` instruction executes the module through
  the same loader logic as the interpreter (relative resolution,
  `realpath` cache, cycle detection, identical messages) — the module
  is parsed, checked, compiled, and run in a fresh module env.

## 7. Stack VM (`kabir/vm.py`)

`VM.run_code(code, filename, lines)` runs a compiled code dict;
`VM.run_source(source, filename)` is parse + compile + run. The VM
holds one value stack shared across frames; each frame records its
code, instruction pointer, stack base, and environment. Calling a
function creates a frame whose environment is a child of the closure's
captured environment, binds the parameters, and runs the body; `RETURN`
pops the frame and pushes the return value onto the caller's stack.
Call depth is capped at 500, like the interpreter.

Built-ins run through the same `(interp, node, args)` protocol as in
the interpreter: the VM exposes `.error(node, msg)`,
`.format_value(v)`, and `._random` (a per-VM `random.Random()`), and
passes a tiny `_CallNode` shim (line/filename from the `CALL`
instruction) as the node — so builtin and stdlib implementations work
unchanged in both runtimes. All arithmetic, comparison, truthiness,
and formatting go through `kabir/semantics.py`, so the two runtimes
cannot disagree about values.

VM errors carry the source line of the faulting instruction (the
bytecode stores lines, not columns), rendered in the same
file:line + source-line + caret format as interpreter errors.

## 8. Shared semantics (`kabir/semantics.py`)

Pure functions — `is_truthy`, `format_value`, `kabir_equal`,
`binary_op` — moved verbatim from the interpreter, including the
exact error strings (division by zero, the int/int exact-division
rule, `**` overflow, "Cannot add %s and %s…"). Each raises
`KabirRuntimeError(message)` (a plain exception, message only, no
position); the interpreter and the VM convert it to `KabirError`
with their own position information. This module is the single
source of truth for what operators do — change a rule here and both
runtimes change together.

## 9. Project tooling (`kabir/project.py`)

- `new_project(name, cwd=".")` validates the name
  (`^[A-Za-z][A-Za-z0-9_-]*$`), refuses an existing directory, and
  writes `Kabir.toml` (`[package]` name / version `0.1.0` / authors),
  `src/main.kbr` (a `func main()` hello program plus a `main()` call),
  and `.gitignore` (`target/`). Returns the created path.
- `build_project(path=".")` reads the manifest with a hand-rolled
  parser (no `tomllib` — the package supports Python 3.8+), compiles
  `src/main.kbr` (parse → check → compile, imports resolved relative
  to `src/`), and writes `target/<name>.kbc` via `save_bytecode`.
  Returns the output path (absolute). Prints nothing — the CLI owns
  output.
- `run_project(path)` resolves `src/main.kbr` for a project directory.
- The checker and compiler are imported lazily inside
  `build_project`, so the module stays importable on its own.

## 10. Built-ins (`kabir/builtins.py`)

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

## 11. Errors (`kabir/errors.py`)

`KabirError` carries `message`, `filename`, `line`, `col`, and `source_line`. `render()` produces the canonical format:

```text
file:line:col: message
  <source line>
  <spaces>^
```

`error_at(message, filename, lines, line, col)` builds one from a list of source lines. Lexer, parser, and interpreter all build errors through this helper, so the format is identical no matter where the error comes from. `__str__` is `render()`, so printing the exception prints the friendly form.

## 12. CLI and REPL (`kabir/cli.py`)

`main(argv=None)` is the entry point (also the `[project.scripts]` target — see `pyproject.toml`). Dispatch, in order:

1. No arguments → `repl()`.
2. `-h` / `--help` / `help` → print `HELP_TEXT`, exit `0`.
3. `-v` / `--version` / `version` → print `kabir <version>`, exit `0`.
4. `run [--interp] <file-or-dir>` → `run_file(path, use_interp)`; exactly one target, else a usage error (exit `2`).
5. `new <name>` → `new_project(name)`; prints `Created new Kabir project "<name>" in <path>`.
6. `build [dir]` → `build_project(dir or ".")`; prints `Built <name> v<version> -> <output>`.
7. `repl` → `repl()`; no extra arguments.
8. `fmt [--check] <file> ...` → `fmt_files(paths, check)`; at least one file, else a usage error (exit `2`). `--check` only reports files that would change and exits `1` when any would; otherwise files are rewritten in place and the exit code is `0`, or `1` if any file could not be read, parsed, or written.
9. An argument ending in `.kbr` or `.kbc`, or a directory → `run_file` directly (a convenience shortcut not shown in `--help`).
10. Anything else → usage error (exit `2`), help text printed to stderr.

`run_file()` reads the file as UTF-8 (a `.kbc` file goes to
`run_bytecode()`, which loads it with `load_bytecode` and runs it in
the VM; a directory with a `Kabir.toml` resolves to its `src/main.kbr`
via `run_project`, a directory without one is a usage error). The VM
path is parse → `check_program` → `compile_program` → `VM.run_code`,
so a type error fails before any code executes; the `--interp` path
runs the legacy `Interpreter` instead and skips type checking. On
`KabirError` it prints `err.render()` to stderr and returns `1`. It
also catches `RecursionError` (deep expression nesting) and reports
it as the recursion error. A missing or unreadable file is a usage
error (exit `2`), not a Kabir error.

`repl()` prints the banner, then loops on `input()` with prompt `kabir> ` (or `... ` while a multi-line construct is open). Multi-line input is brace-aware: `_braces_balanced()` counts `{`/`}` outside strings and comments, and the REPL keeps reading until the depth returns to zero — that is how you type a `func` definition across lines. `exit`/`quit` (typed on an empty buffer), Ctrl-D (`EOFError`), and Ctrl-C (`KeyboardInterrupt`, which clears the buffer) all leave cleanly. Each parsed statement is run with `run_stmt` against one long-lived `Interpreter`, so variables persist between lines; if a statement is an `ExprStmt`, its value is printed with `format_value` — that is why typing `x + 1` in the REPL prints `6`.

History is stdlib `readline`, wired up by `_setup_history()` / `_save_history()` in `cli.py` (both never raise: without `readline` the REPL just runs without history). The history file is `~/.kabir_history` (loaded at startup, written back on exit, up to 1000 lines); importing `readline` is what makes `input()` offer up/down-arrow recall, so there is no other REPL change.

`fmt_files()` reads each file, formats it through `formatter.format_source()` (which parses it first — a syntax error is reported and the file is skipped, never written), and writes the result back only when it differs. With `--check` nothing is written; the files that would change are printed and the exit code is `1`.

## 13. Testing

`python tests/run_tests.py` discovers `tests/test_*.py` with plain
`unittest` (verbosity 2) and exits non-zero on any failure. The tests
are split by layer, mirroring this document:

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
| `tests/test_formatter.py` | `kabir fmt` rules, idempotency, `--check`, invalid files, meaning preserved across formatting |
| `tests/test_repl_history.py` | REPL history setup/save across restarts, missing and unwritable history files |
| `tests/test_examples.py` | Every file in `examples/` runs and produces its expected output |
| `tests/test_match.py` | `match` parsing and interpreter behaviour: all pattern kinds, guards, nesting, no-match error |
| `tests/test_types_syntax.py` | Type annotation parsing, annotations ignored at runtime, formatter round-trips |
| `tests/test_checker.py` | The type checker: every rule, builtin/stdlib signatures, untyped code passes |
| `tests/test_compiler_vm.py` | Compiler + VM: every example's output identical between VM and interpreter, closures, imports, match, bytecode round-trip, compile-time errors |
| `tests/test_project.py` | `kabir new` scaffolding, manifest parsing, `kabir build` round-trip |
| `tests/test_cli_2_0.py` | CLI wiring: type errors inside `kabir run`, `--interp`, `new`/`build`/project-dir/`.kbc` commands |

There is no test framework dependency and no mocking — tests drive the real `Interpreter` and `VM` and compare real output. The VM/interpreter equivalence tests are the safety net for the compiler: any behaviour change shows up there first.

## 14. Where to change things

| You want to… | Touch… |
| --- | --- |
| Add a keyword | `KEYWORDS` in `lexer.py`, a parse rule in `parser.py`, a node in `ast_nodes.py`, an evaluator method in `interpreter.py` **and** a compiler case in `compile.py`, docs, tests |
| Add a built-in | One function + one `BUILTINS` line in `builtins.py`, docs, tests |
| Add a standard-library function | One function + one `STDLIB` line in `stdlib.py`, docs in the Standard library section, tests in `tests/test_stdlib.py` |
| Add a string method | One function + one `STRING_METHODS` entry in `stdlib.py`, docs, tests in `tests/test_string_methods.py` |
| Add `import` | A new statement type end-to-end (lexer keyword, AST node, parser rule, interpreter `_load_module` + module cache + file stack, compiler `IMPORT` + VM import handling), docs, tests in `tests/test_import.py` |
| Add an operator | Lexer op tables, a parser precedence level, `binary_op()` in `semantics.py` (both runtimes change together), docs, tests |
| Add a type-check rule | One rule in `checker.py`, tests in `tests/test_checker.py` |
| Add a bytecode instruction | An emitter case in `compile.py`, a dispatch case in `vm.py` (`_loop`), docs in this file, tests in `tests/test_compiler_vm.py` |
| Change the canonical style | The style rules in `kabir/formatter.py` (`_Printer`), docs in `docs/LANGUAGE.md` (Formatter section), tests in `tests/test_formatter.py` |
| Change error wording | The message string where it is raised — rendering is centralised in `errors.py` |
| Change number formatting | `format_value()` in `semantics.py` — it is the single formatting path for both runtimes |
