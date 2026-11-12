/* Kabir native runtime -- see rt.h. C99, stdlib only.
 *
 * Memory model: ARENA. Every heap allocation goes through k_alloc()
 * and is freed in one pass by k_rt_cleanup(). Nothing is freed
 * earlier, so generated code never has to retain/release anything.
 * Reported honestly as "native (arena)".
 *
 * Semantics mirror kabir/semantics.py, kabir/builtins.py and
 * kabir/stdlib.py (same messages where practical).
 */
#define _POSIX_C_SOURCE 200809L
#define _DEFAULT_SOURCE 1

#include "rt.h"

#include <ctype.h>
#include <errno.h>
#include <math.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <time.h>
#include <unistd.h>

#include <arpa/inet.h>
#include <netdb.h>
#include <sys/socket.h>

/* -- arena ------------------------------------------------------------ */

static void **g_arena = NULL;
static size_t g_arena_len = 0;
static size_t g_arena_cap = 0;

static void arena_track(void *ptr) {
    if (g_arena_len == g_arena_cap) {
        size_t ncap = g_arena_cap ? g_arena_cap * 2 : 256;
        void **next = (void **)realloc(g_arena, ncap * sizeof(void *));
        if (!next) {
            fprintf(stderr, "Kabir runtime error: out of memory\n");
            exit(1);
        }
        g_arena = next;
        g_arena_cap = ncap;
    }
    g_arena[g_arena_len++] = ptr;
}

void *k_alloc(size_t n) {
    void *ptr = calloc(1, n ? n : 1);
    if (!ptr) {
        fprintf(stderr, "Kabir runtime error: out of memory\n");
        exit(1);
    }
    arena_track(ptr);
    return ptr;
}

char *k_strdup(const char *s) {
    size_t n = strlen(s);
    char *copy = (char *)k_alloc(n + 1);
    memcpy(copy, s, n + 1);
    return copy;
}

char *k_strndup(const char *s, size_t n) {
    char *copy = (char *)k_alloc(n + 1);
    memcpy(copy, s, n);
    copy[n] = '\0';
    return copy;
}

char *k_strcat_n(int n, const char **parts) {
    size_t total = 0;
    int i;
    for (i = 0; i < n; i++)
        total += strlen(parts[i]);
    char *out = (char *)k_alloc(total + 1);
    char *dst = out;
    for (i = 0; i < n; i++) {
        size_t len = strlen(parts[i]);
        memcpy(dst, parts[i], len);
        dst += len;
    }
    *dst = '\0';
    return out;
}

void k_rt_init(void) {
    srand((unsigned)time(NULL));
}

void k_rt_cleanup(void) {
    size_t i;
    for (i = 0; i < g_arena_len; i++)
        free(g_arena[i]);
    free(g_arena);
    g_arena = NULL;
    g_arena_len = 0;
    g_arena_cap = 0;
}

/* -- errors ----------------------------------------------------------- */

void k_error(const char *fmt, ...) {
    va_list ap;
    fflush(NULL);
    fprintf(stderr, "Kabir runtime error: ");
    va_start(ap, fmt);
    vfprintf(stderr, fmt, ap);
    va_end(ap);
    fprintf(stderr, "\n");
    exit(1);
}

/* -- constructors ----------------------------------------------------- */

KVal k_null(void) {
    KVal v;
    v.tag = K_NULL;
    v.u.num = 0;
    return v;
}

KVal k_num(double v) {
    KVal r;
    r.tag = K_NUM;
    r.u.num = v;
    return r;
}

KVal k_bool(int v) {
    KVal r;
    r.tag = K_BOOL;
    r.u.b = v ? 1 : 0;
    return r;
}

KVal k_str(const char *s) {
    KVal r;
    r.tag = K_STR;
    r.u.str = k_strdup(s);
    return r;
}

KVal k_strn(const char *s, size_t n) {
    KVal r;
    r.tag = K_STR;
    r.u.str = k_strndup(s, n);
    return r;
}

KVal k_new_array(void) {
    KVal r;
    r.tag = K_ARR;
    r.u.arr = (KArray *)k_alloc(sizeof(KArray));
    r.u.arr->items = NULL;
    r.u.arr->len = 0;
    r.u.arr->cap = 0;
    return r;
}

KVal k_new_dict(void) {
    KVal r;
    r.tag = K_DICT;
    r.u.dict = (KDict *)k_alloc(sizeof(KDict));
    r.u.dict->keys = NULL;
    r.u.dict->vals = NULL;
    r.u.dict->len = 0;
    r.u.dict->cap = 0;
    return r;
}

static KVal make_func(KFnPtr fn, const char *name, int arity,
                      int is_builtin) {
    KVal r;
    KFunc *f = (KFunc *)k_alloc(sizeof(KFunc));
    f->fn = fn;
    f->name = name;
    f->arity = arity;
    f->is_builtin = is_builtin;
    f->has_recv = 0;
    f->recv_str = NULL;
    r.tag = K_FUNC;
    r.u.fn = f;
    return r;
}

KVal k_make_func(KFnPtr fn, const char *name, int arity) {
    return make_func(fn, name, arity, 0);
}

KVal k_make_builtin(KFnPtr fn, const char *name, int arity) {
    return make_func(fn, name, arity, 1);
}

KVal k_make_bound_method(const char *name, KVal recv) {
    KVal r;
    KFunc *f = (KFunc *)k_alloc(sizeof(KFunc));
    f->fn = NULL; /* dispatched by name in k_call */
    f->name = name;
    f->arity = -1;
    f->is_builtin = 1;
    f->has_recv = 1;
    f->recv_str = recv.u.str;
    r.tag = K_FUNC;
    r.u.fn = f;
    return r;
}

static void array_push(KArray *arr, KVal v) {
    if (arr->len == arr->cap) {
        int ncap = arr->cap ? arr->cap * 2 : 8;
        KVal *next = (KVal *)k_alloc(sizeof(KVal) * (size_t)ncap);
        int i;
        for (i = 0; i < arr->len; i++)
            next[i] = arr->items[i];
        arr->items = next;
        arr->cap = ncap;
    }
    arr->items[arr->len++] = v;
}

static void dict_set_raw(KDict *d, const char *key, KVal v) {
    int i;
    for (i = 0; i < d->len; i++) {
        if (strcmp(d->keys[i], key) == 0) {
            d->vals[i] = v;
            return;
        }
    }
    if (d->len == d->cap) {
        int ncap = d->cap ? d->cap * 2 : 8;
        char **nkeys = (char **)k_alloc(sizeof(char *) * (size_t)ncap);
        KVal *nvals = (KVal *)k_alloc(sizeof(KVal) * (size_t)ncap);
        for (i = 0; i < d->len; i++) {
            nkeys[i] = d->keys[i];
            nvals[i] = d->vals[i];
        }
        d->keys = nkeys;
        d->vals = nvals;
        d->cap = ncap;
    }
    d->keys[d->len] = k_strdup(key);
    d->vals[d->len] = v;
    d->len++;
}

KVal k_array_of(int n, KVal *items) {
    KVal r = k_new_array();
    int i;
    for (i = 0; i < n; i++)
        array_push(r.u.arr, items[i]);
    return r;
}

KVal k_build_string(int n, const char **parts) {
    return k_str(k_strcat_n(n, parts));
}

KVal k_iter_items(KVal v) {
    KVal r;
    int i;
    if (v.tag == K_ARR) {
        /* Snapshot: the interpreter iterates over list(iterable). */
        r = k_new_array();
        for (i = 0; i < v.u.arr->len; i++)
            array_push(r.u.arr, v.u.arr->items[i]);
        return r;
    }
    if (v.tag == K_STR) {
        r = k_new_array();
        for (i = 0; v.u.str[i] != '\0'; i++)
            array_push(r.u.arr, k_strn(v.u.str + i, 1));
        return r;
    }
    if (v.tag == K_DICT) {
        r = k_new_array();
        for (i = 0; i < v.u.dict->len; i++)
            array_push(r.u.arr, k_str(v.u.dict->keys[i]));
        return r;
    }
    k_error("Cannot loop over %s -- 'for' needs an array, a string, "
            "or a range", k_type_name(v));
}

int k_array_len(KVal v) {
    if (v.tag != K_ARR)
        k_error("internal error: k_array_len on a non-array");
    return v.u.arr->len;
}

KVal k_array_get(KVal v, int i) {
    return v.u.arr->items[i];
}

/* -- values ----------------------------------------------------------- */

int k_is_truthy(KVal v) {
    switch (v.tag) {
    case K_NULL:
        return 0;
    case K_BOOL:
        return v.u.b;
    case K_NUM:
        return v.u.num != 0;
    case K_STR:
        return v.u.str[0] != '\0';
    case K_ARR:
        return v.u.arr->len > 0;
    case K_DICT:
        return v.u.dict->len > 0;
    case K_FUNC:
        return 1;
    }
    return 1;
}

