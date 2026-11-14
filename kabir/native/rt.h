/* Kabir native runtime (3.0.0 Wave 1) -- C99.
 *
 * Value model: KVal is a tagged struct (tag + union). All heap
 * objects (strings, arrays, dicts) are arena-allocated: every
 * allocation is tracked and freed in one pass by k_rt_cleanup() at
 * program exit. Memory model: native (arena) -- NOT refcounting.
 * See kabir/codegen.py for the exact supported subset.
 *
 * Semantics mirror kabir/semantics.py exactly (same error messages
 * where practical): arithmetic incl. string/array `+`, comparisons,
 * truthiness, equality, format_value/print.
 */
#ifndef KABIR_RT_H
#define KABIR_RT_H

#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
    K_NULL = 0,
    K_NUM,
    K_BOOL,
    K_STR,
    K_ARR,
    K_DICT,
    K_FUNC
} KTag;

typedef struct KVal KVal;
typedef struct KArray KArray;
typedef struct KDict KDict;
typedef struct KFunc KFunc;

typedef KVal (*KFnPtr)(KVal *args, int nargs);

/* A captured variable lives in a heap cell (KCell); a slot (KSlot)
 * points at the cell so the cell can be (re)created when the
 * defining `let` / parameter / loop variable executes, while every
 * closure that captured the slot keeps seeing the current cell. */
typedef KVal *KCell;
typedef KVal **KSlot;

struct KArray {
    KVal *items;
    int len;
    int cap;
};

struct KDict {
    char **keys;   /* arena strings, insertion order */
    KVal *vals;
    int len;
    int cap;
};

struct KFunc {
    KFnPtr fn;         /* C function implementing it */
    const char *name;  /* Kabir name, e.g. "fib", "print", "upper" */
    int arity;         /* -1 = variadic (print only) */
    int is_builtin;    /* 1 for builtins / string methods */
    int has_recv;      /* 1 for a bound string method */
    char *recv_str;    /* bound receiver string when has_recv */
    /* Captured-variable environment (7.0.0 closures): an array of
     * slots, one per free variable, in the order the transpiler
     * lists them. A slot (KSlot = KVal **) points at a heap cell
     * (KCell = KVal *) that holds the variable's current value, so
     * a captured variable stays shared -- and mutable -- between
     * the defining function and every closure that captured it,
     * even after the defining function has returned. All slots and
     * cells are arena-allocated and freed at exit with the rest.
     * Plain (non-closure) functions have env == NULL, nenv == 0. */
    KSlot *env;
    int nenv;
};

struct KVal {
    KTag tag;
    union {
        double num;
        int b;
        char *str;     /* arena-owned, NUL-terminated, UTF-8 bytes */
        KArray *arr;
        KDict *dict;
        KFunc *fn;
    } u;
};

/* -- lifecycle ------------------------------------------------------ */
void k_rt_init(void);      /* seed RNG, reset arena */
void k_rt_cleanup(void);   /* free every arena allocation */

/* -- errors ---------------------------------------------------------- */
/* Prints "Kabir runtime error: <msg>" to stderr and exits(1). */
void k_error(const char *fmt, ...) __attribute__((noreturn));

/* -- arena allocation ------------------------------------------------ */
void *k_alloc(size_t n);            /* tracked, zeroed */
char *k_strdup(const char *s);      /* arena copy */
char *k_strndup(const char *s, size_t n);  /* arena copy */
/* Concatenate n NUL-terminated strings into one arena string. */
char *k_strcat_n(int n, const char **parts);

/* -- constructors ---------------------------------------------------- */
KVal k_null(void);
KVal k_num(double v);
KVal k_bool(int v);
KVal k_str(const char *s);          /* copies s into the arena */
KVal k_strn(const char *s, size_t n);
KVal k_new_array(void);
KVal k_new_dict(void);
KVal k_make_func(KFnPtr fn, const char *name, int arity);
KVal k_make_closure(KFnPtr fn, const char *name, int arity,
                    KSlot *env, int nenv);
KVal k_make_builtin(KFnPtr fn, const char *name, int arity);
KVal k_make_bound_method(const char *name, KVal recv);
/* Captured-variable slots/cells (7.0.0 closures, arena-allocated). */
KSlot k_new_slot(void);               /* slot with no cell yet */
KCell k_new_cell(KVal v);             /* cell holding v */
KVal k_slot_get(KSlot slot, const char *name);  /* error if no cell */
void k_slot_set(KSlot slot, const char *name, KVal v); /* error if none */
KVal k_env_get(int i, const char *name);        /* i-th capture */
void k_env_set(int i, const char *name, KVal v);
KSlot k_env_slot(int i);              /* i-th capture's slot itself */
/* Array / string helpers used by generated code. */
KVal k_array_of(int n, KVal *items);          /* copies items */
KVal k_build_string(int n, const char **parts);
KVal k_iter_items(KVal v);                     /* snapshot for `for` */
int k_array_len(KVal v);
KVal k_array_get(KVal v, int i);

