# Kabir Language Reference — version 6.0.0

This is the complete reference for the Kabir language. Every code block in this document is a real, runnable Kabir program, and every output block is the exact output it produces. Run any example with:

```console
$ python -m kabir run <file.kbr>
```

Conventions used below:

- `#` starts a comment; it is not part of the program.
- An output block labelled `Output:` shows exactly what the program prints (including blank lines).
- An output block labelled `Error:` shows exactly what Kabir prints to stderr, and the program exits with code 1.

For how to run programs, start the REPL, and read error messages, see the [README](../README.md#usage). For how the language is implemented, see [ARCHITECTURE.md](ARCHITECTURE.md).

## Contents

- [Program structure](#program-structure)
- [Comments](#comments)
- [Values and types](#values-and-types)
- [Variables](#variables)
- [Operators](#operators)
- [Strings and interpolation](#strings-and-interpolation)
- [String methods](#string-methods)
- [Arrays](#arrays)
- [Dicts](#dicts)
- [Truthiness](#truthiness)
- [if / elif / else](#if--elif--else)
- [while](#while)
- [for](#for)
- [break and continue](#break-and-continue)
- [match](#match)
- [Functions](#functions)
- [Static types (opt-in)](#static-types-opt-in)
- [Import](#import)
- [Built-in functions](#built-in-functions)
- [Standard library](#standard-library)
- [Errors](#errors)
- [Formatter](#formatter)
- [REPL history](#repl-history)
- [Projects](#projects)
- [Native compilation](#native-compilation)
- [Package registry](#package-registry)
- [Ownership-lite (`let mut`)](#ownership-lite-let-mut)
- [Borrowing (`&` / `&mut`)](#borrowing---mut)
- [Async/await + tasks and channels](#asyncawait--tasks-and-channels)
- [Language server (`kabir lsp` + VS Code)](#language-server-kabir-lsp--vs-code)
- [WebAssembly target](#webassembly-target)
- [Self-hosting, step 1 (the lexer)](#self-hosting-step-1-the-lexer)
- [What Kabir does not have (yet)](#what-kabir-does-not-have-yet)

## Program structure

A Kabir program is a sequence of statements in a `.kbr` file. Statements are separated by newlines or by `;` — either works, and you can mix them. Blocks of statements are wrapped in `{` and `}`.

```kabir
let a = 1; let b = 2
print(a + b)

let c = 3
print(a + b + c)
```

Output:

```text
3
6
```

A statement that starts with `{` followed by a string key and `:` is read as a dict literal, not a block:

```kabir
print({"a": 1})
```

Output:

```text
{"a": 1}
```

A bare `{ ... }` block on its own is a real block: it creates a new scope, so a `let` inside it does not leak out (see [Variables](#variables)).

```kabir
let x = 1
{
    let x = 2
    print(x)
}
print(x)
```

Output:

```text
2
1
```

## Comments

A `#` starts a comment that runs to the end of the line. Kabir has no multi-line comments.

```kabir
# This whole line is a comment.
let x = 5  # so is everything after # on this line
print(x)   # 5
```

Output:

```text
5
```

## Values and types

Kabir has six kinds of value:

| Type | Examples | Notes |
| --- | --- | --- |
| number | `42`, `3.5`, `0`, `-7` | One unified type — no separate `int`/`float` |
| string | `"hello"`, `"a\nb"` | Double quotes only; see [Strings](#strings-and-interpolation) |
| boolean | `true`, `false` | Keywords, not variables |
| null | `null` | The "no value" value |
| array | `[1, 2, 3]`, `[]` | Ordered, items of any type |
| dict | `{"name": "Kat"}`, `{}` | String keys, values of any type |
| function | (a `func` value) | First-class; see [Functions](#functions) |

Use the `type()` built-in to ask for a value's type name:

```kabir
print(type(42))
print(type(3.5))
print(type("hello"))
print(type(true))
print(type(null))
print(type([1, 2]))
print(type({"a": 1}))
print(type(print))
```

Output:

```text
number
number
string
boolean
null
array
dict
function
```

### Numbers

Numbers are written as integers (`42`) or decimals (`3.5`). There is one number type: Kabir does not distinguish integers from floats. When a float is a whole number, it prints without a decimal point — `5.0` prints as `5`.

```kabir
print(5.0)
print(7 / 2)
print(6 / 3)
print(10 / 4)
print(2.5 + 0.5)
```

Output:

```text
5
3.5
2
2.5
3
```

Division is the one place the int/float distinction peeks through: dividing two integers that divide exactly gives an integer (`6 / 3` is `2`); otherwise you get a float (`7 / 2` is `3.5`). Integer powers use arbitrary-precision integers (`10 ** 1000` works); a power that overflows float range, such as `10.0 ** 400`, fails with a friendly error instead of overflowing silently (see [Errors](#errors)).

## Variables

Declare a variable with `let`, then use it by name. `let` needs an initial value — there is no way to declare a variable without one.

```kabir
let name = "Rafid"
let age = 19
print(name)
print(age)
```

Output:

```text
Rafid
19
```

Reassign with `=` (or `+=` / `-=`, which add or subtract and store the result). Assignment is a statement, not an expression — `let x = y = 1` is an error.

```kabir
let x = 1
x = 2
print(x)
x += 3
print(x)
x -= 1
print(x)
```

Output:

```text
2
5
4
```

Assigning to a name that was never declared is an error, with a hint:

```kabir
score = 10
```

Error:

```text
demo.kbr:1:1: Cannot assign to 'score' -- it was never defined. Use 'let score = ...' first
  score = 10
  ^
```

(The location prefix is the file you ran — here the snippets were saved as `demo.kbr`. In the REPL it reads `<repl>` instead.)

Using a name that was never defined is also an error:

```kabir
print(score)
```

Error:

```text
demo.kbr:1:7: Undefined variable 'score' -- it was never defined with 'let'. Check the spelling
  print(score)
        ^
```

### Scoping

Variables are lexically scoped. Every `{ ... }` block creates a new scope: a `let` inside a block is visible only inside that block, and it shadows an outer variable of the same name without changing it. A block can read (and, with `=`, reassign) variables from an enclosing scope — assignment walks outward until it finds the variable.

```kabir
let x = 1
if true {
    let x = 2
    print(x)
    x = 3
    print(x)
}
print(x)
```

Output:

```text
2
3
1
```

The inner `x = 3` changed the inner `x` only; the outer `x` is still `1` when the block ends. Functions capture the environment where they were defined — see [Closures](#closures).

## Operators

### Arithmetic

| Operator | Meaning | Works on |
| --- | --- | --- |
| `+` | Addition (or concatenation) | Two numbers, two strings, or two arrays |
| `-` | Subtraction | Two numbers |
| `*` | Multiplication | Two numbers |
| `/` | Division | Two numbers |
| `%` | Remainder | Two numbers |
| `**` | Power | Two numbers |
| unary `-` | Negation | A number |
| `+=`, `-=` | Add/subtract and assign | A variable or index (see [Variables](#variables)) |

```kabir
print(10 + 4)
print(10 - 4)
print(10 * 4)
print(10 / 4)
print(10 % 3)
print(10.5 % 3)
print(2 ** 10)
print(-5)
print("foo" + "bar")
print([1] + [2, 3])
```

Output:

```text
14
6
40
2.5
1
1.5
1024
-5
foobar
[1, 2, 3]
```

Precedence, lowest to highest: `or` → `and` → `==` `!=` → `<` `<=` `>` `>=` → `+` `-` → `*` `/` `%` → `**` → unary `-` / `not` → calls and indexing → primaries. Parentheses override everything, as usual:

```kabir
print(10 - 4 * 2)
print((10 - 4) * 2)
```

Output:

```text
2
12
```

`**` is right-associative: `2 ** 3 ** 2` means `2 ** (3 ** 2)`, which is `512`, not `(2 ** 3) ** 2` (`64`).

```kabir
print(2 ** 3 ** 2)
print(2 ** -1)
```

Output:

```text
512
0.5
```

One precedence surprise to know about: unary minus binds *less* tightly than `**`, so `-2 ** 2` is `-(2 ** 2)`, which is `-4`. Write `(-2) ** 2` if you mean the square of negative two:

```kabir
print(-2 ** 2)
print((-2) ** 2)
```

Output:

```text
-4
4
```

Adding values of the wrong types is an error, not a silent conversion:

```kabir
print(1 + "a")
```

Error:

```text
demo.kbr:1:9: Cannot add number and string -- '+' works on two numbers, two strings, or two arrays
  print(1 + "a")
          ^
```

### Comparison

| Operator | Meaning |
| --- | --- |
| `==` | Equal |
| `!=` | Not equal |
| `<`, `<=`, `>`, `>=` | Ordering |

Ordering comparisons (`<`, `<=`, `>`, `>=`) work only on two numbers or two strings. Equality (`==`, `!=`) works on any two values, including arrays and dicts (compared element by element, key by key), and functions (equal only to themselves).

```kabir
print(1 < 2)
print(5 >= 5)
print("apple" < "banana")
print(1 == 1.0)
print([1, 2] == [1, 2])
print({"a": 1} == {"a": 1})
print(1 == true)
print(null == null)
print(null == 0)
```

Output:

```text
true
true
true
true
true
true
false
true
false
```

Note that `1 == true` is `false`: booleans are never equal to numbers, even though `1` is truthy (see [Truthiness](#truthiness)).

### Logic

`and`, `or`, and `not` work on truthiness (see [Truthiness](#truthiness)). `and` and `or` short-circuit: the right side is evaluated only if the left side does not already decide the result. And they return *values*, not booleans — `and` returns its left side if that side is falsy, otherwise its right side; `or` returns its left side if that side is truthy, otherwise its right side.

```kabir
print(true and false)
print(true or false)
print(not true)
print(not 0)
print(1 < 2 and 2 < 3)
print(true and "yes")
print(false or "fallback")
print(0 or "fallback")
print("" or "empty?")
```

Output:

```text
false
true
false
true
true
yes
fallback
fallback
empty?
```

Because `or` returns the first truthy value, `x or default` is a handy way to write a fallback — but remember that `0` and `""` are falsy, so they fall through to the default too.

## Strings and interpolation

Strings use **double quotes only**. Single quotes are a friendly error, not a second string style:

```kabir
print('hi')
```

Error:

```text
demo.kbr:1:7: Strings use double quotes, not single quotes -- write "hello" instead of 'hello'
  print('hi')
        ^
```

Strings cannot span multiple lines — a newline before the closing quote is an error. The escape sequences are `\n` (newline), `\t` (tab), `\r` (carriage return), `\"` (a quote), and `\\` (a backslash). Any other escape is an error:

```kabir
let s = "tab\there \"quoted\" end"
print(s)
```

Output:

```text
tab	here "quoted" end
```

(The `\t` above prints as a real tab character.)

### Interpolation

Inside a string, `{ expression }` is replaced by the value of the expression, formatted the same way `print` formats it. Any expression is allowed — a variable, arithmetic, a function call, even a string literal (nested double quotes work inside the braces).

```kabir
let name = "Rafid"
let n = 3
print("Hello, {name}!")
print("2 + 3 = {2 + 3}")
print("You have {n * 2} messages.")
print("sum: {1 + 2} and {len("abc")}")
print("The list is {[1, 2, 3]}")
```

Output:

```text
Hello, Rafid!
2 + 3 = 5
You have 6 messages.
sum: 3 and 3
The list is [1, 2, 3]
```

Empty braces are an error, and an unterminated `{` is an error:

```kabir
print("empty {} here")
```

Error:

```text
demo.kbr:1:7: Empty interpolation {} -- put an expression inside the braces
  print("empty {} here")
        ^
```

### Indexing strings

Index a string with `[i]` to get a one-character string. Negative indexes count from the end. Out-of-range indexes are an error.

```kabir
let word = "Kabir"
print(word[0])
print(word[1])
print(word[-1])
```

Output:

```text
K
a
r
```

Strings are immutable through indexing — `word[0] = "X"` is an error; only arrays and dicts support index assignment (see [Arrays](#arrays) and [Dicts](#dicts)).

## String methods

Strings have methods, called with a dot: `"abc".upper()`. The dot is only for string methods — dicts still use brackets (`user["name"]`, not `user.name`), and using a dot on anything that is not a string is an error (see below). There is no `length()` method: `len("abc")` already does that.

Methods return new values; they never change the string they are called on (strings are immutable). A method call is an expression, so it works anywhere an expression works — including inside `{ ... }` interpolation, chained with other methods, and followed by indexing.

```kabir
print("abc".upper())
print("ABC".lower())
print("  padded  ".strip())
print("a,b,c".split(","))
print("a b  c".split())
print(", ".join(["a", "b", "c"]))
print("hello".contains("ell"))
print("hello".contains("z"))
print("hello".starts_with("he"))
print("hello".ends_with("lo"))
print("aaa".replace("a", "b"))
print(" a ".strip().upper())
print("a b".split(" ")[1])
```

Output:

```text
ABC
abc
padded
[a, b, c]
[a, b, c]
a, b, c
true
false
true
true
bbb
A
b
```

### upper

`"abc".upper()` returns the string in upper case. It takes no arguments.

### lower

`"ABC".lower()` returns the string in lower case. It takes no arguments.

### strip

`"  padded  ".strip()` returns the string with leading and trailing whitespace removed. It takes no arguments — there is no `strip(chars)` form, so `strip(",")` is an argument-count error.

### split

`"a,b,c".split(",")` splits on the separator and returns an array of strings. With no argument, `split()` splits on runs of whitespace and drops empty pieces (`"a b  c".split()` is `["a", "b", "c"]`). The separator must be a non-empty string.

```kabir
print("a,b".split(""))
```

Error:

```text
demo.kbr:1:18: split() needs a non-empty separator
  print("a,b".split(""))
                   ^
```

### join

`", ".join(["a", "b", "c"])` joins an array of strings, using the receiver as the separator. The argument must be an array, and every item in it must be a string.

### contains

`"hello".contains("ell")` is `true` when the string contains the argument as a substring, `false` otherwise. The argument must be a string.

### starts_with

`"hello".starts_with("he")` is `true` when the string starts with the argument. The argument must be a string. (Note the underscore: it is `starts_with`, not `startswith`.)

### ends_with

`"hello".ends_with("lo")` is `true` when the string ends with the argument. The argument must be a string. (Note the underscore: it is `ends_with`, not `endswith`.)

### replace

`"aaa".replace("a", "b")` replaces **all** occurrences of the first argument with the second. Both arguments must be strings. There is no count argument — every occurrence is replaced.

### Methods inside interpolation, and stored methods

A method call inside `{ ... }` works like any other expression:

```kabir
print("shout: {"abc".upper()}")
```

Output:

```text
shout: ABC
```

A method is a value too: `"abc".upper` (without the call) is a function you can store in a variable and call later. It stays bound to the string it came from:

```kabir
let u = "abc".upper
print(u())
```

Output:

```text
ABC
```

### Errors

Using a dot on something that is not a string is an error, and the message names the type:

```kabir
print([1, 2].upper())
```

Error:

```text
demo.kbr:1:13: Cannot use .upper on a array -- only strings have methods
  print([1, 2].upper())
              ^
```

A dot on a dict gets a pointer at the Kabir way instead:

```kabir
let user = {"name": "Rafid"}
print(user.name)
```

Error:

```text
demo.kbr:2:11: Cannot use .name on a dict -- write dict["name"] instead (for example, user["name"])
  print(user.name)
            ^
```

An unknown method on a string is an error naming the method:

```kabir
print("a".frobnicate())
```

Error:

```text
demo.kbr:1:10: No method '.frobnicate' on a string -- strings have no method named 'frobnicate'
  print("a".frobnicate())
           ^
```

## Arrays

Array literals use `[` and `]`, with items of any type — including mixed types and nested arrays.

```kabir
let empty = []
let nums = [1, 2, 3]
let mixed = [1, "two", true, null, [4, 5]]
print(empty)
print(nums)
print(mixed)
print(len(nums))
```

Output:

```text
[]
[1, 2, 3]
[1, two, true, null, [4, 5]]
3
```

### Indexing and assignment

Index with `[i]`; negative indexes count from the end. Assign through an index to replace an item in place.

```kabir
let arr = [10, 20, 30]
print(arr[0])
print(arr[-1])
print(arr[-2])
arr[1] = 99
print(arr)
arr[-1] = 0
print(arr)
```

Output:

```text
10
30
20
[10, 99, 30]
[10, 99, 0]
```

An index that is not a whole number, or is out of range, is an error:

```kabir
let a = [1]
print(a[5])
```

Error:

```text
demo.kbr:2:9: Array index 5 is out of range -- the array has 1 item
  print(a[5])
          ^
```

### Growing and shrinking

Use the `push()` and `pop()` built-ins. `push` appends and returns the array; `pop` removes and returns the last item. Arrays are passed by reference — a function that pushes onto an array you pass in changes your array.

```kabir
let a = [1, 2]
print(push(a, 3))
print(a)
print(pop(a))
print(a)
```

Output:

```text
[1, 2, 3]
[1, 2, 3]
3
[1, 2]
```

`pop` on an empty array is an error. `+` concatenates two arrays into a new one (it does not change either input):

```kabir
print([1] + [2, 3])
```

Output:

```text
[1, 2, 3]
```

## Dicts

Dict literals use `{` and `}` with string keys and `:` between key and value. Keys must be plain string literals — bare names (`{name: 1}`) and interpolated strings (`{"a{1}": 1}`) are both errors.

```kabir
let user = {"name": "Rafid", "age": 19, "tags": ["dev", "creator"]}
print(user)
print(user["name"])
print(len(user))
print(keys(user))
```

Output:

```text
{"name": Rafid, "age": 19, "tags": [dev, creator]}
Rafid
3
[name, age, tags]
```

`keys()` returns the keys in insertion order. Read a value with `["key"]`; assign through `["key"]` to change a value or add a new key:

```kabir
let d = {"a": 1}
d["b"] = 2
print(d)
d["a"] = 99
print(d)
```

Output:

```text
{"a": 1, "b": 2}
{"a": 99, "b": 2}
```

Reading a missing key is an error that lists the keys that do exist:

```kabir
let d = {"a": 1}
print(d["b"])
```

Error:

```text
demo.kbr:2:9: No key 'b' in this dict -- available keys: "a"
  print(d["b"])
          ^
```

Dict keys must be strings — `d[0]` is an error. Dicts still use brackets: write `user["name"]`, not `user.name`. Strings, though, have methods: `"abc".upper()`. See [String methods](#string-methods).

## Truthiness

`if`, `while`, `and`, `or`, and `not` do not need a boolean — they use *truthiness*:

| Value | Truthy? |
| --- | --- |
| `false`, `null` | Falsy |
| `0`, `0.0` | Falsy |
| `""` (empty string) | Falsy |
| `[]` (empty array) | Falsy |
| `{}` (empty dict) | Falsy |
| Everything else — non-zero numbers, non-empty strings/arrays/dicts, functions | Truthy |

```kabir
if 1 { print("1 is truthy") }
if 0 { print("0 is truthy") } else { print("0 is falsy") }
if "" { print("empty string is truthy") } else { print("empty string is falsy") }
if [0] { print("a non-empty array is truthy, even holding 0") }
```

Output:

```text
1 is truthy
0 is falsy
empty string is falsy
a non-empty array is truthy, even holding 0
```

## if / elif / else

`if` takes a condition and a `{ ... }` block. `elif` and `else` are optional, and you may have as many `elif` branches as you like. The `else` may start on the line after the closing `}` — Kabir allows newlines between `}` and `elif`/`else`.

```kabir
let score = 85
if score >= 90 {
    print("A")
} elif score >= 80 {
    print("B")
} elif score >= 70 {
    print("C")
} else {
    print("Keep practising")
}
```

Output:

```text
B
```

Only the first branch whose condition is truthy runs; the rest are skipped. The same shape with `else` on its own line also works:

```kabir
let x = 1
if x == 2 {
    print("two")
}
else {
    print("not two")
}
```

Output:

```text
not two
```

## while

`while` repeats its block as long as the condition is truthy. The condition is checked before every iteration, including the first — so the body may run zero times.

```kabir
let i = 1
let total = 0
while i <= 5 {
    total += i
    i += 1
}
print(total)
```

Output:

```text
15
```

`while true { ... }` loops forever unless the body `break`s out — that is how the interactive examples (`examples/guessing-game.kbr`, `examples/todo.kbr`, `examples/calculator.kbr`) structure their main loop.

## for

`for x in <iterable> { ... }` assigns each item to `x` in turn. The iterable can be an array, a string, a dict, or a `range()`:

- **Array** — each item in order.
- **String** — each character, as a one-character string.
- **Dict** — each key, in insertion order.
- **`range()`** — each number in the range (see [`range()`](#range)).

The loop variable is fresh on every iteration: a `let` with the same name inside the body, or a rebinding of the loop variable, does not leak into the next iteration or outside the loop.

```kabir
for n in [1, 2, 3] {
    print(n)
}
for ch in "Kabir" {
    print(ch)
}
for k in {"a": 1, "b": 2} {
    print(k)
}
for i in range(3) {
    print(i)
}
```

Output:

```text
1
2
3
K
a
b
i
r
a
b
0
1
2
```

Looping over anything else — a number, a boolean, `null` — is an error:

```kabir
for i in 5 { print(i) }
```

Error:

```text
demo.kbr:1:10: Cannot loop over number -- 'for' needs an array, a string, or a range
  for i in 5 { print(i) }
           ^
```

## break and continue

`break` leaves the innermost loop immediately; `continue` skips the rest of the current iteration and starts the next one. Both are errors outside a loop.

```kabir
let i = 0
while i < 5 {
    i += 1
    if i == 3 { continue }
    if i == 5 { break }
    print(i)
}
print("done")
```

Output:

```text
1
2
4
done
```

```kabir
break
```

Error:

```text
demo.kbr:1:1: 'break' can only be used inside a loop
  break
  ^
```

## match

`match` tests one value against a list of patterns and evaluates the
first arm that fits — Rust-style pattern matching, in one expression.
Arms are tried in order; the first matching arm wins and the rest are
not evaluated.

```kabir
let label = match x { 0 => "zero", 1 => "one", _ => "other" }
print(label)
```

An arm is `<pattern> => <expression>`, with arms separated by commas
(a trailing comma is allowed). There are three kinds of pattern:

- **Literal** — a number, string, `true`, `false`, or `null`. The arm
  matches when the value equals the literal.
- **Binding** — a bare name (`n`). It always matches and binds the
  value to that name for the arm's expression. Because it always
  matches, arms after a binding arm are unreachable.
- **Wildcard** — `_`. It always matches and binds nothing.

```kabir
let kind = match n {
    0 => "zero",
    1 => "one",
    x if x > 1 => "big",
    _ => "negative",
}
```

A guard (`if <condition>` between the pattern and `=>`) adds a further
test: the arm matches only when both the pattern and the guard hold.
A guard must be a boolean (when its type is known).

`match` is an expression, so it works anywhere an expression works —
in `let`, in `return`, as a call argument, and as a statement on its
own line:

```kabir
func label(n) {
    return match n { 0 => "zero", _ => "other" }
}

match cmd {
    "go" => print("going"),
    "stop" => print("stopping"),
    _ => print("unknown command"),
}
```

If no arm matches, it is an error:

```kabir
match 5 { 0 => "zero" }
```

Error:

```text
demo.kbr:1:1: No pattern matched the value 5
  match 5 { 0 => "zero" }
  ^
```

When the scrutinee's type is known (see [Static types](#static-types-opt-in)),
a literal pattern of a different type is a compile-time error —
`match "a" { 0 => "zero", _ => "other" }` fails before the program runs.

## Functions

### Named functions

Declare a function with `func`, a name, a parameter list, and a body block:

```kabir
func greet(name) {
    return "Hello, {name}!"
}

print(greet("Rafid"))
```

Output:

```text
Hello, Rafid!
```

A function that runs off the end of its body without hitting `return` returns `null`. A bare `return` (with no value) also returns `null`.

```kabir
func nothing() {
}

func early() {
    return
}

print(nothing())
print(early())
print(type(nothing()))
```

Output:

```text
null
null
null
```

`return` outside a function is an error:

```kabir
return 1
```

Error:

```text
demo.kbr:1:1: 'return' can only be used inside a function
  return 1
  ^
```

Calling a function with the wrong number of arguments is an error:

```kabir
func f(a) { return a }
print(f(1, 2))
```

Error:

```text
demo.kbr:2:8: Function 'f' takes 1 argument, but got 2
  print(f(1, 2))
         ^
```

### Anonymous functions and first-class values

`func` without a name is an expression that creates a function value. Functions can be stored in variables, passed as arguments, returned from other functions, and stored in arrays and dicts — and `print` shows them as `<function name>` (or `<function <anonymous>>` for an unnamed one).

```kabir
let double = func(x) { return x * 2 }
print(double(21))
print(double)

func apply(f, x) {
    return f(x)
}
print(apply(double, 10))
```

Output:

```text
42
<function <anonymous>>
20
```

### Closures

A function captures the environment where it was defined — its *closure*. It keeps seeing those variables even after the defining scope has finished, and it sees later changes to them, because it holds a reference to the environment, not a copy.

```kabir
func make_adder(n) {
    return func(x) { return x + n }
}

let add5 = make_adder(5)
let add10 = make_adder(10)
print(add5(1))
print(add10(1))
print(add5(100))
```

Output:

```text
6
11
105
```

Each call to `make_adder` creates a fresh environment holding its own `n`, so `add5` and `add10` do not interfere with each other.

### Recursion

Functions can call themselves. Kabir caps nesting at 500 calls deep — deeper recursion (usually infinite recursion) is an error, not a crash:

```kabir
func fib(n) {
    if n < 2 {
        return n
    }
    return fib(n - 1) + fib(n - 2)
}

for i in range(10) {
    print(fib(i))
}
```

Output:

```text
0
1
1
2
3
5
8
13
21
34
```

(This is `examples/fibonacci.kbr`, extended to the first 10 numbers; the example file itself prints the first 15.)

## Static types (opt-in)

Types in Kabir are optional. Unannotated code works exactly as before —
nothing is checked, nothing breaks. Add an annotation and the compiler
checks it **before the program runs**: a mistyped program fails at
compile time with a clear error instead of crashing halfway through.

The types are `number`, `string`, `bool`, `array`, `dict`, `function`,
and `any` (`any` means "unchecked" — it accepts everything).

### Annotations

```kabir
let x: number = 5
let name: string = "Rafid"
let tags: array = ["a", "b"]

func add(a: number, b: number) -> number {
    return a + b
}
```

Annotate a `let`, a parameter, or a function's return type (`-> Type`
after the parameter list). You can annotate some parameters and leave
others bare — unannotated parts stay dynamic.

### What is checked

- **Annotated `let`** — the value's type must match the annotation.
- **Function arguments** — calling an annotated function with the wrong
  argument type is an error (only when the callee's annotations are
  known).
- **Return values** — a `return` inside an annotated function must give
  the annotated type. A bare `return` gives `null`.
- **Operators** — `- * / % **` need numbers; `+` needs two numbers, two
  strings, or two arrays; comparisons need two numbers or two strings.
- **Conditions** — `if` / `while` conditions and `match` guards must be
  booleans (when their type is known).
- **`for`** — the iterable must be an array, a string, or a dict.
- **`match`** — a literal pattern of a different type from the
  scrutinee is an error; a binding arm takes the scrutinee's type.

Checks only fire when the types are statically known. An unannotated
`let` takes the inferred type of its value, and an unannotated
parameter has type `any` — and `any` never causes an error:

```kabir
let x = "hello"   # x is inferred as string
let y: number = x # error: x is a string, not a number

func show(v) {    # v is any
    let n: number = v  # fine: any accepts the annotation
    print(n)
}
```

But this fails before running:

```kabir
let x: number = "hello"
```

Error:

```text
demo.kbr:1:17: Variable 'x' is declared as number, but the value is a string -- give it a number, or change the annotation
  let x: number = "hello"
                  ^
```

### What is NOT checked

The checker is type-only. It never reports undefined variables, wrong
argument counts, or anything that can only be known at run time —
those are still runtime errors, reported the same way as before.
Annotations are ignored at run time: `kabir run --interp` (the legacy
interpreter path) does not check types at all.

## Import

`import` loads another `.kbr` file and brings its top-level bindings into the importing file. It is a statement with two forms: the keyword `import` followed by a plain string literal holding the path, or by a bare package name (7.0.0 — see [Package imports](#package-imports)).

```kabir
import "utils/helpers.kbr"
import "lib/math-tools.kbr"
import greet
```

The path must be a plain string literal — an interpolated path (`import "dir/{name}.kbr"`) is a parse error:

```kabir
import "dir/{x}.kbr"
```

Error:

```text
demo.kbr:1:8: import paths must be plain strings, not interpolated ones
  import "dir/{x}.kbr"
         ^
```

The path must end in `.kbr` — anything else is also a parse error:

```kabir
import "notes.txt"
```

Error:

```text
demo.kbr:1:8: import "notes.txt" must name a .kbr file
  import "notes.txt"
         ^
```

### Where the path points

The path resolves **relative to the importing file's directory**, never to the process working directory. `import "helpers.kbr"` in `/home/rafid/proj/main.kbr` loads `/home/rafid/proj/helpers.kbr`, even when you run `kabir run proj/main.kbr` from elsewhere. A file imported by an imported file resolves relative to *its own* directory, not the entry file's. An absolute path in the string is honoured as-is. In the REPL there is no importing file, so the path resolves relative to the working directory.

There is no search path and no `..` sandboxing: `import "../shared.kbr"` is allowed.

### Package imports

A bare name imports a registry package instead of a file (7.0.0):

```kabir
import greet
print(greet("Rafid"))   # Hello, Rafid!
```

The name resolves through the enclosing project: `Kabir.toml`
`[dependencies]` gives the exact version wanted, `Kabir.lock`
gives the exact version installed, and the entry point is
`vendor/<name>/<version>/src/main.kbr` under the project root.
Install it first with `kabir add greet@<version>` (see
[REGISTRY.md](REGISTRY.md)).

* **Hyphenated names work:** `import my-pkg`. A name must start
  with a letter and contain only letters, digits, `-` and `_` —
  the same rule as project names. `import _foo` and `import 5`
  are parse errors.
* **Transitive imports work:** a vendored package that itself does
  `import <its-dependency>` finds that dependency in the
  *consumer* project's `vendor/` tree — the consumer's lockfile
  lists every transitive dependency, so it is already installed.
  A transitive dependency can also be imported directly from the
  consumer, since it is vendored and locked.
* **Exact versions only.** If `Kabir.toml` and `Kabir.lock`
  disagree about a package's version, the import fails with a
  `Version mismatch` error naming the package and both versions —
  run `kabir add <name>@<version>` to reinstall. A package that
  is not a dependency at all fails with a clear error naming the
  package and the `kabir add` command that fixes it. A bare import
  outside any project (no `Kabir.toml` above the importing file)
  fails the same way.
* **Same semantics as a file import.** The package's top-level
  bindings come in flat, the module is cached (importing it twice
  runs it once), and cycles are detected — everything in
  [What an import brings in](#what-an-import-brings-in) applies.

### What an import brings in

All top-level bindings of the imported file come in **flat** — `let`s and `func`s defined at the top level of the imported file become visible in the importing file's scope, exactly as if you had defined them there. There are no namespaces, no `as` alias, and no selective import: importing a file always brings in all of its top-level bindings.

- **Only top-level bindings are exported.** A `let` inside a function or a block in the imported file stays local to that function or block.
- **Name collisions overwrite silently.** If the importer already has a name the module also defines, the import overwrites it — the same rule as redefining a name with `let`. There is no collision warning in 1.1.0.
- **Mutable values are shared, not copied.** If the module defines `let scores = [1, 2]` and the importer `push`es onto it, the module's functions see the change. Function values are shared too: two importers of the same module get the same function objects.
- **Bindings are visible only after the `import` statement.** An `import` at the bottom of a file cannot be used at the top. The convention is imports at the top of the file.
- **The imported file runs in its own scope.** Its functions close over the module's own bindings, not the importer's variables, so a module helper cannot accidentally read (or be broken by) the importer's later `let`s. Its top-level `print`s and other side effects run at import time.

`import` is now a reserved word: it cannot be used as a variable or function name.

### Each file runs once (module cache)

Kabir remembers every file it has imported, keyed by its canonical (absolute, resolved) path, so `./utils.kbr`, `utils.kbr`, and `sub/../utils.kbr` are the same module. Re-importing a file does **not** re-execute it — the cached bindings are simply copied into the current scope again. Diamond imports work and are cheap: if `main.kbr` imports `b.kbr` and `c.kbr`, and both import `d.kbr`, `d.kbr` runs once.

A file is cached only after it executes successfully. And there is no `reload`: editing a file on disk and re-importing it in the same run — or the same REPL session — does **not** pick up the change; the cached bindings are returned.

### Cycles are an error

If two files import each other, Kabir reports the cycle instead of looping forever. The error names the whole chain and points at the `import` statement that closed the cycle:

```kabir
# main.kbr imports a.kbr, a.kbr imports b.kbr, b.kbr imports a.kbr
```

Error:

```text
b.kbr:1:1: Import cycle detected: a.kbr -> b.kbr -> a.kbr
  import "a.kbr"
  ^
```

One honest wrinkle: the chain lists the files Kabir was in the middle of *importing* when the cycle closed. The entry file — the file you ran — is loaded directly, not imported, so it never appears in the chain. If the entry file itself is part of the cycle (say `a.kbr` run directly, importing `b.kbr`, which imports `a.kbr`), the cycle is detected one re-entry later and the chain reads `b.kbr -> a.kbr -> b.kbr`. Either way, mutually recursive functions across two files are not possible in 1.1.0 — that is a documented limitation, not a common program shape for a small language. Self-import (`a.kbr` importing `"a.kbr"`) errors the same way.

### Errors in an imported file

Errors inside an imported file are Kabir errors, never Python tracebacks:

- **Missing file** — a Kabir error at the `import` statement in the importing file:

```kabir
import "nope.kbr"
```

Error:

```text
demo.kbr:1:1: Cannot import "nope.kbr" -- no such file
  import "nope.kbr"
  ^
```

An unreadable or non-UTF-8 file errors the same way, at the same place.

- **Syntax or runtime errors inside the imported file** — the error propagates unchanged, pointing at the imported file's own line and column. An error on the line *after* an import still points at the importing file, not the imported one.
- **A top-level `return`, `break`, or `continue` in an imported file** — the same error as in any file (`'return' can only be used inside a function`, and so on).

### Import inside a function or block

`import` is allowed anywhere a statement is allowed, and its bindings land in the scope current at the `import` statement. An import inside a block does not leak out of the block; an import inside a function body executes on each call (cheap after the first, because the cache copy still runs). Put imports at the top of the file unless you have a reason not to.

### Worked example

`examples/import-demo.kbr` imports `examples/greet-lib.kbr`, a small library file with only `let`/`func` at the top level and no prints:

```kabir
# greet-lib.kbr -- small reusable helpers (imported by import-demo.kbr).
func greet(name) {
    return "Hello, " + name + "!"
}
let version_note = "imported from greet-lib.kbr"
```

```kabir
# import-demo.kbr -- shows import: load greet-lib.kbr and use it.
import "greet-lib.kbr"
print(greet("Rafid"))
print(version_note)
```

Output:

```text
Hello, Rafid!
imported from greet-lib.kbr
```

## Built-in functions

Kabir has 15 core built-in functions. They are available in every program — no import needed — and they live in the global environment, so a `let` with the same name in your program shadows the built-in in that scope. Every built-in checks its argument count and types and reports mistakes in the same friendly error format.

The math functions `sqrt`, `pow`, and `abs` live in this section; the rest of the math library (`floor`, `ceil`, `sin`, `cos`, `tan`, `log`, `exp`, `pi`, `e`) and the `random` functions are in [Standard library](#standard-library), documented in the same style.

### print

`print(...)` prints its arguments, separated by single spaces, followed by a newline. Values are formatted Kabir-style: strings print as themselves, `5.0` prints as `5`, `true`/`false`/`null` print as written, arrays print as `[1, 2, 3]`, dicts as `{"a": 1}`, functions as `<function name>`. With no arguments it prints a blank line. It returns `null`.

```kabir
print("Hello")
print("a", "b", 42)
print(5.0, true, null)
print([1, 2], {"x": 1})
print()
print("after the blank line")
```

Output:

```text
Hello
a b 42
5 true null
[1, 2] {"x": 1}

after the blank line
```

### input

`input()` reads one line from the user and returns it as a string. `input(prompt)` prints the prompt first (the prompt must be a string) and reads the reply. There is no trailing-newline stripping to worry about — the newline is not part of the returned string. If the input stream ends, it is an error.

```kabir
let name = input("Name: ")
print("Hi, {name}!")
```

With `Rafid` typed at the prompt:

```text
Name: Hi, Rafid!
```

(`input()` with no prompt reads a line silently. The interactive examples use it in loops — see `examples/calculator.kbr` and `examples/todo.kbr`.)

### len

`len(x)` returns the number of characters in a string, items in an array, or keys in a dict. Anything else is an error.

```kabir
print(len("hello"))
print(len([1, 2, 3]))
print(len({"a": 1, "b": 2}))
print(len(""))
print(len([]))
```

Output:

```text
5
3
2
0
0
```

### str

`str(x)` converts a value to a string, using the same formatting as `print`: numbers print without a trailing `.0`, booleans and `null` become `"true"` / `"false"` / `"null"`, arrays and dicts become their printed form.

```kabir
print(str(42))
print(str(5.0))
print(str(true))
print(str(null))
print(str([1, 2]))
```

Output:

```text
42
5
true
null
[1, 2]
```

### num

`num(x)` turns a string into a number — an integer if the text is a whole number, a float otherwise — and passes numbers through unchanged. Leading/trailing whitespace is ignored. Text that is not a number is an error. Booleans, arrays, and dicts are errors too.

```kabir
print(num("42"))
print(num("3.5"))
print(num("  7  "))
print(num(9))
print(type(num("42")))
print(type(num("3.5")))
```

Output:

```text
42
3.5
7
9
number
number
```

### type

`type(x)` returns the type name as a string: `"number"`, `"string"`, `"boolean"`, `"null"`, `"array"`, `"dict"`, or `"function"` (built-in and user-defined functions alike).

```kabir
print(type(1))
print(type("s"))
print(type(false))
print(type(null))
print(type([]))
print(type({}))
print(type(len))
```

Output:

```text
number
string
boolean
null
array
dict
function
```

### range

`range(n)` returns `[0, 1, ..., n-1]`; `range(a, b)` returns `[a, a+1, ..., b-1]`. Arguments must be whole numbers; anything else is an error. A range that would be empty returns `[]` — `range(5, 2)` is `[]`, and so is `range(0)`.

```kabir
print(range(3))
print(range(2, 5))
print(range(5, 2))
print(range(0))
```

Output:

```text
[0, 1, 2]
[2, 3, 4]
[]
[]
```

There is no three-argument `range` with a step — write the loop yourself if you need one.

### push

`push(array, value)` appends `value` to `array` and returns the array. The first argument must be an array.

```kabir
let a = [1, 2]
print(push(a, 3))
print(a)
print(push(a, 4))
```

Output:

```text
[1, 2, 3]
[1, 2, 3]
[1, 2, 3, 4]
```

### pop

`pop(array)` removes the last item and returns it. The argument must be an array, and it must not be empty.

```kabir
let a = [1, 2, 3]
print(pop(a))
print(a)
print(pop(a))
print(a)
```

Output:

```text
3
[1, 2]
2
[1]
```

### sqrt

`sqrt(x)` returns the square root of a number. A negative argument or a non-number is an error.

```kabir
print(sqrt(16))
print(sqrt(2))
print(sqrt(0))
```

Output:

```text
4
1.4142135623730951
0
```

### pow

`pow(x, y)` returns `x` to the power of `y`. Both arguments must be numbers. (The `**` operator does the same thing; `pow` exists for when a function value is handier, e.g. passing it to another function.)

```kabir
print(pow(2, 10))
print(pow(2, -1))
print(pow(9, 0.5))
```

Output:

```text
1024
0.5
3
```

(`pow(9, 0.5)` is `3.0` internally, and Kabir prints whole-number floats as `3` — see [Numbers](#numbers).)

### abs

`abs(x)` returns the absolute value of a number.

```kabir
print(abs(7))
print(abs(0 - 7))
print(abs(-3.5))
print(abs(0))
```

Output:

```text
7
7
3.5
0
```

### keys

`keys(dict)` returns a list of the dict's keys, in insertion order. The argument must be a dict.

```kabir
let d = {"name": "Rafid", "age": 19}
print(keys(d))
print(keys({}))
```

Output:

```text
[name, age]
[]
```

### int

`int(x)` converts a number or a string to an integer, truncating toward zero (`int(-3.7)` is `-3`). A string holding a decimal (`"3.9"`) converts through float and truncates to `3`. Booleans are an error, and so is text that is not a number.

```kabir
print(int(3.9))
print(int(-3.7))
print(int("42"))
print(int("3.9"))
print(int(5))
```

Output:

```text
3
-3
42
3
5
```

### float

`float(x)` converts a number or a string to a float. Booleans are an error, and so is text that is not a number.

```kabir
print(float(2))
print(float("2.5"))
print(float(3.5))
```

Output:

```text
2
2.5
3.5
```

(`float(2)` is `2.0` internally, and Kabir prints whole-number floats as `2` — see [Numbers](#numbers).)

## Standard library

The standard library is 21 functions plus the constants `pi` and `e`, all plain globals — available in every program with no import, just like the core built-ins above. `sqrt`, `pow`, and `abs` stay in [Built-in functions](#built-in-functions) and are documented there; the functions below join them to make one math library. There are no new global string functions: string helpers are the [methods](#string-methods). The 3.0.0 additions — file I/O, JSON, HTTP, and time — are documented after the math functions.

Each run — and each REPL session — has its own random sequence (see `seed`).

### randint

`randint(a, b)` returns a random whole number from `a` to `b`, **both ends included**. Both arguments must be whole numbers, and `a` must not be greater than `b`. This is the guessing-game primitive: `randint(1, 100)`.

```kabir
print(randint(10, 1))
```

Error:

```text
demo.kbr:1:14: randint() needs the first number to be no greater than the second, but got 10 and 1
  print(randint(10, 1))
               ^
```

### randrange

`randrange(n)` returns one random whole number from `range(n)` — that is, from `0` to `n - 1`. `randrange(a, b)` returns one from `range(a, b)`. Arguments must be whole numbers, and the range must not be empty (`randrange(0)` and `randrange(5, 2)` are errors).

### choice

`choice(array)` returns one random item from an array, of any type. The argument must be an array, and it must not be empty.

```kabir
print(choice([]))
```

Error:

```text
demo.kbr:1:13: choice() cannot choose from an empty array
  print(choice([]))
              ^
```

### shuffle

`shuffle(array)` shuffles an array **in place** and returns the same array — the same mutate-and-return convention as `push()`. The argument must be an array.

### seed

`seed(n)` seeds the random sequence, so the numbers after it are deterministic — the same seed always gives the same sequence. It returns `null`. The argument must be a whole number. `seed` is for tests and repeatable demos; the shipped examples never seed the game itself (see `examples/guessing-game.kbr`).

```kabir
seed(7)
print(randint(1, 100))
print(randint(1, 100))
print(randrange(10))
print(randrange(5, 10))
print(choice(["red", "green", "blue"]))
let deck = [1, 2, 3, 4, 5]
print(shuffle(deck))
print(deck)
seed(7)
print(randint(1, 100))
```

Output:

```text
42
20
6
5
red
[3, 4, 2, 1, 5]
[3, 4, 2, 1, 5]
42
```

### floor

`floor(x)` returns the greatest whole number less than or equal to `x`, as a number. The argument must be a number.

### ceil

`ceil(x)` returns the least whole number greater than or equal to `x`, as a number. The argument must be a number.

```kabir
print(floor(3.7))
print(floor(0 - 3.2))
print(ceil(3.2))
print(ceil(4))
```

Output:

```text
3
-4
4
4
```

### sin

`sin(x)` returns the sine of `x`. The angle is in **radians, not degrees** — there is no degree variant in 1.1.0, so `sin(90)` is not `1`; `sin(pi / 2)` is. The argument must be a number, and the result is a float.

### cos

`cos(x)` returns the cosine of `x`, in **radians** like `sin`. The argument must be a number.

### tan

`tan(x)` returns the tangent of `x`, in **radians** like `sin`. The argument must be a number.

```kabir
print(sin(0))
print(cos(0))
print(sin(pi / 2))
```

Output:

```text
0
1
1
```

### log

`log(x)` returns the natural logarithm of `x` (base `e`), with one argument only — there is no `log(x, base)` form. The argument must be a number greater than 0.

```kabir
print(log(0))
```

Error:

```text
demo.kbr:1:10: log() needs a number greater than 0, not 0
  print(log(0))
           ^
```

### exp

`exp(x)` returns `e` to the power of `x`. The argument must be a number. A result too large to calculate is a friendly error, like the `**` overflow error.

```kabir
print(log(1))
print(log(10))
print(exp(0))
print(exp(1))
```

Output:

```text
0
2.302585092994046
1
2.718281828459045
```

### pi and e

`pi` and `e` are global numbers — the constants `3.141592653589793` and `2.718281828459045` — not functions. Like any built-in name, a `let` with the same name in your program shadows them in that scope. `type(pi)` is `"number"`.

```kabir
print(pi)
print(e)
print(type(pi))
```

Output:

```text
3.141592653589793
2.718281828459045
number
```

### read_file

`read_file(path)` reads a whole text file and returns its contents as a string. The argument must be a string path. A missing file, a directory, or an unreadable file is a runtime error. Paths are relative to the current working directory, not to the `.kbr` file.

```kabir
let text = read_file("notes.txt")
print(text)
```

### write_file

`write_file(path, contents)` writes a string to a file, creating it if it does not exist and replacing its contents if it does. It returns `null`. Both arguments must be strings. The parent directory must already exist — `write_file` does not create directories.

```kabir
write_file("out.txt", "hello\n")
print(read_file("out.txt"))
```

### file_exists

`file_exists(path)` returns `true` if the path exists (file or directory) and `false` otherwise. The argument must be a string. Unlike `read_file`, it never errors on a missing path.

### json_parse

`json_parse(text)` parses a JSON string into a Kabir value — objects become dicts, arrays become arrays, `true`/`false`/`null` become bools/null, numbers become numbers. The argument must be a string, and invalid JSON is a runtime error. One quirk: a literal `{` cannot appear inside a Kabir string (it starts interpolation), so JSON object text must come from `read_file`, `http_get`, or `json_stringify`, not from a string literal in your program.

### json_stringify

`json_stringify(value)` converts any Kabir value to a JSON string — dicts, arrays, strings, numbers, bools, and null all round-trip through `json_parse`. Functions cannot be stringified (a runtime error).

### http_get

`http_get(url)` fetches a URL over HTTP and returns the response body as a string. The argument must be a string URL. It follows redirects (up to 5), times out after 10 seconds, and a failed request (bad host, non-2xx status) is a runtime error. HTTPS is supported in the VM/interpreter path; the native backend supports `http://` only (see [Native compilation](#native-compilation)).

### http_post

`http_post(url, body)` sends a POST request with a string body and returns the response body as a string. Same timeout, redirect, and error rules as `http_get`.

### now

`now()` returns the current time as a number — seconds since the Unix epoch (1970-01-01), with fractions. It takes no arguments.

```kabir
let start = now()
sleep(0.1)
print(now() - start)
```

### sleep

`sleep(seconds)` pauses the program for the given number of seconds (fractions allowed) and returns `null`. The argument must be a number, and negative values are an error.

## Errors

Every Kabir error has the same shape: `file:line:col`, a plain-language message, the offending source line, and a caret pointing at the problem. There are no Python tracebacks. Lex errors (bad characters, unterminated strings), parse errors (missing `}`, a bad expression), and runtime errors (undefined variable, division by zero, wrong argument count) all render this way.

```kabir
let x = y
```

Error:

```text
demo.kbr:1:9: Undefined variable 'y' -- it was never defined with 'let'. Check the spelling
  let x = y
          ^
```

```kabir
print(1 / 0)
```

Error:

```text
demo.kbr:1:9: Division by zero -- the number after '/' cannot be 0
  print(1 / 0)
          ^
```

Common errors and what they mean:

| Error | Cause |
| --- | --- |
| `Undefined variable 'x'` | The name was never defined with `let` (check spelling) |
| `Cannot assign to 'x' -- it was never defined` | `=` on a name with no `let` |
| `Division by zero` | `/` or `%` with `0` on the right |
| `Array index N is out of range` | Index past the end (or before the start, after negative indexing) |
| `No key 'k' in this dict` | Dict read of a missing key (the error lists the keys that exist) |
| `Cannot loop over X` | `for` over a number, boolean, or `null` |
| `'break' can only be used inside a loop` | `break`/`continue` outside a loop |
| `'return' can only be used inside a function` | `return` at the top level |
| `Function 'f' takes N argument(s), but got M` | Wrong argument count |
| `Too many nested function calls` | Recursion deeper than 500 calls |
| `Strings use double quotes, not single quotes` | `'...'` instead of `"..."` |
| `Unterminated string` | Missing closing `"` (or a string spanning lines) |
| `Unknown escape sequence` | A `\` escape other than `\n`, `\t`, `\r`, `\"`, `\\` |
| `Dict keys must be strings` | A non-string key, or an interpolated key, in a dict literal |
| `Only a variable or an index can be assigned to` | Something like `1 = 2` or `f() = 3` |

Exit code is `1` for a Kabir error, `2` for a usage error (unknown command, missing file). A program that runs cleanly exits `0`.

## Formatter

`kabir fmt` rewrites `.kbr` files in one canonical style. It parses the file first, so a file with a syntax error is reported and left untouched — the formatter never writes a file it cannot parse.

```console
$ kabir fmt my_program.kbr          # format in place
$ kabir fmt --check examples/*.kbr  # list files that would change, change nothing
```

With `--check`, `kabir fmt` prints each file that would change (`kabir fmt: <file> would change`) and exits `1`; it exits `0` when everything is already formatted. Formatting twice is a no-op: the second run changes nothing.

The canonical style:

- **Indentation** — 4 spaces per level, one statement per line.
- **Spacing** — one space around binary operators (`a + b`, not `a+b`) and after commas (`f(1, 2)`, not `f(1,2)`); no space before a comma, `(` or `[` in calls and indexing.
- **Braces** — the `{` of `if` / `elif` / `else` / `while` / `for` / `func` stays on the header line; the closing `}` sits on its own line at the header's indent.
- **Blank lines** — at the top level, function declarations are separated from their neighbours by exactly one blank line, and a blank line after a comment block (the file header, a section note) is kept; no blank lines are inserted anywhere else (blank lines inside blocks are removed).
- **Collections** — short arrays and dicts stay on one line (up to 80 characters); longer ones break to one element per line, with no trailing comma (Kabir does not allow trailing commas).
- **Whitespace** — no trailing whitespace; the file ends with exactly one newline.

Two things the formatter never touches:

- **Comments** — a comment on its own line stays on its own line before the statement it preceded; a comment after code on a line stays at the end of that line. A comment it cannot place exactly (for example one inside a multi-line array literal) is kept as its own line right after the statement it was inside. Comments are never dropped.
- **Strings** — literal parts come back character for character (including `#` inside a string, which is text, not a comment), and interpolation expressions are re-formatted the same way as any other expression (`"{a+1}"` becomes `"{a + 1}"`).

Parentheses are added only where meaning needs them: `(a + b) * c` keeps its parentheses, `a + (b + c)` keeps them too (dropping them would change the grouping), and `a ** b ** c` stays unparenthesised because `**` is right-associative. Formatting never changes what a program prints — `kabir fmt` followed by `kabir run` gives exactly the same output as the unformatted file.

## REPL history

The REPL remembers your lines. Press up/down to recall previous lines, and the history is saved to `~/.kabir_history` when the REPL exits, so it is still there next time you start it. If `readline` is not available (or the history file cannot be read or written), the REPL simply runs without history — nothing else changes.

## Projects

For anything bigger than one file, make a project. `kabir new` scaffolds
it, `kabir build` compiles it to bytecode, and `kabir run` runs it.

```console
$ kabir new my_app
Created new Kabir project "my_app" in my_app
$ kabir build my_app
Built my_app v0.1.0 -> /path/to/my_app/target/my_app.kbc
$ kabir run my_app
Hello from my_app!
$ kabir run my_app/target/my_app.kbc
Hello from my_app!
```

A project is a directory with a `Kabir.toml` manifest:

```toml
[package]
name = "my_app"
version = "0.1.0"
authors = ["Afrain Kabir Rafid"]
```

plus `src/main.kbr` (the entry point — `build` compiles it and its
imports, resolved relative to `src/`) and a `.gitignore` that ignores
`target/` and `vendor/`. `kabir build` type-checks first, then writes the compiled
bytecode to `target/<name>.kbc` — a JSON file you can run directly with
`kabir run <file.kbc>`. Dependencies from the [package registry](#package-registry)
are vendored under `vendor/` and loaded with `import` by path, as always.

## Native compilation

`kabir build --native` and `kabir run --native` compile a Kabir program to C99 with the system `cc` and run the resulting binary. It is the fast path: `fib(25)` runs in about 0.007 s natively versus about 3.0 s on the VM (the one-off compile takes about 1.3 s, so native wins on compute-heavy programs, not on one-liners).

```console
$ kabir build --native my_app
Built my_app v0.1.0 (native, arena) -> ./target/my_app
$ ./target/my_app
Hello from my_app!
```

Memory model is **native (arena)**: values are freed all at once at exit, not refcounted. Supported: numbers, strings, bools, null, arrays, dicts, `if`/`while`/`for`, functions including recursion, all built-ins and stdlib functions, string methods, `match`, and `let mut` (no runtime effect). Closures work too (7.0.0): a function that reads a variable from an enclosing function captures it — returned, stored, and passed closures keep working, and `let mut` captures stay mutable. `import "path.kbr"` works too (7.0.0): the imported module is compiled to native as well, its top-level code runs once, and its bindings are copied into the importer. **Not supported**: async/await and `spawn`/`chan`/`send`/`recv` (VM-only — clear compile-time errors), and bare-name package imports (`import greet`, waiting on the shared package resolver). Numbers are C doubles (integers beyond 2^53 lose precision), and `http_get`/`http_post` are `http://` only in native mode. Native runtime errors print `Kabir runtime error: <message>` and exit 1, without `file:line:col`.

## Package registry

Kabir has a GitHub-based package registry — no server. A registry is an `index.json` file served over HTTP:

```json
{"packages": {"greet": {"1.0.0": {"url": "https://example.com/greet-1.0.0.tar.gz", "sha256": "..."}}}}
```

`kabir add <name>@<version> [dir]` fetches the tarball, verifies its SHA-256, vendors it into `vendor/<name>/<version>/`, records the exact version in `[dependencies]` in `Kabir.toml`, and writes `Kabir.lock`. Versions must be exact — no ranges. `kabir publish [dir]` builds a deterministic tarball of your project (`target/<name>-<version>.tar.gz`), prints its SHA-256 and the registry-entry JSON to merge into the registry repo's `index.json`, and pushes nothing itself. The registry URL comes from the `KABIR_REGISTRY` environment variable; the built-in default points at a registry that does not exist yet, so `kabir add` refuses with a clear error until a real registry URL is set. Vendored packages are loaded with `import` by path and are ignored by git (`vendor/` is in the scaffold `.gitignore`).

## Ownership-lite (`let mut`) and immutable-by-default

Since 6.0.0, plain `let` is **immutable**: reassigning it (`x = ...`, `x += ...`, `x -= ...`) is a compile-time error. `let mut` is the mutable form. Function parameters, `for` loop variables, and `match` bindings are immutable too. `let mut` also marks a value as owned:

```kabir
let mut xs = [1, 2, 3]
let ys = xs        # moves xs into ys; xs is unusable after this
print(ys)          # [1, 2, 3]
```

Moving applies to arrays and dicts (Copy values — numbers, strings, bools, null — are never moved). A moved value used again is a compile-time error, and reassignment revives it (`xs = [9]` makes `xs` usable again). Passing an owned value as an argument moves it — **except** to a read-only builtin, which borrows it instead: `print(xs)`, `len(xs)`, `str(xs)`, and the other read-only builtins (`num`, `type`, `range`, `sqrt`, `pow`, `abs`, `keys`, `int`, `float`, `input`) and read-only stdlib functions (`choice`, `json_stringify`) take a shared borrow for the call, so the value stays usable afterwards. The consuming builtins `push`, `pop`, and `send` still move. `&` / `&mut` borrowing is in 4.1.0 — see [Borrowing](#borrowing---mut).

```kabir
let mut xs = [1, 2, 3]
print(xs)          # borrows: xs stays usable
print(len(xs))     # 3, xs still usable
let ys = xs        # moves xs into ys; xs is unusable after this
```

One boundary to know: index assignment (`xs[0] = 9`) and `push`/`pop` mutate a value's *contents*, not the binding itself, so they still work on a plain `let` — only rebinding the name is forbidden. Migration for older programs: add `mut` to every binding you reassign.

## Borrowing (`&` / `&mut`)

Borrowing lets a function use a value without taking ownership of it. A shared borrow `&x` lets many readers look at `x`; a mutable borrow `&mut x` lets exactly one writer change it. Borrows are checked at compile time by the checker — at run time a borrow **is** the value (`&x` compiles to just `x`), so borrowing costs nothing at run time. The REPL runs without the checker, so it does not enforce borrow rules.

```kabir
let mut xs = [1, 2, 3]
let r = &xs        # shared borrow: read xs through r
print(r[0])        # 1

let m = &mut xs    # mutable borrow: change xs through m
m[0] = 9
print(m)           # [9, 2, 3]
```

Borrow types can appear in annotations:

```kabir
func first(items: &array) -> number {
    return items[0]
}

func set_first(items: &mut array) {
    items[0] = 99
}
```

## Dereferencing (`*`) and reborrowing

Since 7.0.0, a borrow can be dereferenced with `*`: `*r` is the value behind the borrow, usable in arithmetic and anywhere the plain type is expected. A deref costs nothing at run time — a borrow **is** the value, so `*r` compiles to just `r`.

```kabir
let x = 5
let r = &x
print(*r)          # 5
let y = *r + 1     # 6

func double(a: &number) -> number {
    return *a * 2
}
print(double(&x))  # 10
```

A mutable borrow can be reborrowed with `&mut *r`, and a value can be written through a mutable borrow with `*s = 10`:

```kabir
let mut x = 5
let r = &mut x
let s = &mut *r    # reborrow: s borrows through r
*s = 10
print(x)           # 10
```

Rules for reborrowing: a mutable reborrow needs a mutable borrow (`&mut *r` on a shared borrow is an error); while a reborrow is live, the original cannot be used or written through; a second reborrow while one is live is an error; writing through a shared borrow is an error. A plain `&r` (no `*`) is still an error — reborrowing goes through `*`.

## Lifetimes (`'a`)

Since 7.0.0, functions can declare explicit lifetime parameters: `func f<'a>(x: &'a number) -> &'a number`. A lifetime must be declared on the function before use, must be used at least once, and must not be duplicated. A returned `&'a T` needs a parameter carrying that same lifetime; inside the function body, a value must carry exactly the lifetime the return type (or annotation) requires — a borrow with a different named lifetime, or with no lifetime at all, does not satisfy `'a`:

```kabir
func first<'a>(x: &'a number) -> &'a number {
    return x
}

func bad<'a>(x: &'a number, y: &number) -> &'a number {
    return y   # error: y has no lifetime, it does not live as long as 'a
}
```

At a call site lifetimes are generic: each argument instantiates the callee's lifetime parameter, so a single argument of any lifetime is accepted. But when the callee requires the *same* lifetime in two or more parameter positions, all arguments in those positions must carry the same named lifetime:

```kabir
func pick<'a>(x: &'a number, y: &'a number) -> &'a number {
    return x
}

func caller<'a, 'b>(x: &'a number, y: &'b number) -> &'a number {
    return pick(x, y)   # error: 'a and 'b are different lifetimes
}
```

A function that returns an elided borrow (`-> &number`, no lifetime) keeps the older elision rule: it must take exactly one borrowed parameter.

Rules the checker enforces (compile-time errors otherwise):

1. While a mutable borrow of a place is live, no other access to that place — no read, no write, no move.
2. While shared borrows of a place are live, no mutation or move of that place.
3. A borrowed place cannot be moved while it is borrowed.
4. A borrow ends at its last use (NLL-lite: liveness is a simple last-use walk within a function body). A borrow whose holder is never used ends at once.
5. A function returning a borrow must be tied to an input: a named return lifetime `&'a T` needs a parameter carrying lifetime `'a`; an elided return needs exactly one borrowed parameter. Returning a borrow of a local is an error — only a borrow of a borrowed parameter (or of a top-level binding) may be returned.

Documented limits:

- **Borrows of `a[i]` are treated as borrowing the whole container** — borrowing one element borrows the array.
- **No interprocedural tracking beyond signatures** — what a function does with a borrow is checked only through its declared parameter and return types. A borrow is tracked within one function body only.
- **A reborrow keeps its source borrow's lifetime** — the checker does not track that a reborrow's true lifetime is shorter than its source's; lifetime checking is name-based, so a reborrow is treated as living as long as the borrow it came from.
- **Writing through a borrowed parameter of a Copy type does not reach the caller** — `func bump(r: &mut number) { *r = *r + 1 }` updates the parameter, not the caller's variable, because a borrow **is** the value at run time and a number has no identity to share. (Arrays and dicts do propagate: they are shared objects.) A write through a borrow whose chain is visible in the same function body does reach the original.
- **The self-hosting parser and compiler do not parse the new syntax yet** — `kabir run --selfhost` rejects `*r`, `&mut *r`, and `<'a>` programs. The Python parser, checker, VM, interpreter, and native backend all support them.

## Async/await + tasks and channels

Concurrency in Kabir is cooperative tasks, not threads. Mark a function `async`, run it as a task with `spawn`, and pass values between tasks through a channel. `await` suspends the current task until the thing it waits on is ready.

```kabir
async func worker(ch) {
    send(ch, 42)
}

async func main() {
    let ch = chan()
    let t = spawn(worker(ch))
    let answer = recv(ch)
    print(answer)     # 42
    await t           # wait for the task to finish
}

main()
```

- `async func f() { ... }` declares an async function. `async` and `await` are real keywords in 4.1.0 (like `import` became reserved in 1.1.0) — they cannot be used as variable names.
- `spawn(<call>)` starts a task and returns it. `await <task>` suspends the current task until that task completes.
- `chan()` creates a channel. `send(ch, v)` sends a value; `recv(ch)` receives one — `recv` on an empty channel blocks the task until a send arrives.
- The scheduler round-robins tasks cooperatively in 1000-instruction quanta: a task runs until it `await`s or blocks on `recv`, then another task gets a turn. A run waits for every spawned task before exiting; if the only live tasks are blocked with nothing able to wake them, the run fails with a deadlock error instead of hanging. At most 1000 live tasks.

Limits:

- **VM only.** Async runs on the bytecode VM; native async is out of scope — the native backend refuses async programs with a clear error before compiling.
- **The interpreter/REPL path rejects `async`** with a clear error ("async needs the VM path — run with `kabir run`").
- Channels are unbounded (no backpressure). There are no threads, so `input()` and `sleep()` block the whole scheduler.
- `await` is only legal inside an `async func` (a compile-time error otherwise, including at the top level). A demo program with two tasks and a channel lives in `examples/async-demo.kbr` (output: `main done` / `received: [10, 11, 12]` / `sum: 42`).

## Language server (`kabir lsp` + VS Code)

`kabir lsp` runs a Language Server over stdio, speaking LSP (JSON-RPC with `Content-Length` framing) so an editor can offer Kabir support: diagnostics, completion, hover, go-to-definition, and formatting.

```console
$ kabir lsp
```

Methods implemented:

- `initialize`, `initialized`, `shutdown`, `exit` — server lifecycle. Unknown methods get a proper `MethodNotFound` response, never a crash.
- `textDocument/didOpen`, `didChange`, `didClose` — the server tracks open files and re-checks them on every change.
- Diagnostics: on open/change the server runs the parser and the type checker and publishes diagnostics with file/line/col and a plain message — so type and borrow errors show in the editor as you type.
- `textDocument/completion` — keywords, built-ins, stdlib functions, and in-scope names.
- `textDocument/hover` — for a variable, its declared/inferred type and its definition line.
- `textDocument/definition` — jump to the `let`/func definition in the same file.
- `textDocument/formatting` — delegates to `kabir fmt` (`format_source`).

The VS Code extension lives in `editors/vscode/` in the repo: `package.json` (language id `kabir`, `.kbr` files), `syntaxes/kabir.tmLanguage.json` (syntax highlighting), a minimal client that launches `kabir lsp`, and a README for installing it locally. Publishing it to the VS Code Marketplace is out of scope — that needs the author's account.

## WebAssembly target

`kabir build --target wasm` compiles a Kabir program to WebAssembly so it runs in a browser page. The backend is a **direct WASM backend for the bytecode VM**, written in Python — no Emscripten (it is not available in this environment). It emits a real `.wasm` binary plus a tiny HTML harness that loads the module and wires its output to a `<pre>`.

```console
$ kabir build --target wasm examples/fibonacci.kbr
Built fibonacci (wasm) -> target/fibonacci.wasm
```

`build --target wasm` also takes a project directory (entry from `Kabir.toml`); the outputs are `target/<name>.wasm` and `target/<name>.html`. The `.wasm` imports `print` and number formatting from an `env` module the host provides (`print_num`, `print_str`, `print_bool`, `print_nl`, `format_num`, `pow`, `parse_num`); the HTML harness implements those imports in JavaScript, and `run_wasm()` in `kabir/wasm.py` implements them in a Node harness for verification.

Supported: numbers, bools, null, strings (including interpolation), arrays, arithmetic, comparisons, `if`/`while`/`for`, `break`/`continue`, functions (including recursion and first-class values), `match`, and the builtins `print`, `range`, `len`, `abs`, `sqrt`, `pow`, `int`, `float`, `num`, `type`, `str`, `push`, `pop`, plus the constants `pi` and `e`.

Not supported yet: `import`, dicts, string methods, closures, `input`, and the file/JSON/HTTP/time stdlib — these are rejected with a clear error at build time. Runtime errors (wrong types, bad index, division by zero, undefined variable) trap the module instead of reporting a Kabir error message. Numbers are 64-bit floats, so integers are exact only up to 2^53.

Verified: `examples/fibonacci.kbr` and `examples/fizzbuzz.kbr` compile to `.wasm` and produce output byte-identical to the VM run (38 tests in `tests/test_wasm.py`).

## Self-hosting (the compiler)

5.0.0 completes self-hosting: the lexer, the parser, AND the compiler are all written in Kabir itself — `kabir/selfhost/lexer.kbr`, `kabir/selfhost/parser.kbr`, and `kabir/selfhost/compiler.kbr` (the compiler embeds the lexer and parser). The Python implementation stays as the bootstrap compiler.

The proof it works is a **bootstrap check**: the Kabir parser, run through the Python VM over the full corpus (every `.kbr` in the repo plus the test fixtures), produces ASTs byte-identical to the Python parser's — 41 grammar snippets, 11 error cases (both sides agree, including line numbers), and all 13 `.kbr` files in the repo, including `parser.kbr` parsing itself. The Kabir compiler likewise produces bytecode byte-identical to `compile_source()` — 26 snippets, 6 error cases, all 11 examples, and `compiler.kbr` compiling itself (~28 s). Kabir-compiled bytecode also runs in the VM, not just matches textually.

Run a program with the Kabir compiler: `kabir run --selfhost <file.kbr>` — it type-checks with the Python checker, compiles with the Kabir compiler, and runs the result on the VM. The type checker and the VM are still Python-only, and the Kabir compiler is much slower than Python's, so `--selfhost` is a proof and a tool for working on the compiler, not the default path.

The lexer alone was step 1 in 4.1.0; 5.0.0 finishes the compiler. What is still Python: the type checker (`kabir/checker.py`), the VM (`kabir/vm.py`), the native/WASM backends, and the LSP.

## What Kabir does not have (yet)

To avoid surprises, the things people most often expect that are **not** in 6.0.0:

- **Dot access on dicts** — write `user["name"]`, not `user.name`.
- **`else` on loops, ternary `?:`** — none of these exist; `if`/`elif`/`else` covers the conditional ground.
- **A step argument for `range()`** — `range(a, b)` only; there is no `range(a, b, step)`.
- **Assignment as an expression** — `let x = y = 1` and `print(x = 1)` are errors.
- **Tail-call optimisation** — recursion is capped at 500 nested calls.
- **Closures and `import` in native mode** — both work since 7.0.0 (closures via captured arena slots, imports via per-module init functions); async and bare-name package imports are still VM-only / pending there.

The roadmap for these lives in the [README](../README.md#roadmap).