const char *k_type_name(KVal v) {
    switch (v.tag) {
    case K_NULL:
        return "null";
    case K_BOOL:
        return "boolean";
    case K_NUM:
        return "number";
    case K_STR:
        return "string";
    case K_ARR:
        return "array";
    case K_DICT:
        return "dict";
    case K_FUNC:
        return "function";
    }
    return "function";
}

static int is_integral_double(double v) {
    return isfinite(v) && v == trunc(v);
}

char *k_format_num(double v) {
    if (isnan(v))
        return k_strdup("nan");
    if (isinf(v))
        return k_strdup(v < 0 ? "-inf" : "inf");
    if (is_integral_double(v) && fabs(v) < 9007199254740992.0) {
        char buf[64];
        snprintf(buf, sizeof(buf), "%lld", (long long)v);
        return k_strdup(buf);
    }
    /* Shortest %.*g that round-trips (mirrors Python repr). */
    {
        int prec;
        for (prec = 1; prec <= 17; prec++) {
            char buf[64];
            snprintf(buf, sizeof(buf), "%.*g", prec, v);
            if (strtod(buf, NULL) == v)
                return k_strdup(buf);
        }
    }
    {
        char buf[64];
        snprintf(buf, sizeof(buf), "%.17g", v);
        return k_strdup(buf);
    }
}

/* Format a value exactly like semantics.format_value. */
char *k_format(KVal v) {
    switch (v.tag) {
    case K_NULL:
        return k_strdup("null");
    case K_BOOL:
        return k_strdup(v.u.b ? "true" : "false");
    case K_NUM:
        return k_format_num(v.u.num);
    case K_STR:
        return k_strdup(v.u.str);
    case K_ARR: {
        /* "[" + ", ".join(items) + "]" */
        size_t total = 2;
        char **parts = NULL;
        int i;
        parts = (char **)k_alloc(sizeof(char *) *
                                 (size_t)(v.u.arr->len > 0 ? v.u.arr->len : 1));
        for (i = 0; i < v.u.arr->len; i++) {
            parts[i] = k_format(v.u.arr->items[i]);
            total += strlen(parts[i]) + 2;
        }
        {
            char *out = (char *)k_alloc(total + 1);
            char *dst = out;
            *dst++ = '[';
            for (i = 0; i < v.u.arr->len; i++) {
                size_t len = strlen(parts[i]);
                if (i > 0) {
                    *dst++ = ',';
                    *dst++ = ' ';
                }
                memcpy(dst, parts[i], len);
                dst += len;
            }
            *dst++ = ']';
            *dst = '\0';
            return out;
        }
    }
    case K_DICT: {
        size_t total = 2;
        char **parts = NULL;
        int i;
        parts = (char **)k_alloc(sizeof(char *) *
                                 (size_t)(v.u.dict->len > 0 ? v.u.dict->len : 1));
        for (i = 0; i < v.u.dict->len; i++) {
            char *val = k_format(v.u.dict->vals[i]);
            size_t need = strlen(v.u.dict->keys[i]) + strlen(val) + 6;
            parts[i] = (char *)k_alloc(need);
            snprintf(parts[i], need, "\"%s\": %s", v.u.dict->keys[i], val);
            total += strlen(parts[i]) + 2;
        }
        {
            char *out = (char *)k_alloc(total + 1);
            char *dst = out;
            *dst++ = '{';
            for (i = 0; i < v.u.dict->len; i++) {
                size_t len = strlen(parts[i]);
                if (i > 0) {
                    *dst++ = ',';
                    *dst++ = ' ';
                }
                memcpy(dst, parts[i], len);
                dst += len;
            }
            *dst++ = '}';
            *dst = '\0';
            return out;
        }
    }
    case K_FUNC:
        if (v.u.fn->is_builtin) {
            char buf[256];
            snprintf(buf, sizeof(buf), "<builtin function %s>",
                     v.u.fn->name);
            return k_strdup(buf);
        } else {
            char buf[256];
            snprintf(buf, sizeof(buf), "<function %s>", v.u.fn->name);
            return k_strdup(buf);
        }
    }
    return k_strdup("?");
}

int k_is_whole(KVal v) {
    return v.tag == K_NUM && is_integral_double(v.u.num);
}

int k_whole_int(KVal v, const char *fname) {
    if (!k_is_whole(v))
        k_error("%s needs whole numbers, not %s", fname, k_format(v));
    return (int)v.u.num;
}

/* -- operators (mirror semantics.binary_op / kabir_equal) ------------ */

static int is_num(KVal v) {
    return v.tag == K_NUM;
}

int k_eq(KVal a, KVal b) {
    int i;
    if (a.tag == K_BOOL || b.tag == K_BOOL)
        return a.tag == K_BOOL && b.tag == K_BOOL && a.u.b == b.u.b;
    if (a.tag == K_NULL || b.tag == K_NULL)
        return a.tag == K_NULL && b.tag == K_NULL;
    if (is_num(a) && is_num(b))
        return a.u.num == b.u.num;
    if (a.tag == K_STR && b.tag == K_STR)
        return strcmp(a.u.str, b.u.str) == 0;
    if (a.tag == K_ARR && b.tag == K_ARR) {
        if (a.u.arr->len != b.u.arr->len)
            return 0;
        for (i = 0; i < a.u.arr->len; i++)
            if (!k_eq(a.u.arr->items[i], b.u.arr->items[i]))
                return 0;
        return 1;
    }
    if (a.tag == K_DICT && b.tag == K_DICT) {
        KDict *da = a.u.dict, *db = b.u.dict;
        int j;
        if (da->len != db->len)
            return 0;
        for (i = 0; i < da->len; i++) {
            int found = 0;
            for (j = 0; j < db->len; j++) {
                if (strcmp(da->keys[i], db->keys[j]) == 0) {
                    if (!k_eq(da->vals[i], db->vals[j]))
                        return 0;
                    found = 1;
                    break;
                }
            }
            if (!found)
                return 0;
        }
        return 1;
    }
    if (a.tag == K_FUNC || b.tag == K_FUNC) {
        if (a.tag != K_FUNC || b.tag != K_FUNC)
            return 0;
        if (a.u.fn->fn != b.u.fn->fn)
            return 0;
        if (a.u.fn->has_recv != b.u.fn->has_recv)
            return 0;
        if (a.u.fn->has_recv)
            return strcmp(a.u.fn->recv_str, b.u.fn->recv_str) == 0
                && strcmp(a.u.fn->name, b.u.fn->name) == 0;
        return strcmp(a.u.fn->name, b.u.fn->name) == 0;
    }
    return 0;
}

KVal k_cmp(const char *op, KVal a, KVal b) {
    int result;
    if (is_num(a) && is_num(b)) {
        if (strcmp(op, "<") == 0)
            result = a.u.num < b.u.num;
        else if (strcmp(op, "<=") == 0)
            result = a.u.num <= b.u.num;
        else if (strcmp(op, ">") == 0)
            result = a.u.num > b.u.num;
        else
            result = a.u.num >= b.u.num;
        return k_bool(result);
    }
    if (a.tag == K_STR && b.tag == K_STR) {
        int c = strcmp(a.u.str, b.u.str);
        if (strcmp(op, "<") == 0)
            result = c < 0;
        else if (strcmp(op, "<=") == 0)
            result = c <= 0;
        else if (strcmp(op, ">") == 0)
            result = c > 0;
        else
            result = c >= 0;
        return k_bool(result);
    }
    k_error("Cannot compare %s with %s using '%s' -- compare numbers "
            "with numbers, or strings with strings",
            k_type_name(a), k_type_name(b), op);
}

static void check_two_nums(const char *op, KVal a, KVal b) {
    if (!is_num(a) || !is_num(b))
        k_error("'%s' needs two numbers, but got %s and %s", op,
                k_type_name(a), k_type_name(b));
}

KVal k_add(KVal a, KVal b) {
    if (is_num(a) && is_num(b))
        return k_num(a.u.num + b.u.num);
    if (a.tag == K_STR && b.tag == K_STR) {
        const char *parts[2];
        parts[0] = a.u.str;
        parts[1] = b.u.str;
        return k_str(k_strcat_n(2, parts));
    }
    if (a.tag == K_ARR && b.tag == K_ARR) {
        KVal r = k_new_array();
        int i;
        for (i = 0; i < a.u.arr->len; i++)
            array_push(r.u.arr, a.u.arr->items[i]);
        for (i = 0; i < b.u.arr->len; i++)
            array_push(r.u.arr, b.u.arr->items[i]);
        return r;
    }
    k_error("Cannot add %s and %s -- '+' works on two numbers, two "
            "strings, or two arrays",
            k_type_name(a), k_type_name(b));
}