/* -- values ---------------------------------------------------------- */
int k_is_truthy(KVal v);
const char *k_type_name(KVal v);    /* "null"/"boolean"/"number"/... */
char *k_format(KVal v);             /* format_value, arena string */
char *k_format_num(double v);       /* shortest round-trip repr */
int k_is_whole(KVal v);             /* number with an integer value */
int k_whole_int(KVal v, const char *fname);  /* whole value or error */

/* -- operators (mirror semantics.binary_op / kabir_equal) ------------ */
KVal k_add(KVal a, KVal b);
KVal k_sub(KVal a, KVal b);
KVal k_mul(KVal a, KVal b);
KVal k_div(KVal a, KVal b);
KVal k_mod(KVal a, KVal b);
KVal k_pow(KVal a, KVal b);
KVal k_neg(KVal a);
KVal k_not(KVal a);
int k_eq(KVal a, KVal b);           /* kabir_equal */
KVal k_cmp(const char *op, KVal a, KVal b);  /* < <= > >= -> bool KVal */
KVal k_len_val(KVal v);             /* len() as a value */
KVal k_get_index(KVal target, KVal index);
void k_set_index(KVal target, KVal index, KVal value);
KVal k_call(KVal callee, KVal *args, int nargs);
KVal k_method(KVal recv, const char *name);  /* bound method or error */
KVal k_builtin(const char *name);   /* builtin function value */

/* -- builtins: each is KVal (*)(KVal *args, int nargs) --------------- */
KVal k_b_print(KVal *args, int nargs);
KVal k_b_input(KVal *args, int nargs);
KVal k_b_len(KVal *args, int nargs);
KVal k_b_str(KVal *args, int nargs);
KVal k_b_num(KVal *args, int nargs);
KVal k_b_type(KVal *args, int nargs);
KVal k_b_range(KVal *args, int nargs);
KVal k_b_push(KVal *args, int nargs);
KVal k_b_pop(KVal *args, int nargs);
KVal k_b_sqrt(KVal *args, int nargs);
KVal k_b_pow(KVal *args, int nargs);
KVal k_b_abs(KVal *args, int nargs);
KVal k_b_keys(KVal *args, int nargs);
KVal k_b_int(KVal *args, int nargs);
KVal k_b_float(KVal *args, int nargs);
/* stdlib (kabir/stdlib.py) */
KVal k_b_randint(KVal *args, int nargs);
KVal k_b_randrange(KVal *args, int nargs);
KVal k_b_choice(KVal *args, int nargs);
KVal k_b_shuffle(KVal *args, int nargs);
KVal k_b_seed(KVal *args, int nargs);
KVal k_b_floor(KVal *args, int nargs);
KVal k_b_ceil(KVal *args, int nargs);
KVal k_b_sin(KVal *args, int nargs);
KVal k_b_cos(KVal *args, int nargs);
KVal k_b_tan(KVal *args, int nargs);
KVal k_b_log(KVal *args, int nargs);
KVal k_b_exp(KVal *args, int nargs);
KVal k_b_read_file(KVal *args, int nargs);
KVal k_b_write_file(KVal *args, int nargs);
KVal k_b_file_exists(KVal *args, int nargs);
KVal k_b_json_parse(KVal *args, int nargs);
KVal k_b_json_stringify(KVal *args, int nargs);
KVal k_b_http_get(KVal *args, int nargs);
KVal k_b_http_post(KVal *args, int nargs);
KVal k_b_now(KVal *args, int nargs);
KVal k_b_sleep(KVal *args, int nargs);

/* -- string methods (receiver is a string KVal) ---------------------- */
KVal k_str_method(const char *name, KVal recv, KVal *args, int nargs);

/* -- JSON ------------------------------------------------------------ */
KVal k_json_parse(const char *text);       /* error on bad JSON */
char *k_json_stringify(KVal v);            /* arena string */

/* -- HTTP (http:// only; https errors) ------------------------------- */
char *k_http_request(const char *url, const char *method,
                     const char *body);    /* response body, arena */

#ifdef __cplusplus
}
#endif

#endif /* KABIR_RT_H */
