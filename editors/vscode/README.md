# Kabir for VS Code

Syntax highlighting and language-server support for Kabir (`.kbr` files):
diagnostics, completion, hover, go to definition, and formatting — all
provided by `kabir lsp` (see `kabir/lsp.py` in the Kabir repo).

Publishing to the VS Code Marketplace is out of scope for this
extension. Install it locally instead.

## Prerequisites

- VS Code 1.85 or newer.
- Node.js (for `npm install`).
- Kabir installed so that the `kabir` command is on your PATH, e.g.
  from the Kabir repo root:

  ```sh
  pip install -e .
  kabir --version
  ```

  If `kabir` is not on your PATH, set the absolute path in VS Code
  settings as `kabir.serverCommand`.

## Install locally

```sh
cd editors/vscode
npm install
```

Then, in VS Code:

1. Open this folder (`editors/vscode`) in VS Code.
2. Press `F5` (Run Extension / Extension Development Host). A new VS
   Code window opens with the Kabir extension active.
3. Open any `.kbr` file in that window. Syntax highlighting, error
   squiggles, completion, hover, go to definition, and formatting
   (Shift+Alt+F) come from the language server.

To install it permanently in your own VS Code instead of the
development host, package it with `vsce` (optional, not required for
local use):

```sh
npm install -g @vscode/vsce
vsce package
code --install-extension kabir-0.1.0.vsix
```

## Files

- `package.json` — extension manifest: language id `kabir`, `.kbr`
  extension, grammar contribution, `kabir.serverCommand` setting.
- `extension.js` — minimal client: launches `kabir lsp` over stdio.
- `syntaxes/kabir.tmLanguage.json` — TextMate grammar: keywords,
  strings (with `{...}` interpolation), comments, numbers, functions.
- `language-configuration.json` — `#` line comments, brackets,
  auto-closing pairs.

## Troubleshooting

- **No highlighting / no server:** check `kabir lsp` starts from a
  terminal (`echo '{}' | kabir lsp` should wait for input, not exit
  with an error) and that `kabir.serverCommand` points at it.
- **Server output:** use the VS Code command "Output: Show Output
  Channels" and pick "Kabir Language Server".