KVal k_sub(KVal a, KVal b) {
    check_two_nums("-", a, b);
    return k_num(a.u.num - b.u.num);
}

KVal k_mul(KVal a, KVal b) {
    check_two_nums("*", a, b);
    return k_num(a.u.num * b.u.num);
}

KVal k_div(KVal a, KVal b) {
    check_two_nums("/", a, b);
    if (b.u.num == 0)
        k_error("Division by zero -- the number after '/' cannot be 0");
    /* Python: int / int that divides evenly stays an int (prints the
     * same either way as a double, but keep the value exact). */
    if (is_integral_double(a.u.num) && is_integral_double(b.u.num)
            && fmod(a.u.num, b.u.num) == 0)
        return k_num(a.u.num / b.u.num);
    return k_num(a.u.num / b.u.num);
}

KVal k_mod(KVal a, KVal b) {
    double r;
    check_two_nums("%", a, b);
    if (b.u.num == 0)
        k_error("Division by zero -- the number after '%' cannot be 0");
    /* Python-style modulo: sign follows the divisor. */
    r = fmod(a.u.num, b.u.num);
    if (r != 0 && ((r < 0) != (b.u.num < 0)))
        r += b.u.num;
    return k_num(r);
}

KVal k_pow(KVal a, KVal b) {
    double r;
    check_two_nums("**", a, b);
    r = pow(a.u.num, b.u.num);
    if (isnan(r) || isinf(r))
        k_error("That power is too large to calculate");
    return k_num(r);
}

KVal k_neg(KVal a) {
    if (!is_num(a))
        k_error("Cannot negate %s -- '-' needs a number", k_type_name(a));
    return k_num(-a.u.num);
}

KVal k_not(KVal a) {
    return k_bool(!k_is_truthy(a));
}

KVal k_len_val(KVal v) {
    if (v.tag == K_STR)
        return k_num((double)strlen(v.u.str));
    if (v.tag == K_ARR)
        return k_num((double)v.u.arr->len);
    if (v.tag == K_DICT)
        return k_num((double)v.u.dict->len);
    k_error("len() works on a string, array or dict, not %s",
            k_type_name(v));
}

static int index_to_int(KVal index, const char *what, int len) {
    int idx;
    if (!k_is_whole(index))
        k_error("%s index must be a whole number, not %s", what,
                k_type_name(index));
    idx = (int)index.u.num;
    if (idx < 0)
        idx += len;
    return idx;
}

KVal k_get_index(KVal target, KVal index) {
    if (target.tag == K_ARR) {
        int idx = index_to_int(index, "An array", target.u.arr->len);
        if (idx < 0 || idx >= target.u.arr->len)
            k_error("Array index %d is out of range -- the array has "
                    "%d item%s", (int)index.u.num, target.u.arr->len,
                    target.u.arr->len == 1 ? "" : "s");
        return target.u.arr->items[idx];
    }
    if (target.tag == K_STR) {
        int idx = index_to_int(index, "A string", (int)strlen(target.u.str));
        if (idx < 0 || idx >= (int)strlen(target.u.str))
            k_error("String index %d is out of range -- the string has "
                    "%d character%s", (int)index.u.num,
                    (int)strlen(target.u.str),
                    strlen(target.u.str) == 1 ? "" : "s");
        return k_strn(target.u.str + idx, 1);
    }
    if (target.tag == K_DICT) {
        int i;
        if (index.tag != K_STR)
            k_error("A dict key must be a string, not %s",
                    k_type_name(index));
        for (i = 0; i < target.u.dict->len; i++)
            if (strcmp(target.u.dict->keys[i], index.u.str) == 0)
                return target.u.dict->vals[i];
        /* "No key 'x' in this dict -- available keys: ..." */
        {
            size_t total = 0;
            char *list;
            char *dst;
            for (i = 0; i < target.u.dict->len; i++)
                total += strlen(target.u.dict->keys[i]) + 4;
            list = (char *)k_alloc(total + 8);
            dst = list;
            *dst = '\0';
            for (i = 0; i < target.u.dict->len; i++) {
                if (i > 0) {
                    *dst++ = ',';
                    *dst++ = ' ';
                }
                dst += sprintf(dst, "\"%s\"", target.u.dict->keys[i]);
            }
            if (target.u.dict->len == 0)
                strcpy(list, "none");
            k_error("No key '%s' in this dict -- available keys: %s",
                    index.u.str, list);
        }
    }
    k_error("Cannot index %s -- only arrays, strings and dicts support "
            "[ ... ]", k_type_name(target));
}

void k_set_index(KVal target, KVal index, KVal value) {
    if (target.tag == K_ARR) {
        int idx = index_to_int(index, "An array", target.u.arr->len);
        if (idx < 0 || idx >= target.u.arr->len)
            k_error("Array index %d is out of range -- the array has "
                    "%d item%s", (int)index.u.num, target.u.arr->len,
                    target.u.arr->len == 1 ? "" : "s");
        target.u.arr->items[idx] = value;
        return;
    }
    if (target.tag == K_DICT) {
        if (index.tag != K_STR)
            k_error("A dict key must be a string, not %s",
                    k_type_name(index));
        dict_set_raw(target.u.dict, index.u.str, value);
        return;
    }
    k_error("Cannot assign into %s -- only arrays and dicts support "
            "index assignment", k_type_name(target));
}

/* -- calls ------------------------------------------------------------ */

KVal k_call(KVal callee, KVal *args, int nargs) {
    KFunc *f;
    if (callee.tag != K_FUNC) {
        if (callee.tag == K_STR)
            k_error("'%s' is a string, not a function -- check that you "
                    "are calling the right name", callee.u.str);
        k_error("This value (%s) is not a function, so it cannot be "
                "called", k_type_name(callee));
    }
    f = callee.u.fn;
    if (f->has_recv)
        return k_str_method(f->name, k_str(f->recv_str), args, nargs);
    if (f->arity >= 0 && nargs != f->arity)
        k_error("Function '%s' takes %d argument%s, but got %d", f->name,
                f->arity, f->arity == 1 ? "" : "s", nargs);
    return f->fn(args, nargs);
}

static const char *METHOD_NAMES[] = {
    "upper", "lower", "strip", "split", "join", "contains",
    "starts_with", "ends_with", "replace", NULL
};

static const char *article(const char *word) {
    return (word[0] == 'a' || word[0] == 'e' || word[0] == 'i'
            || word[0] == 'o' || word[0] == 'u') ? "an" : "a";
}

KVal k_method(KVal recv, const char *name) {
    int i;
    if (recv.tag == K_STR) {
        for (i = 0; METHOD_NAMES[i]; i++)
            if (strcmp(METHOD_NAMES[i], name) == 0)
                return k_make_bound_method(name, recv);
        k_error("No method '.%s' on a string -- strings have no method "
                "named '%s'", name, name);
    }
    if (recv.tag == K_DICT)
        k_error("Cannot use .%s on a dict -- write dict[\"%s\"] instead "
                "(for example, user[\"name\"])", name, name);
    k_error("Cannot use .%s on %s %s -- only strings have methods",
            name, article(k_type_name(recv)), k_type_name(recv));
}

/* -- builtins --------------------------------------------------------- */

static void need_args(const char *name, int nargs, int count) {
    if (nargs != count)
        k_error("%s() takes exactly %d argument%s, but got %d", name,
                count, count == 1 ? "" : "s", nargs);
}

static double need_num(KVal v, const char *fname) {
    if (!is_num(v))
        k_error("%s() needs a number, not %s", fname, k_type_name(v));
    return v.u.num;
}

KVal k_b_print(KVal *args, int nargs) {
    int i;
    for (i = 0; i < nargs; i++) {
        if (i > 0)
            putchar(' ');
        fputs(k_format(args[i]), stdout);
    }
    putchar('\n');
    return k_null();
}

KVal k_b_input(KVal *args, int nargs) {
    char *line = NULL;
    size_t cap = 0;
    ssize_t got;
    const char *prompt = "";
    if (nargs > 1)
        k_error("input() takes at most 1 argument, but got %d", nargs);
    if (nargs == 1) {
        if (args[0].tag != K_STR)
            k_error("input() prompt must be a string, not %s",
                    k_type_name(args[0]));
        prompt = args[0].u.str;
    }
    fputs(prompt, stdout);
    fflush(stdout);
    got = getline(&line, &cap, stdin);
    if (got < 0) {
        free(line);
        k_error("No more input -- the input stream ended");
    }
    while (got > 0 && (line[got - 1] == '\n' || line[got - 1] == '\r'))
        line[--got] = '\0';
    {
        KVal r = k_str(line);
        free(line);
        return r;
    }
}

KVal k_b_len(KVal *args, int nargs) {
    need_args("len", nargs, 1);
    return k_len_val(args[0]);
}

