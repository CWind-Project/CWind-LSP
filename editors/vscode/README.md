# CWind for VS Code

`cwind-lsp` 的 VS Code 客户端扩展，提供 CWind (`.wind` / `.wd` / `.cwind` /
`.cwd`) 的语义高亮、跳转、补全、悬停、诊断与重命名。

## 构建

```shell
cd editors/vscode
npm install
npm run compile
```

## 本地安装

方式一：以扩展开发模式启动

1. 用 VS Code 打开 `editors/vscode` 目录；
2. `F5` 启动 Extension Development Host，打开任意 `.wind` 文件即可。

方式二：打包成 VSIX 安装

```shell
npm install -g @vscode/vsce
vsce package
code --install-extension cwind-lsp-0.0.1.vsix
```

## 配置

扩展会自动按以下顺序找服务器，通常**无需任何配置**：

1. `cwind.serverPath` 设置；
2. 环境变量 `CWIND_LSP_PATH`；
3. 工作区（及其上溯 3 层）里的 `.venv/Scripts/cwind-lsp.exe` /
   `.venv/bin/cwind-lsp`（也支持 `venv/`）；
4. `cwind.pythonPath` 指定的解释器，以 `python -m cwind_lsp` 启动；
5. `PATH` 上的 `cwind-lsp`。

都找不到时弹窗提示，并提供 "Open Settings" / "Show Output" 两个按钮；
也可以随时用命令面板的 `CWind: Show Language Server Output` 查看启动
日志和服务器 stderr。

手动配置示例 (`.vscode/settings.json`)：

```jsonc
{
  "cwind.serverPath": "path/to/cwind-lsp",
  "cwind.serverArgs": ["--stdio"],
  "cwind.stdPath": "path/to/cwind/libs/parent",  // 可选, 拥有 libs/ 的安装根
  "cwind.targetOs": "windows",           // 可选, 固定 #[cfg] 目标
  "cwind.pythonPath": "",                // 可选, cwind-lsp 找不到时的兜底
  "cwind.trace.server": "verbose"
}
```

> 装了扩展但"没反应"时，先看 `CWind: Show Language Server Output`：
> 若是 `spawn cwind-lsp ENOENT`，说明服务器不在 PATH 上，设
> `cwind.serverPath` 即可。

项目级设置也可以直接写在仓库根目录的 `cwind-lsp.toml` 里，编辑器两端共用：

```toml
# cwind-lsp.toml
target_os = "windows"
no_std = false
```

> `.wind` 首次打开时，若服务器报 `std not found`，通常是 `stdPath` 未配置
> 且安装根无法从 `cwind_frontend` 推导；显式设置 `cwind.stdPath` 或环境变量
> `CWIND_HOME` 即可。

## 功能覆盖

| LSP 能力                                 | 说明                             |
|------------------------------------------|----------------------------------|
| `textDocument/semanticTokens/full`       | 词法兜底 + SA 语义角色           |
| `textDocument/definition` / `references` | 跨模块跳转与引用                 |
| `textDocument/completion`                | 推断类型成员 / 模块路径 / 作用域 |
| `textDocument/hover`                     | 签名与推断类型                   |
| `textDocument/publishDiagnostics`        | Lex / Parse / SA 诊断            |
| `textDocument/prepareRename` / `rename`  | 作用域感知跨文件重命名           |

