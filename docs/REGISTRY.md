# Kabir Registry — index format & hosting model (5.0.0)

The Kabir package registry is **static files in a GitHub repo**. No
server to run, no paid service, no account besides GitHub. `kabir add`
downloads from it; `kabir publish` never pushes anything — it builds
the package and stages the exact index update for the owner to push
by hand.

> **Status (5.0.0):** the code is complete and verified against local
> test registries. The live index repo does **not** exist yet —
> creating `afrainkabirrafid/kabir-registry` on GitHub and choosing
> the URL form (raw.githubusercontent.com vs GitHub Pages) needs the
> owner's approval. Until then `kabir add` without `KABIR_REGISTRY`
> set refuses with a clear error. See
> [Decision needed](#decision-needed) at the end.

## 1. The index repo layout

```
kabir-registry/                 (a plain GitHub repo)
├── index.json                  (the whole registry: every package,
│                                every version, one file)
└── packages/
    ├── greet-1.0.0.tar.gz
    ├── greet-1.1.0.tar.gz
    └── other-0.2.0.tar.gz
```

* `index.json` is the only file `kabir add` reads first. It lists
  every package and version with the tarball location and sha256.
* `packages/<name>-<version>.tar.gz` is the package itself: a
  deterministic `.tar.gz` built by `kabir publish` containing
  `Kabir.toml` and everything under `src/`.
* Nothing else is required. No CI, no build step, no server.

## 2. `index.json` schema (exact)

Top level: a JSON object with one required key.

```json
{
  "packages": {
    "<name>": {
      "<version>": {
        "url": "<string, required>",
        "sha256": "<string, optional>",
        "description": "<string, optional>",
        "dependencies": {"<name>": "<version>", "...": "..."}
      }
    }
  }
}
```

| Field | Required | Type | Meaning |
|---|---|---|---|
| `packages` | yes | object | Package name → versions map. |
| `<name>` | yes | string | Package name. Must start with a letter; only letters, digits, `-`, `_`. Same rule as project names in `Kabir.toml`. |
| `<version>` | yes | string | Exact version, `MAJOR.MINOR.PATCH` only, e.g. `"1.0.0"`. No ranges (`^`, `>=`, `*`, `latest` are never stored in the index). |
| `url` | yes | string | Where the tarball is. Either an **absolute** `http://`/`https://` URL, or a **path relative to the registry**: a file inside a local registry directory, or a path under the URL base of a remote registry (normally `"packages/<name>-<version>.tar.gz"`). |
| `sha256` | no | string | 64 lowercase hex chars: the SHA-256 of the tarball. When present, `kabir add` verifies it before installing and refuses on mismatch. When absent, the digest of the downloaded file is recorded in `Kabir.lock` instead. `kabir publish` always writes it. |
| `description` | no | string | Free text, copied from `description` in the package's `[package]` section in `Kabir.toml`. Shown to humans; never used for resolution. |
| `dependencies` | no | object | Transitive dependencies: package name → **exact** version. `kabir add` installs these recursively (vendored + locked, but not added to the consumer's `[dependencies]` — only the package asked for is a direct dependency). |

Rules enforced by `kabir.project.validate_index` (used by every
`add` and by `stage_publish` before it hands a staged index back):

* Unknown extra keys anywhere are **ignored** — the format can grow
  without breaking old readers.
* `url` must be a non-empty string; `sha256`, when present, must be
  64 lowercase hex chars; `description` must be a string;
  `dependencies` must map valid names to exact versions.
* A malformed entry fails the whole read with the package/version
  named in the error — a bad entry can never silently install.

Example (complete, minimal + full):

```json
{
  "packages": {
    "greet": {
      "1.0.0": {
        "url": "packages/greet-1.0.0.tar.gz",
        "sha256": "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
        "description": "Say hello in Kabir.",
        "dependencies": {"shout": "0.1.0"}
      },
      "1.1.0": {
        "url": "packages/greet-1.1.0.tar.gz",
        "sha256": "4d3b5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b"
      }
    }
  }
}
```

Backward compatibility: an index written by 3.0.0 (`url` + `sha256`
only) is valid under this schema. `description` and `dependencies`
are purely additive.

## 3. How `kabir add` resolves

```
kabir add greet@1.0.0        exact version
kabir add greet@latest       highest version in the index
```

1. Registry source: `KABIR_REGISTRY` env var, or the argument passed
   to `add_package`. A local directory path, or an `http(s)://` URL
   base. With nothing set, the default is
   `https://raw.githubusercontent.com/afrainkabirrafid/kabir-registry/main`
   — which does not exist yet, so `add` refuses with an error telling
   the user to set `KABIR_REGISTRY`.
2. Fetch `<base>/index.json` (local file read, or HTTP GET) and
   validate it (see §2).
3. Look up `packages[greet]["1.0.0"]`; unknown package / unknown
   version is an error that lists the available versions.
4. Fetch the tarball (`url` resolved against the registry base),
   verify `sha256` when the entry has one, refuse on mismatch.
5. Extract into `vendor/greet/1.0.0/` (tarball paths are checked:
   absolute paths and `..` are refused). Re-adding re-vendors cleanly.
6. Install transitive `dependencies` the same way (recursively).
   A diamond (same package, same version, twice) installs once; a
   conflict (same package, two different versions) is an error —
   exact versions only, the conflicting packages must agree.
7. Record `greet = "1.0.0"` in the project's `[dependencies]` in
   `Kabir.toml` (comments and other sections untouched) and write
   `Kabir.lock` (`{name: {version, sha256}}` for the direct package
   **and** every transitive one).

## 4. How `kabir publish` works (staging, never pushing)

```
kabir publish [dir] [--stage DIR]
```

1. Validate the project: `Kabir.toml` parses, `src/main.kbr`
   exists, version is an exact `MAJOR.MINOR.PATCH` (a non-exact
   version is refused — registry versions must be exact).
2. Build a deterministic `target/<name>-<version>.tar.gz`
   (`Kabir.toml` + everything under `src/`, fixed timestamps/uids,
   so rebuilding gives the same sha256). Compute its sha256.
3. Build the registry entry from the manifest: `url`
   (`packages/<name>-<version>.tar.gz`), `sha256`, plus
   `description` and `dependencies` when the manifest sets them.
4. Merge the entry into the registry index — the index at
   `KABIR_REGISTRY` when one is configured (local dir, or fetched
   over HTTP; a missing index means a fresh registry), otherwise
   a fresh empty index. **A version already in the index is
   refused**: released versions are never overwritten; bump the
   version and publish again.
5. Write the staged update into `<project>/target/registry-stage/`
   (or `--stage DIR`): `index.json` (the full merged index,
   sorted keys) + `packages/<name>-<version>.tar.gz`. The staged
   index is re-read and validated before the command returns.
6. Print the exact shell commands to publish the staged files
   (copy into the registry repo, `git add` / `commit` / `push`).
   Nothing is executed, no credentials are needed, nothing is
   pushed by Kabir.

`kabir.project.stage_publish()` is the same flow as a library call;
`publish_package()` is the build-only half (tarball + entry JSON).

## 5. Creating the index repo (steps for the owner)

```sh
# 1. Create the repo on GitHub (empty, no README needed):
#    https://github.com/new — owner afrainkabirrafid, name kabir-registry
#    Or: gh repo create afrainkabirrafid/kabir-registry --public

git clone https://github.com/afrainkabirrafid/kabir-registry.git
cd kabir-registry
mkdir -p packages
echo '{"packages": {}}' > index.json
git add index.json
git commit -m "Create empty Kabir registry index"
git push
```

After that, publishing a package is exactly what `kabir publish`
prints: copy the staged `index.json` + tarball in, commit, push.
Consumers then use it with no setup at all — the default
`KABIR_REGISTRY` already points at it:

```sh
kabir add greet@1.0.0
```

For a local test registry instead (development, or before the repo
exists), any directory with the §1 layout works:

```sh
export KABIR_REGISTRY=/path/to/my-registry
kabir add greet@1.0.0
```

## 6. URL form: raw.githubusercontent.com vs GitHub Pages

Both serve the same static files; the code works with either, since
the registry is just a URL base + `index.json` + tarballs.

| | raw.githubusercontent.com | GitHub Pages |
|---|---|---|
| URL base | `https://raw.githubusercontent.com/afrainkabirrafid/kabir-registry/main` | `https://afrainkabirrafid.github.io/kabir-registry` |
| Setup | Nothing — works the moment the repo exists. | Enable Pages on the repo (Settings → Pages → Deploy from branch `main`, folder `/ (root)`). |
| Caching | Raw URLs are CDN-cached; a pushed update can take minutes to show. | Same CDN behaviour; similar delay. |
| Branch in URL | The branch (`main`) is part of the URL. | No branch in the URL. |
| Extra files | None needed. | None needed (no Jekyll config required for plain files). |

**Recommendation: raw.githubusercontent.com.** It needs zero setup,
and it is already the default in `kabir/project.py`
(`DEFAULT_REGISTRY_URL`). GitHub Pages adds a settings step for no
benefit here — the registry has no HTML to serve. Either works;
switching later is a one-line change to `DEFAULT_REGISTRY_URL`.

## 7. Security notes

* **sha256 is the trust anchor.** The index is public and the
  tarballs are public; anyone who can push to the index repo can
  list any tarball. `add` refuses to install when the downloaded
  bytes do not match the index digest, so a tampered tarball (or a
  man-in-the-middle on plain `http://`) cannot install silently.
  Keep the registry repo's push access tight — it is the whole
  trust model, same as any package index.
* **Tarball paths are checked** before extraction: absolute paths
  and `..` components are refused, so a hostile tarball cannot
  write outside `vendor/`.
* **No credentials in Kabir.** Publishing is a manual `git push`
  by the owner; Kabir never stores or asks for a token.

## 8. Limits (honest)

* Exact versions only — no ranges, no `^1.0.0`. A version conflict
  between transitive dependencies is an error, not a resolution.
* No yank / delete: a published version stays in the index. (A
  future `kabir yank` would remove the entry and leave the tarball
  in place; not built in 5.0.0.)
* No signatures beyond sha256, no per-package owners/permissions.
* The registry is one `index.json` file: fine for a small ecosystem;
  if it ever grows to thousands of packages, the index should be
  split per package (e.g. `index/<name>.json`) — a format change
  for a later version, not 5.0.0.
* `kabir add` does not run any code from the package and does not
  wire it into `import` yet — vendored files land under
  `vendor/<name>/<version>/` for the project to use.

## 9. Decision needed

Before the live registry works, the owner must decide:

1. **Create the repo** `afrainkabirrafid/kabir-registry` on GitHub
   (public, empty) — §5 has the exact steps.
2. **URL form**: raw.githubusercontent.com (recommended, already
   the default — no action needed) or GitHub Pages (needs Pages
   enabled on the repo).

No code change is needed for either choice: `kabir add` and
`kabir publish` work with any `KABIR_REGISTRY` URL base or local
directory today.