KVal k_b_str(KVal *args, int nargs) {
    need_args("str", nargs, 1);
    return k_str(k_format(args[0]));
}

KVal k_b_num(KVal *args, int nargs) {
    char *end = NULL;
    double d;
    need_args("num", nargs, 1);
    if (is_num(args[0]))
        return args[0];
    if (args[0].tag == K_STR) {
        const char *text = args[0].u.str;
        /* Skip leading whitespace like Python int()/float(). */
        while (isspace((unsigned char)*text))
            text++;
        if (*text == '\0')
            k_error("Cannot turn '%s' into a number", args[0].u.str);
        {
            char *ep = NULL;
            long iv = strtol(text, &ep, 10);
            const char *tail = ep;
            while (isspace((unsigned char)*tail))
                tail++;
            if (*tail == '\0' && ep != text)
                return k_num((double)iv);
        }
        d = strtod(text, &end);
        if (end != text) {
            const char *tail = end;
            while (isspace((unsigned char)*tail))
                tail++;
            if (*tail == '\0')
                return k_num(d);
        }
        k_error("Cannot turn '%s' into a number", args[0].u.str);
    }
    k_error("num() works on a string or a number, not %s",
            k_type_name(args[0]));
}

KVal k_b_type(KVal *args, int nargs) {
    need_args("type", nargs, 1);
    return k_str(k_type_name(args[0]));
}

KVal k_b_range(KVal *args, int nargs) {
    KVal r;
    int start = 0, stop, i;
    if (nargs != 1 && nargs != 2)
        k_error("range() takes 1 or 2 arguments, but got %d", nargs);
    for (i = 0; i < nargs; i++)
        if (!k_is_whole(args[i]))
            k_error("range() needs whole numbers, not %s",
                    k_format(args[i]));
    if (nargs == 1) {
        stop = (int)args[0].u.num;
    } else {
        start = (int)args[0].u.num;
        stop = (int)args[1].u.num;
    }
    r = k_new_array();
    for (i = start; i < stop; i++)
        array_push(r.u.arr, k_num((double)i));
    return r;
}

KVal k_b_push(KVal *args, int nargs) {
    need_args("push", nargs, 2);
    if (args[0].tag != K_ARR)
        k_error("push() needs an array first, not %s",
                k_type_name(args[0]));
    array_push(args[0].u.arr, args[1]);
    return args[0];
}

KVal k_b_pop(KVal *args, int nargs) {
    need_args("pop", nargs, 1);
    if (args[0].tag != K_ARR)
        k_error("pop() needs an array, not %s", k_type_name(args[0]));
    if (args[0].u.arr->len == 0)
        k_error("pop() cannot remove from an empty array");
    return args[0].u.arr->items[--args[0].u.arr->len];
}

KVal k_b_sqrt(KVal *args, int nargs) {
    need_args("sqrt", nargs, 1);
    if (!is_num(args[0]))
        k_error("sqrt() needs a number, not %s", k_type_name(args[0]));
    if (args[0].u.num < 0)
        k_error("sqrt() cannot take the square root of a negative "
                "number");
    return k_num(sqrt(args[0].u.num));
}

KVal k_b_pow(KVal *args, int nargs) {
    double r;
    need_args("pow", nargs, 2);
    if (!is_num(args[0]) || !is_num(args[1]))
        k_error("pow() needs two numbers");
    r = pow(args[0].u.num, args[1].u.num);
    if (isnan(r) || isinf(r))
        k_error("That power is too large to calculate");
    return k_num(r);
}

KVal k_b_abs(KVal *args, int nargs) {
    need_args("abs", nargs, 1);
    if (!is_num(args[0]))
        k_error("abs() needs a number, not %s", k_type_name(args[0]));
    return k_num(fabs(args[0].u.num));
}

KVal k_b_keys(KVal *args, int nargs) {
    KVal r;
    int i;
    need_args("keys", nargs, 1);
    if (args[0].tag != K_DICT)
        k_error("keys() needs a dict, not %s", k_type_name(args[0]));
    r = k_new_array();
    for (i = 0; i < args[0].u.dict->len; i++)
        array_push(r.u.arr, k_str(args[0].u.dict->keys[i]));
    return r;
}

static long parse_int_str(const char *text, int *ok) {
    char *end = NULL;
    long v;
    while (isspace((unsigned char)*text))
        text++;
    v = strtol(text, &end, 10);
    if (end == text) {
        *ok = 0;
        return 0;
    }
    while (isspace((unsigned char)*end))
        end++;
    if (*end != '\0') {
        *ok = 0;
        return 0;
    }
    *ok = 1;
    return v;
}

KVal k_b_int(KVal *args, int nargs) {
    need_args("int", nargs, 1);
    if (args[0].tag == K_BOOL)
        k_error("int() cannot convert a boolean");
    if (is_num(args[0]))
        return k_num((double)(long long)args[0].u.num);
    if (args[0].tag == K_STR) {
        int ok = 0;
        long v = parse_int_str(args[0].u.str, &ok);
        if (ok)
            return k_num((double)v);
        {
            char *end = NULL;
            double d;
            const char *text = args[0].u.str;
            while (isspace((unsigned char)*text))
                text++;
            d = strtod(text, &end);
            if (end != text) {
                const char *tail = end;
                while (isspace((unsigned char)*tail))
                    tail++;
                if (*tail == '\0')
                    return k_num((double)(long long)d);
            }
        }
        k_error("Cannot turn '%s' into an integer", args[0].u.str);
    }
    k_error("int() works on a number or a string, not %s",
            k_type_name(args[0]));
}

KVal k_b_float(KVal *args, int nargs) {
    need_args("float", nargs, 1);
    if (args[0].tag == K_BOOL)
        k_error("float() cannot convert a boolean");
    if (is_num(args[0]))
        return k_num(args[0].u.num);
    if (args[0].tag == K_STR) {
        char *end = NULL;
        double d;
        const char *text = args[0].u.str;
        while (isspace((unsigned char)*text))
            text++;
        d = strtod(text, &end);
        if (end != text) {
            const char *tail = end;
            while (isspace((unsigned char)*tail))
                tail++;
            if (*tail == '\0')
                return k_num(d);
        }
        k_error("Cannot turn '%s' into a float", args[0].u.str);
    }
    k_error("float() works on a number or a string, not %s",
            k_type_name(args[0]));
}

/* -- string methods --------------------------------------------------- */

static char *ascii_case(const char *s, int upper) {
    char *out = k_strdup(s);
    char *p;
    for (p = out; *p; p++)
        *p = (char)(upper ? toupper((unsigned char)*p)
                          : tolower((unsigned char)*p));
    return out;
}

static KVal str_split_ws(const char *s) {
    /* Python "".split() with no args: runs of whitespace, no empties. */
    KVal r = k_new_array();
    const char *p = s;
    while (*p) {
        while (*p && isspace((unsigned char)*p))
            p++;
        if (!*p)
            break;
        {
            const char *start = p;
            while (*p && !isspace((unsigned char)*p))
                p++;
            array_push(r.u.arr, k_strn(start, (size_t)(p - start)));
        }
    }
    return r;
}

static KVal str_split_sep(const char *s, const char *sep) {
    KVal r = k_new_array();
    size_t seplen = strlen(sep);
    const char *p = s;
    const char *hit;
    while ((hit = strstr(p, sep)) != NULL) {
        array_push(r.u.arr, k_strn(p, (size_t)(hit - p)));
        p = hit + seplen;
    }
    array_push(r.u.arr, k_str(p));
    return r;
}

static char *str_replace_all(const char *s, const char *old,
                             const char *new_) {
    size_t oldlen = strlen(old);
    size_t newlen = strlen(new_);
    size_t cap, len = 0;
    char *out;
    const char *p = s;
    const char *hit;
    if (oldlen == 0) {
        /* Python: insert `new` before every char and at the end. */
        cap = strlen(s) * (newlen + 1) + newlen + 1;
        out = (char *)k_alloc(cap);
        out[0] = '\0';
        len = 0;
        memcpy(out + len, new_, newlen);
        len += newlen;
        while (*p) {
            out[len++] = *p++;
            memcpy(out + len, new_, newlen);
            len += newlen;
        }
        out[len] = '\0';
        return out;
    }
    cap = strlen(s) + 1;
    out = (char *)k_alloc(cap * 2 + 64);
    while ((hit = strstr(p, old)) != NULL) {
        size_t chunk = (size_t)(hit - p);
        if (len + chunk + newlen + 1 > cap) {
            cap = (len + chunk + newlen + 1) * 2;
            char *next = (char *)k_alloc(cap);
            memcpy(next, out, len);
            out = next;
        }
        memcpy(out + len, p, chunk);
        len += chunk;
        memcpy(out + len, new_, newlen);
        len += newlen;
        p = hit + oldlen;
    }
    {
        size_t rest = strlen(p);
        if (len + rest + 1 > cap) {
            cap = len + rest + 1;
            char *next = (char *)k_alloc(cap);
            memcpy(next, out, len);
            out = next;
        }
        memcpy(out + len, p, rest);
        len += rest;
    }
    out[len] = '\0';
    return out;
}

