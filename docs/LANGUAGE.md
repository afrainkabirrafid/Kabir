# Kabir Language Reference — version 1.0.0

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
- [Arrays](#arrays)
- [Dicts](#dicts)
- [Truthiness](#truthiness)
- [if / elif / else](#if--elif--else)
- [while](#while)
- [for](#for)
- [break and continue](#break-and-continue)
- [Functions](#functions)
- [Built-in functions](#built-in-functions)
- [Errors](#errors)
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

Dict keys must be strings — `d[0]` is an error. (There is no dot access: write `user["name"]`, not `user.name`.)

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

## Built-in functions

Kabir has 15 built-in functions. They are available in every program — no import needed — and they live in the global environment, so a `let` with the same name in your program shadows the built-in in that scope. Every built-in checks its argument count and types and reports mistakes in the same friendly error format.

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

## What Kabir does not have (yet)

To avoid surprises, the things people most often expect that are **not** in 1.0.0:

- **`import`** — a program is a single `.kbr` file; there is no way to load another file.
- **File I/O, random numbers, dates, networking** — `print` and `input` are the only way in or out.
- **Dot access on dicts** — write `user["name"]`, not `user.name`.
- **String methods, `switch`/`match`, `else` on loops, ternary `?:`** — none of these exist; `if`/`elif`/`else` covers the conditional ground.
- **A step argument for `range()`** — `range(a, b)` only; there is no `range(a, b, step)`.
- **Assignment as an expression** — `let x = y = 1` and `print(x = 1)` are errors.
- **Tail-call optimisation** — recursion is capped at 500 nested calls.
- **A JIT or native compilation** — Kabir is a tree-walking interpreter by design (see [ARCHITECTURE.md](ARCHITECTURE.md)).

The roadmap for these lives in the [README](../README.md#roadmap).
