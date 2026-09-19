import * as fs from "fs";
import * as path from "path";
import * as vscode from "vscode";
import {
  LanguageClient,
  LanguageClientOptions,
  ServerOptions,
} from "vscode-languageclient/node";

let client: LanguageClient | undefined;
let output: vscode.OutputChannel | undefined;

const SERVER_NAMES =
  process.platform === "win32"
    ? ["cwind-lsp.exe", "cwind-lsp.cmd", "cwind-lsp.bat", "cwind-lsp"]
    : ["cwind-lsp"];

function log(message: string): void {
  output?.appendLine(`[cwind] ${message}`);
}

function initializationOptions(): Record<string, unknown> {
  const config = vscode.workspace.getConfiguration("cwind");
  const optional = (key: string): string | undefined => {
    const value = config.get<string>(key, "").trim();
    return value.length > 0 ? value : undefined;
  };
  return {
    cwind: {
      std_path: optional("stdPath"),
      target_os: optional("targetOs"),
      target_arch: optional("targetArch"),
      target_vendor: optional("targetVendor"),
      target_pointer_width: optional("targetPointerWidth"),
      no_std: config.get<boolean>("noStd", false),
      debounce_ms: config.get<number>("debounceMs", 350),
      diagnostics: config.get<boolean>("diagnostics", true),
      semantic_tokens: config.get<boolean>("semanticTokens", true),
    },
  };
}

function venvCandidates(folder: vscode.WorkspaceFolder): string[] {
  const candidates: string[] = [];
  let directory = folder.uri.fsPath;
  for (let depth = 0; depth < 4; depth += 1) {
    for (const base of [".venv/Scripts", ".venv/bin", "venv/Scripts", "venv/bin"]) {
      for (const name of SERVER_NAMES) {
        candidates.push(path.join(directory, base, name));
      }
    }
    const parent = path.dirname(directory);
    if (parent === directory) {
      break;
    }
    directory = parent;
  }
  return candidates;
}

interface ServerCommand {
  command: string;
  args: string[];
}

function resolveServer(): ServerCommand {
  const config = vscode.workspace.getConfiguration("cwind");
  const explicit = config.get<string>("serverPath", "").trim();
  if (explicit) {
    return {
      command: explicit,
      args: config.get<string[]>("serverArgs", ["--stdio"]),
    };
  }
  const fromEnv = (process.env.CWIND_LSP_PATH ?? "").trim();
  if (fromEnv) {
    return { command: fromEnv, args: ["--stdio"] };
  }
  for (const folder of vscode.workspace.workspaceFolders ?? []) {
    for (const candidate of venvCandidates(folder)) {
      if (fs.existsSync(candidate)) {
        return { command: candidate, args: ["--stdio"] };
      }
    }
  }
  const python = config.get<string>("pythonPath", "").trim();
  if (python) {
    return { command: python, args: ["-m", "cwind_lsp", "--stdio"] };
  }
  return { command: "cwind-lsp", args: ["--stdio"] };
}

async function startClient(): Promise<void> {
  const { command, args } = resolveServer();
  log(`starting: ${command} ${args.join(" ")}`);
  const serverOptions: ServerOptions = { command, args };
  const clientOptions: LanguageClientOptions = {
    documentSelector: [{ scheme: "file", language: "cwind" }],
    synchronize: {
      fileEvents: vscode.workspace.createFileSystemWatcher(
        "**/*.{wind,wd,cwind,cwd}"
      ),
    },
    initializationOptions: initializationOptions(),
  };
  client = new LanguageClient(
    "cwind",
    "CWind Language Server",
    serverOptions,
    clientOptions
  );
  try {
    await client.start();
    log("server started");
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    log(`start failed: ${message}`);
    const action = await vscode.window.showErrorMessage(
      `CWind language server failed to start (${command}). ` +
        'Set "cwind.serverPath" to the cwind-lsp executable ' +
        "(e.g. <cwind>/.venv/Scripts/cwind-lsp.exe) or add it to PATH.",
      "Open Settings",
      "Show Output"
    );
    if (action === "Open Settings") {
      await vscode.commands.executeCommand(
        "workbench.action.openSettings",
        "cwind.serverPath"
      );
    } else if (action === "Show Output") {
      output?.show();
    }
    client = undefined;
  }
}

export async function activate(context: vscode.ExtensionContext) {
  output = vscode.window.createOutputChannel("CWind Language Server");
  context.subscriptions.push(output);
  context.subscriptions.push(
    vscode.commands.registerCommand("cwind.showOutput", () => output?.show()),
    vscode.commands.registerCommand("cwind.restartServer", async () => {
      if (client) {
        await client.stop();
        client = undefined;
      }
      await startClient();
    })
  );
  await startClient();
}

export async function deactivate(): Promise<void> {
  if (client) {
    await client.stop();
    client = undefined;
  }
}