KVal k_str_method(const char *name, KVal recv, KVal *args, int nargs) {
    const char *s = recv.u.str;
    if (strcmp(name, "upper") == 0) {
        need_args("upper", nargs, 0);
        return k_str(ascii_case(s, 1));
    }
    if (strcmp(name, "lower") == 0) {
        need_args("lower", nargs, 0);
        return k_str(ascii_case(s, 0));
    }
    if (strcmp(name, "strip") == 0) {
        const char *start = s;
        const char *end;
        need_args("strip", nargs, 0);
        while (*start && isspace((unsigned char)*start))
            start++;
        end = s + strlen(s);
        while (end > start && isspace((unsigned char)end[-1]))
            end--;
        return k_strn(start, (size_t)(end - start));
    }
    if (strcmp(name, "split") == 0) {
        if (nargs != 0 && nargs != 1)
            k_error("split() takes 0 or 1 arguments, but got %d", nargs);
        if (nargs == 0)
            return str_split_ws(s);
        if (args[0].tag != K_STR)
            k_error("split() needs a string separator, not %s",
                    k_type_name(args[0]));
        if (args[0].u.str[0] == '\0')
            k_error("split() needs a non-empty separator");
        return str_split_sep(s, args[0].u.str);
    }
    if (strcmp(name, "join") == 0) {
        KVal r;
        size_t total = 0;
        char *out, *dst;
        int i;
        need_args("join", nargs, 1);
        if (args[0].tag != K_ARR)
            k_error("join() needs an array, not %s",
                    k_type_name(args[0]));
        for (i = 0; i < args[0].u.arr->len; i++) {
            KVal el = args[0].u.arr->items[i];
            if (el.tag != K_STR)
                k_error("join() needs an array of strings, not %s",
                        k_type_name(el));
            total += strlen(el.u.str);
        }
        total += strlen(s) * (size_t)(args[0].u.arr->len > 0
                                           ? args[0].u.arr->len - 1 : 0);
        out = (char *)k_alloc(total + 1);
        dst = out;
        for (i = 0; i < args[0].u.arr->len; i++) {
            const char *el = args[0].u.arr->items[i].u.str;
            size_t len = strlen(el);
            if (i > 0) {
                size_t sl = strlen(s);
                memcpy(dst, s, sl);
                dst += sl;
            }
            memcpy(dst, el, len);
            dst += len;
        }
        *dst = '\0';
        r.tag = K_STR;
        r.u.str = out;
        return r;
    }
    if (strcmp(name, "contains") == 0) {
        need_args("contains", nargs, 1);
        if (args[0].tag != K_STR)
            k_error("contains() needs a string, not %s",
                    k_type_name(args[0]));
        return k_bool(strstr(s, args[0].u.str) != NULL);
    }
    if (strcmp(name, "starts_with") == 0) {
        need_args("starts_with", nargs, 1);
        if (args[0].tag != K_STR)
            k_error("starts_with() needs a string, not %s",
                    k_type_name(args[0]));
        return k_bool(strncmp(s, args[0].u.str,
                              strlen(args[0].u.str)) == 0);
    }
    if (strcmp(name, "ends_with") == 0) {
        size_t sl = strlen(s), suflen;
        need_args("ends_with", nargs, 1);
        if (args[0].tag != K_STR)
            k_error("ends_with() needs a string, not %s",
                    k_type_name(args[0]));
        suflen = strlen(args[0].u.str);
        if (suflen > sl)
            return k_bool(0);
        return k_bool(strcmp(s + sl - suflen, args[0].u.str) == 0);
    }
    if (strcmp(name, "replace") == 0) {
        need_args("replace", nargs, 2);
        if (args[0].tag != K_STR)
            k_error("replace() needs strings, not %s",
                    k_type_name(args[0]));
        if (args[1].tag != K_STR)
            k_error("replace() needs strings, not %s",
                    k_type_name(args[1]));
        return k_str(str_replace_all(s, args[0].u.str, args[1].u.str));
    }
    k_error("No method '.%s' on a string", name);
}

/* -- stdlib: random / math / time -------------------------------------- */

KVal k_b_randint(KVal *args, int nargs) {
    int a, b;
    need_args("randint", nargs, 2);
    a = k_whole_int(args[0], "randint");
    b = k_whole_int(args[1], "randint");
    if (a > b)
        k_error("randint() needs the first number to be no greater than "
                "the second, but got %d and %d", a, b);
    return k_num((double)(a + rand() % (b - a + 1)));
}

KVal k_b_randrange(KVal *args, int nargs) {
    int a, b;
    if (nargs != 1 && nargs != 2)
        k_error("randrange() takes 1 or 2 arguments, but got %d", nargs);
    if (nargs == 1) {
        a = k_whole_int(args[0], "randrange");
        if (a <= 0)
            k_error("randrange() needs a number greater than 0, but got "
                    "%d", a);
        return k_num((double)(rand() % a));
    }
    a = k_whole_int(args[0], "randrange");
    b = k_whole_int(args[1], "randrange");
    if (a >= b)
        k_error("randrange() needs a non-empty range -- the first number "
                "must be less than the second, but got %d and %d", a, b);
    return k_num((double)(a + rand() % (b - a)));
}

KVal k_b_choice(KVal *args, int nargs) {
    need_args("choice", nargs, 1);
    if (args[0].tag != K_ARR)
        k_error("choice() needs an array, not %s", k_type_name(args[0]));
    if (args[0].u.arr->len == 0)
        k_error("choice() cannot choose from an empty array");
    return args[0].u.arr->items[rand() % args[0].u.arr->len];
}

KVal k_b_shuffle(KVal *args, int nargs) {
    int i;
    need_args("shuffle", nargs, 1);
    if (args[0].tag != K_ARR)
        k_error("shuffle() needs an array, not %s",
                k_type_name(args[0]));
    for (i = args[0].u.arr->len - 1; i > 0; i--) {
        int j = rand() % (i + 1);
        KVal tmp = args[0].u.arr->items[i];
        args[0].u.arr->items[i] = args[0].u.arr->items[j];
        args[0].u.arr->items[j] = tmp;
    }
    return args[0];
}

KVal k_b_seed(KVal *args, int nargs) {
    int n;
    need_args("seed", nargs, 1);
    n = k_whole_int(args[0], "seed");
    srand((unsigned)n);
    return k_null();
}

KVal k_b_floor(KVal *args, int nargs) {
    need_args("floor", nargs, 1);
    return k_num(floor(need_num(args[0], "floor")));
}

KVal k_b_ceil(KVal *args, int nargs) {
    need_args("ceil", nargs, 1);
    return k_num(ceil(need_num(args[0], "ceil")));
}

KVal k_b_sin(KVal *args, int nargs) {
    need_args("sin", nargs, 1);
    return k_num(sin(need_num(args[0], "sin")));
}

KVal k_b_cos(KVal *args, int nargs) {
    need_args("cos", nargs, 1);
    return k_num(cos(need_num(args[0], "cos")));
}

KVal k_b_tan(KVal *args, int nargs) {
    need_args("tan", nargs, 1);
    return k_num(tan(need_num(args[0], "tan")));
}

KVal k_b_log(KVal *args, int nargs) {
    double v;
    need_args("log", nargs, 1);
    v = need_num(args[0], "log");
    if (v <= 0)
        k_error("log() needs a number greater than 0, not %s",
                k_format(args[0]));
    return k_num(log(v));
}

KVal k_b_exp(KVal *args, int nargs) {
    double r;
    need_args("exp", nargs, 1);
    r = exp(need_num(args[0], "exp"));
    if (isinf(r))
        k_error("That power is too large to calculate");
    return k_num(r);
}

KVal k_b_now(KVal *args, int nargs) {
    struct timespec ts;
    (void)args;
    need_args("now", nargs, 0);
    clock_gettime(CLOCK_REALTIME, &ts);
    return k_num((double)ts.tv_sec + (double)ts.tv_nsec / 1e9);
}

KVal k_b_sleep(KVal *args, int nargs) {
    double seconds;
    struct timespec ts;
    need_args("sleep", nargs, 1);
    seconds = need_num(args[0], "sleep");
    if (seconds < 0)
        k_error("sleep() needs a number of seconds no less than 0, but "
                "got %s", k_format(args[0]));
    ts.tv_sec = (time_t)seconds;
    ts.tv_nsec = (long)((seconds - (double)ts.tv_sec) * 1e9);
    nanosleep(&ts, NULL);
    return k_null();
}

