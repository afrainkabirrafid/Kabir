"use strict";

// Minimal VS Code client for the Kabir language server.
//
// The server is the `kabir lsp` command (kabir/lsp.py): LSP over stdio,
// JSON-RPC with Content-Length framing. This client just launches it;
// all language intelligence (diagnostics, completion, hover, go to
// definition, formatting) lives in the server.
//
// No build step: this file is plain JavaScript and is the extension's
// entry point (see package.json "main").

const vscode = require("vscode");
const { LanguageClient, TransportKind } = require("vscode-languageclient/node");

let client;

/**
 * @param {vscode.ExtensionContext} context
 */
function activate(context) {
    const config = vscode.workspace.getConfiguration("kabir");
    const serverCommand = config.get("serverCommand", "kabir");
    const serverOptions = {
        command: serverCommand,
        args: ["lsp"],
        transport: TransportKind.stdio,
    };
    const clientOptions = {
        documentSelector: [{ scheme: "file", language: "kabir" }],
    };
    client = new LanguageClient(
        "kabir",
        "Kabir Language Server",
        serverOptions,
        clientOptions
    );
    context.subscriptions.push(client.start());
}

function deactivate() {
    if (!client) {
        return undefined;
    }
    return client.stop();
}

module.exports = { activate, deactivate };