/* -- stdlib: file I/O --------------------------------------------------- */

static int valid_utf8(const char *s, size_t n) {
    size_t i = 0;
    while (i < n) {
        unsigned char c = (unsigned char)s[i];
        size_t extra = 0;
        uint32_t cp = 0;
        size_t j;
        if (c < 0x80) {
            i++;
            continue;
        }
        if ((c & 0xE0) == 0xC0) {
            extra = 1;
            cp = c & 0x1F;
            if (cp == 0)
                return 0; /* overlong */
        } else if ((c & 0xF0) == 0xE0) {
            extra = 2;
            cp = c & 0x0F;
        } else if ((c & 0xF8) == 0xF0) {
            extra = 3;
            cp = c & 0x07;
            if (cp > 4)
                return 0;
        } else {
            return 0;
        }
        if (i + extra >= n + 0 && i + extra > n - 1 && i + extra >= n)
            return 0;
        if (i + extra >= n)
            return 0;
        for (j = 1; j <= extra; j++) {
            unsigned char cc = (unsigned char)s[i + j];
            if ((cc & 0xC0) != 0x80)
                return 0;
            cp = (cp << 6) | (cc & 0x3F);
        }
        if (extra == 2 && cp < 0x800)
            return 0;
        if (extra == 3 && cp < 0x10000)
            return 0;
        if (cp >= 0xD800 && cp <= 0xDFFF)
            return 0;
        if (cp > 0x10FFFF)
            return 0;
        i += extra + 1;
    }
    return 1;
}

KVal k_b_read_file(KVal *args, int nargs) {
    FILE *handle;
    char *buf = NULL;
    size_t len = 0, cap = 0;
    need_args("read_file", nargs, 1);
    if (args[0].tag != K_STR)
        k_error("read_file() needs a string path, not %s",
                k_type_name(args[0]));
    handle = fopen(args[0].u.str, "rb");
    if (!handle) {
        if (errno == ENOENT)
            k_error("Cannot read file '%s' -- no such file",
                    args[0].u.str);
        if (errno == EISDIR)
            k_error("Cannot read file '%s' -- it is a directory",
                    args[0].u.str);
        if (errno == EACCES)
            k_error("Cannot read file '%s' -- permission denied",
                    args[0].u.str);
        k_error("Cannot read file '%s' -- %s", args[0].u.str,
                strerror(errno));
    }
    for (;;) {
        size_t want;
        size_t got;
        if (len == cap) {
            cap = cap ? cap * 2 : 4096;
            buf = (char *)realloc(buf, cap);
            if (!buf)
                k_error("out of memory");
        }
        want = cap - len;
        got = fread(buf + len, 1, want, handle);
        len += got;
        if (got < want) {
            if (ferror(handle)) {
                fclose(handle);
                free(buf);
                k_error("Cannot read file '%s' -- %s", args[0].u.str,
                        strerror(errno));
            }
            break;
        }
    }
    fclose(handle);
    if (!valid_utf8(buf ? buf : "", len)) {
        free(buf);
        k_error("Cannot read file '%s' -- it is not valid UTF-8 text",
                args[0].u.str);
    }
    {
        KVal r = k_strn(buf ? buf : "", len);
        free(buf);
        return r;
    }
}

KVal k_b_write_file(KVal *args, int nargs) {
    FILE *handle;
    need_args("write_file", nargs, 2);
    if (args[0].tag != K_STR)
        k_error("write_file() needs a string path, not %s",
                k_type_name(args[0]));
    if (args[1].tag != K_STR)
        k_error("write_file() needs a string to write, not %s",
                k_type_name(args[1]));
    handle = fopen(args[0].u.str, "wb");
    if (!handle) {
        if (errno == EISDIR)
            k_error("Cannot write file '%s' -- it is a directory",
                    args[0].u.str);
        if (errno == EACCES)
            k_error("Cannot write file '%s' -- permission denied",
                    args[0].u.str);
        k_error("Cannot write file '%s' -- %s", args[0].u.str,
                strerror(errno));
    }
    if (fwrite(args[1].u.str, 1, strlen(args[1].u.str), handle)
            != strlen(args[1].u.str)) {
        fclose(handle);
        k_error("Cannot write file '%s' -- %s", args[0].u.str,
                strerror(errno));
    }
    fclose(handle);
    return k_null();
}

KVal k_b_file_exists(KVal *args, int nargs) {
    struct stat st;
    need_args("file_exists", nargs, 1);
    if (args[0].tag != K_STR)
        k_error("file_exists() needs a string path, not %s",
                k_type_name(args[0]));
    if (stat(args[0].u.str, &st) != 0)
        return k_bool(0);
    return k_bool(S_ISREG(st.st_mode));
}

/* -- JSON --------------------------------------------------------------- */

typedef struct {
    const char *p;
} JsonParser;

static void json_skip_ws(JsonParser *js) {
    while (*js->p == ' ' || *js->p == '\t' || *js->p == '\n'
           || *js->p == '\r')
        js->p++;
}

static KVal json_parse_value(JsonParser *js);

static void json_utf8_encode(char *out, size_t *len, uint32_t cp) {
    if (cp < 0x80) {
        out[(*len)++] = (char)cp;
    } else if (cp < 0x800) {
        out[(*len)++] = (char)(0xC0 | (cp >> 6));
        out[(*len)++] = (char)(0x80 | (cp & 0x3F));
    } else if (cp < 0x10000) {
        out[(*len)++] = (char)(0xE0 | (cp >> 12));
        out[(*len)++] = (char)(0x80 | ((cp >> 6) & 0x3F));
        out[(*len)++] = (char)(0x80 | (cp & 0x3F));
    } else {
        out[(*len)++] = (char)(0xF0 | (cp >> 18));
        out[(*len)++] = (char)(0x80 | ((cp >> 12) & 0x3F));
        out[(*len)++] = (char)(0x80 | ((cp >> 6) & 0x3F));
        out[(*len)++] = (char)(0x80 | (cp & 0x3F));
    }
}

static uint32_t json_parse_hex4(JsonParser *js) {
    uint32_t v = 0;
    int i;
    for (i = 0; i < 4; i++) {
        char c = *js->p++;
        v <<= 4;
        if (c >= '0' && c <= '9')
            v |= (uint32_t)(c - '0');
        else if (c >= 'a' && c <= 'f')
            v |= (uint32_t)(c - 'a' + 10);
        else if (c >= 'A' && c <= 'F')
            v |= (uint32_t)(c - 'A' + 10);
        else
            k_error("Cannot parse JSON -- invalid \\u escape");
    }
    return v;
}

static KVal json_parse_string(JsonParser *js) {
    /* js->p is at the opening quote. */
    char *buf = NULL;
    size_t len = 0, cap = 0;
    js->p++;
    for (;;) {
        unsigned char c = (unsigned char)*js->p;
        if (c == '\0')
            k_error("Cannot parse JSON -- unterminated string");
        if (c == '"') {
            js->p++;
            break;
        }
        if (c == '\\') {
            char e;
            js->p++;
            e = *js->p++;
            if (len + 4 >= cap) {
                cap = cap ? cap * 2 : 64;
                buf = (char *)realloc(buf, cap);
            }
            switch (e) {
            case '"':
                buf[len++] = '"';
                break;
            case '\\':
                buf[len++] = '\\';
                break;
            case '/':
                buf[len++] = '/';
                break;
            case 'b':
                buf[len++] = '\b';
                break;
            case 'f':
                buf[len++] = '\f';
                break;
            case 'n':
                buf[len++] = '\n';
                break;
            case 'r':
                buf[len++] = '\r';
                break;
            case 't':
                buf[len++] = '\t';
                break;
            case 'u': {
                uint32_t cp = json_parse_hex4(js);
                if (cp >= 0xD800 && cp <= 0xDBFF
                        && js->p[0] == '\\' && js->p[1] == 'u') {
                    uint32_t lo;
                    js->p += 2;
                    lo = json_parse_hex4(js);
                    if (lo >= 0xDC00 && lo <= 0xDFFF)
                        cp = 0x10000 + ((cp - 0xD800) << 10)
                             + (lo - 0xDC00);
                    else
                        k_error("Cannot parse JSON -- invalid "
                                "surrogate pair");
                }
                if (len + 4 >= cap) {
                    cap = cap ? cap * 2 : 64;
                    buf = (char *)realloc(buf, cap);
                }
                json_utf8_encode(buf, &len, cp);
                break;
            }
            default:
                k_error("Cannot parse JSON -- invalid escape '\\%c'", e);
            }
            continue;
        }
        if (c < 0x20)
            k_error("Cannot parse JSON -- control character in string");
        if (len + 1 >= cap) {
            cap = cap ? cap * 2 : 64;
            buf = (char *)realloc(buf, cap);
        }
        buf[len++] = (char)c;
        js->p++;
    }
    {
        KVal r = k_strn(buf ? buf : "", len);
        free(buf);
        return r;
    }
}

static KVal json_parse_value(JsonParser *js) {
    json_skip_ws(js);
    if (*js->p == '{') {
        KVal r = k_new_dict();
        js->p++;
        json_skip_ws(js);
        if (*js->p == '}') {
            js->p++;
            return r;
        }
        for (;;) {
            KVal key;
            KVal val;
            json_skip_ws(js);
            if (*js->p != '"')
                k_error("Cannot parse JSON -- expected a string key");
            key = json_parse_string(js);
            json_skip_ws(js);
            if (*js->p != ':')
                k_error("Cannot parse JSON -- expected ':'");
            js->p++;
            val = json_parse_value(js);
            dict_set_raw(r.u.dict, key.u.str, val);
            json_skip_ws(js);
            if (*js->p == ',') {
                js->p++;
                continue;
            }
            if (*js->p == '}') {
                js->p++;
                return r;
            }
            k_error("Cannot parse JSON -- expected ',' or '}'");
        }
    }
    if (*js->p == '[') {
        KVal r = k_new_array();
        js->p++;
        json_skip_ws(js);
        if (*js->p == ']') {
            js->p++;
            return r;
        }
        for (;;) {
            KVal val = json_parse_value(js);
            array_push(r.u.arr, val);
            json_skip_ws(js);
            if (*js->p == ',') {
                js->p++;
                continue;
            }
            if (*js->p == ']') {
                js->p++;
                return r;
            }
            k_error("Cannot parse JSON -- expected ',' or ']'");
        }
    }
    if (*js->p == '"')
        return json_parse_string(js);
    if (strncmp(js->p, "true", 4) == 0) {
        js->p += 4;
        return k_bool(1);
    }
    if (strncmp(js->p, "false", 5) == 0) {
        js->p += 5;
        return k_bool(0);
    }
    if (strncmp(js->p, "null", 4) == 0) {
        js->p += 4;
        return k_null();
    }
    if (*js->p == '-' || (*js->p >= '0' && *js->p <= '9')) {
        char *end = NULL;
        double d = strtod(js->p, &end);
        if (end == js->p)
            k_error("Cannot parse JSON -- invalid number");
        js->p = end;
        return k_num(d);
    }
    if (*js->p == '\0')
        k_error("Cannot parse JSON -- unexpected end of input");
    k_error("Cannot parse JSON -- unexpected character '%c'", *js->p);
}

KVal k_json_parse(const char *text) {
    JsonParser js;
    KVal v;
    js.p = text;
    v = json_parse_value(&js);
    json_skip_ws(&js);
    if (*js.p != '\0')
        k_error("Cannot parse JSON -- extra data after the value");
    return v;
}

static void json_escape_into(char **out, size_t *len, size_t *cap,
                             const char *s) {
    const unsigned char *p = (const unsigned char *)s;
    /* Grow helper: append one char, growing as needed. */
    for (; *p; p++) {
        char esc[8];
        const char *use = NULL;
        switch (*p) {
        case '"':
            use = "\\\"";
            break;
        case '\\':
            use = "\\\\";
            break;
        case '\b':
            use = "\\b";
            break;
        case '\f':
            use = "\\f";
            break;
        case '\n':
            use = "\\n";
            break;
        case '\r':
            use = "\\r";
            break;
        case '\t':
            use = "\\t";
            break;
        default:
            if (*p < 0x20) {
                snprintf(esc, sizeof(esc), "\\u%04x", *p);
                use = esc;
            }
            break;
        }
        if (use) {
            size_t n = strlen(use);
            while (*len + n + 1 > *cap) {
                *cap *= 2;
                *out = (char *)realloc(*out, *cap);
            }
            memcpy(*out + *len, use, n);
            *len += n;
        } else {
            while (*len + 2 > *cap) {
                *cap *= 2;
                *out = (char *)realloc(*out, *cap);
            }
            (*out)[(*len)++] = (char)*p;
        }
    }
}

static void json_write(KVal v, char **out, size_t *len, size_t *cap);

static void json_buf_putc(char **out, size_t *len, size_t *cap, char c) {
    while (*len + 2 > *cap) {
        *cap *= 2;
        *out = (char *)realloc(*out, *cap);
    }
    (*out)[(*len)++] = c;
}

static void json_buf_puts(char **out, size_t *len, size_t *cap,
                          const char *s) {
    size_t n = strlen(s);
    while (*len + n + 1 > *cap) {
        *cap *= 2;
        *out = (char *)realloc(*out, *cap);
    }
    memcpy(*out + *len, s, n);
    *len += n;
}

static void json_write(KVal v, char **out, size_t *len, size_t *cap) {
    int i;
    switch (v.tag) {
    case K_NULL:
        json_buf_puts(out, len, cap, "null");
        return;
    case K_BOOL:
        json_buf_puts(out, len, cap, v.u.b ? "true" : "false");
        return;
    case K_NUM:
        json_buf_puts(out, len, cap, k_format_num(v.u.num));
        return;
    case K_STR:
        json_buf_putc(out, len, cap, '"');
        json_escape_into(out, len, cap, v.u.str);
        json_buf_putc(out, len, cap, '"');
        return;
    case K_ARR:
        json_buf_putc(out, len, cap, '[');
        for (i = 0; i < v.u.arr->len; i++) {
            if (i > 0)
                json_buf_puts(out, len, cap, ", ");
            json_write(v.u.arr->items[i], out, len, cap);
        }
        json_buf_putc(out, len, cap, ']');
        return;
    case K_DICT:
        json_buf_putc(out, len, cap, '{');
        for (i = 0; i < v.u.dict->len; i++) {
            if (i > 0)
                json_buf_puts(out, len, cap, ", ");
            json_buf_putc(out, len, cap, '"');
            json_escape_into(out, len, cap, v.u.dict->keys[i]);
            json_buf_puts(out, len, cap, "\": ");
            json_write(v.u.dict->vals[i], out, len, cap);
        }
        json_buf_putc(out, len, cap, '}');
        return;
    case K_FUNC:
        k_error("Cannot turn %s into JSON -- Object of type function "
                "is not JSON serializable", k_format(v));
    }
}

char *k_json_stringify(KVal v) {
    size_t cap = 256, len = 0;
    char *out = (char *)malloc(cap);
    if (!out)
        k_error("out of memory");
    json_write(v, &out, &len, &cap);
    out[len] = '\0';
    /* Hand the buffer to the arena so cleanup frees it. */
    {
        char *copy = k_strndup(out, len);
        free(out);
        return copy;
    }
}

KVal k_b_json_parse(KVal *args, int nargs) {
    need_args("json_parse", nargs, 1);
    if (args[0].tag != K_STR)
        k_error("json_parse() needs a string, not %s",
                k_type_name(args[0]));
    return k_json_parse(args[0].u.str);
}

KVal k_b_json_stringify(KVal *args, int nargs) {
    need_args("json_stringify", nargs, 1);
    return k_str(k_json_stringify(args[0]));
}

/* -- HTTP (http:// only) ---------------------------------------------- */

static void set_timeout(int fd) {
    struct timeval tv;
    tv.tv_sec = 10;
    tv.tv_usec = 0;
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
    setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));
}

char *k_http_request(const char *url, const char *method,
                     const char *body) {
    const char *fname = strcmp(method, "POST") == 0 ? "http_post"
                                                    : "http_get";
    const char *p;
    char host[256];
    char path[2048];
    int port = 80;
    struct addrinfo hints;
    struct addrinfo *res = NULL;
    int fd = -1;
    char request[4096];
    char *resp = NULL;
    size_t resp_len = 0, resp_cap = 0;
    char *hdr_end;
    char *body_start;
    int status = 0;

    if (strncmp(url, "http://", 7) != 0) {
        if (strncmp(url, "https://", 8) == 0)
            k_error("%s('%s') failed -- https URLs are not supported "
                    "in native mode yet (http:// only)", fname, url);
        k_error("%s('%s') failed -- the URL must start with http://",
                fname, url);
    }
    p = url + 7;
    {
        const char *slash = strchr(p, '/');
        const char *colon;
        size_t hostlen;
        if (slash) {
            hostlen = (size_t)(slash - p);
            if (hostlen >= sizeof(path))
                k_error("%s('%s') failed -- URL path too long", fname,
                        url);
            strcpy(path, slash);
        } else {
            hostlen = strlen(p);
            strcpy(path, "/");
        }
        colon = (const char *)memchr(p, ':', hostlen);
        if (colon) {
            char portbuf[16];
            size_t plen = hostlen - (size_t)(colon - p) - 1;
            if (hostlen - (size_t)(colon - p) - 1 >= sizeof(host))
                k_error("%s('%s') failed -- host too long", fname, url);
            if (plen >= sizeof(portbuf))
                k_error("%s('%s') failed -- bad port", fname, url);
            memcpy(host, p, (size_t)(colon - p));
            host[colon - p] = '\0';
            memcpy(portbuf, colon + 1, plen);
            portbuf[plen] = '\0';
            port = atoi(portbuf);
            if (port <= 0 || port > 65535)
                k_error("%s('%s') failed -- bad port", fname, url);
        } else {
            if (hostlen >= sizeof(host))
                k_error("%s('%s') failed -- host too long", fname, url);
            memcpy(host, p, hostlen);
            host[hostlen] = '\0';
        }
    }

    memset(&hints, 0, sizeof(hints));
    hints.ai_family = AF_UNSPEC;
    hints.ai_socktype = SOCK_STREAM;
    if (getaddrinfo(host, NULL, &hints, &res) != 0 || !res) {
        if (res)
            freeaddrinfo(res);
        k_error("%s('%s') failed -- cannot resolve host '%s'", fname,
                url, host);
    }
    {
        struct addrinfo *ai;
        for (ai = res; ai; ai = ai->ai_next) {
            struct sockaddr_in *sin;
            struct sockaddr_in6 *sin6;
            if (ai->ai_family == AF_INET) {
                sin = (struct sockaddr_in *)ai->ai_addr;
                sin->sin_port = htons((uint16_t)port);
            } else if (ai->ai_family == AF_INET6) {
                sin6 = (struct sockaddr_in6 *)ai->ai_addr;
                sin6->sin6_port = htons((uint16_t)port);
            } else {
                continue;
            }
            fd = socket(ai->ai_family, ai->ai_socktype, ai->ai_protocol);
            if (fd < 0)
                continue;
            set_timeout(fd);
            if (connect(fd, ai->ai_addr, ai->ai_addrlen) == 0)
                break;
            close(fd);
            fd = -1;
        }
    }
    freeaddrinfo(res);
    if (fd < 0)
        k_error("%s('%s') failed -- cannot connect to '%s'", fname, url,
                host);

    if (strcmp(method, "POST") == 0) {
        snprintf(request, sizeof(request),
                 "POST %s HTTP/1.0\r\nHost: %s\r\n"
                 "Content-Type: text/plain; charset=utf-8\r\n"
                 "Content-Length: %zu\r\nConnection: close\r\n\r\n",
                 path, host, strlen(body));
    } else {
        snprintf(request, sizeof(request),
                 "GET %s HTTP/1.0\r\nHost: %s\r\n"
                 "Connection: close\r\n\r\n", path, host);
    }
    if (send(fd, request, strlen(request), 0) < 0) {
        close(fd);
        k_error("%s('%s') failed -- %s", fname, url, strerror(errno));
    }
    if (strcmp(method, "POST") == 0
            && send(fd, body, strlen(body), 0) < 0) {
        close(fd);
        k_error("%s('%s') failed -- %s", fname, url, strerror(errno));
    }
    for (;;) {
        char chunk[4096];
        ssize_t got = recv(fd, chunk, sizeof(chunk), 0);
        if (got < 0) {
            if (errno == EAGAIN || errno == EWOULDBLOCK) {
                close(fd);
                free(resp);
                k_error("%s('%s') failed -- timed out", fname, url);
            }
            close(fd);
            free(resp);
            k_error("%s('%s') failed -- %s", fname, url,
                    strerror(errno));
        }
        if (got == 0)
            break;
        if (resp_len + (size_t)got + 1 > resp_cap) {
            resp_cap = (resp_len + (size_t)got + 1) * 2;
            resp = (char *)realloc(resp, resp_cap);
            if (!resp)
                k_error("out of memory");
        }
        memcpy(resp + resp_len, chunk, (size_t)got);
        resp_len += (size_t)got;
    }
    close(fd);
    if (!resp) {
        k_error("%s('%s') failed -- empty response", fname, url);
    }
    resp[resp_len] = '\0';

    if (sscanf(resp, "HTTP/%*s %d", &status) != 1)
        k_error("%s('%s') failed -- bad HTTP response", fname, url);
    if (status != 200) {
        free(resp);
        k_error("%s('%s') failed -- the server returned HTTP %d", fname,
                url, status);
    }
    hdr_end = strstr(resp, "\r\n\r\n");
    if (!hdr_end) {
        free(resp);
        k_error("%s('%s') failed -- bad HTTP response", fname, url);
    }
    body_start = hdr_end + 4;
    {
        size_t body_len = resp_len - (size_t)(body_start - resp);
        char *result;
        /* Chunked transfer-encoding: decode the chunks. */
        if (strstr(resp, "Transfer-Encoding: chunked") != NULL
                || strstr(resp, "transfer-encoding: chunked") != NULL) {
            char *decoded = (char *)malloc(body_len + 1);
            size_t dlen = 0;
            char *cur = body_start;
            if (!decoded)
                k_error("out of memory");
            for (;;) {
                long chunk_len = strtol(cur, &cur, 16);
                if (chunk_len < 0) {
                    free(decoded);
                    free(resp);
                    k_error("%s('%s') failed -- bad chunked response",
                            fname, url);
                }
                if (chunk_len == 0)
                    break;
                cur += 2; /* CRLF after the size line */
                if ((size_t)(cur - resp) + (size_t)chunk_len > resp_len) {
                    free(decoded);
                    free(resp);
                    k_error("%s('%s') failed -- bad chunked response",
                            fname, url);
                }
                memcpy(decoded + dlen, cur, (size_t)chunk_len);
                dlen += (size_t)chunk_len;
                cur += chunk_len + 2;
            }
            decoded[dlen] = '\0';
            result = k_strndup(decoded, dlen);
            free(decoded);
            free(resp);
            return result;
        }
        result = k_strndup(body_start, body_len);
        free(resp);
        return result;
    }
}

KVal k_b_http_get(KVal *args, int nargs) {
    need_args("http_get", nargs, 1);
    if (args[0].tag != K_STR)
        k_error("http_get() needs a string URL, not %s",
                k_type_name(args[0]));
    return k_str(k_http_request(args[0].u.str, "GET", ""));
}

KVal k_b_http_post(KVal *args, int nargs) {
    need_args("http_post", nargs, 2);
    if (args[0].tag != K_STR)
        k_error("http_post() needs a string URL, not %s",
                k_type_name(args[0]));
    if (args[1].tag != K_STR)
        k_error("http_post() needs a string body, not %s",
                k_type_name(args[1]));
    return k_str(k_http_request(args[0].u.str, "POST", args[1].u.str));
}

/* -- builtin table ---------------------------------------------------- */

typedef struct {
    const char *name;
    KFnPtr fn;
    int arity;
} BuiltinEntry;

static const BuiltinEntry BUILTIN_TABLE[] = {
    {"print", k_b_print, -1},
    {"input", k_b_input, -1},
    {"len", k_b_len, 1},
    {"str", k_b_str, 1},
    {"num", k_b_num, 1},
    {"type", k_b_type, 1},
    {"range", k_b_range, -1},
    {"push", k_b_push, 2},
    {"pop", k_b_pop, 1},
    {"sqrt", k_b_sqrt, 1},
    {"pow", k_b_pow, 2},
    {"abs", k_b_abs, 1},
    {"keys", k_b_keys, 1},
    {"int", k_b_int, 1},
    {"float", k_b_float, 1},
    {"randint", k_b_randint, 2},
    {"randrange", k_b_randrange, -1},
    {"choice", k_b_choice, 1},
    {"shuffle", k_b_shuffle, 1},
    {"seed", k_b_seed, 1},
    {"floor", k_b_floor, 1},
    {"ceil", k_b_ceil, 1},
    {"sin", k_b_sin, 1},
    {"cos", k_b_cos, 1},
    {"tan", k_b_tan, 1},
    {"log", k_b_log, 1},
    {"exp", k_b_exp, 1},
    {"read_file", k_b_read_file, 1},
    {"write_file", k_b_write_file, 2},
    {"file_exists", k_b_file_exists, 1},
    {"json_parse", k_b_json_parse, 1},
    {"json_stringify", k_b_json_stringify, 1},
    {"http_get", k_b_http_get, 1},
    {"http_post", k_b_http_post, 2},
    {"now", k_b_now, 0},
    {"sleep", k_b_sleep, 1},
    {NULL, NULL, 0},
};

KVal k_builtin(const char *name) {
    int i;
    for (i = 0; BUILTIN_TABLE[i].name; i++) {
        if (strcmp(BUILTIN_TABLE[i].name, name) == 0)
            return k_make_builtin(BUILTIN_TABLE[i].fn, name,
                                  BUILTIN_TABLE[i].arity);
    }
    k_error("Undefined variable '%s' -- it was never defined with "
            "'let'. Check the spelling", name);
}
