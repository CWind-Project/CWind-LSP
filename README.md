# cwind-lsp

基于 [cwind_frontend](https://github.com/starwindv/cwind-lang/) 的 CWind 语言服务器 (LSP)。
复用前端的 Lexer / Parser / SA 结果, 为编辑器提供接近编译器的语义能力。

## 功能

| 能力       | 说明                                                                                                                                                                          |
|------------|-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| 细粒度高亮 | `textDocument/semanticTokens/full`: 词法兜底 + SA 语义角色 (函数/结构体/字段/变体/参数/类型参数/声明修饰等)                                                                   |
| 跳转       | 定义跳转 (`definition`)、引用查询 (`references`): 跨模块/标准库、模块名 (`mod::` 段与 `use` 路径)、`macro_rules!`/过程宏定义、结构体/枚举及其变体; trait 声明 ↔ impl 方法统一 |
| 智能补全   | 基于推断类型的成员补全 (`x.`)、模块路径补全 (`mod::`)、作用域变量/顶层符号/关键字/snippet; 类型位优先类型、值位优先函数/宏, 同前缀区分大小写; 类型未知时用占位符探测分析 |
| 悬停       | 函数签名、声明文本、推断类型 (Markdown)                                                                                                                                       |
| 诊断       | Lex / Parse / SA 错误与警告, 按来源文件归属, 按编辑防抖发布                                                                                                                   |
| 重命名     | 作用域感知 + 跨文件 `WorkspaceEdit`, 含 `prepareRename`; 标准库符号只读                                                                                                       |
| 编辑辅助   | 未保存缓冲区参与分析 (import 也能看到未落盘的修改)、增量同步、丢失焦点自动分析                                                                                                |

## 安装与运行

要求 Python >= 3.13; 与分析目标处于同一环境即可直接 `import cwind_frontend`

```shell
poetry install            # 或者 pip install -e .
cwind-lsp --stdio         # 也可 --tcp host:port / --ws host:port
```

常用参数: 

```
--std-path DIR    指定拥有 libs/ 的 CWind 安装根 (等价于 CWIND_HOME)
--log-file FILE   日志落盘 (默认 stderr)
--log-level        DEBUG / INFO / WARNING / ERROR
```

## 编辑器配置

现成客户端位于 [`editors/`](editors): 

| 编辑器                                       | 支持方式                                                         | 说明                                                         |
|----------------------------------------------|------------------------------------------------------------------|--------------------------------------------------------------|
| VS Code / Cursor 等                          | [`editors/vscode`](editors/vscode) 扩展 (含语法文件与全部设置项) | `npm install && npm run compile`, `F5` 调试或打包 VSIX       |
| JetBrains 全家桶 (IDEA/CLion/RustRover...)   | [`editors/jetbrains`](editors/jetbrains) (LSP4IJ 接入步骤)       | 插件市场装 LSP4IJ, 添加 `cwind-lsp --stdio` 与 `*.wind` 映射 |
| Neovim                                       | 内置 `vim.lsp.start`                                             | 见下                                                         |

### Neovim (内置 lspconfig 风格)

```lua
vim.lsp.start({
  name = "cwind-lsp",
  cmd = { "cwind-lsp", "--stdio" },
  root_dir = vim.fs.dirname(vim.fs.find({ "Breeze.toml", "libs" }, { upward = true })[1]),
  init_options = {
    cwind = {
      -- std_path = "path/to/lsp",
      -- target_os = "linux",
    },
  },
})
```

### 项目级配置 `cwind-lsp.toml`

在仓库根目录放置一份配置, 所有编辑器共用 (支持 target 三件套 /
`no_std` 等编译配置; `std_path`、`debounce_ms` 等编辑器专属项仍走
`initializationOptions`。对 target 类键的优先级: `cwind-lsp.toml` >
`initializationOptions` > 默认值): 

```toml
# cwind-lsp.toml
target_os = "windows"
target_arch = "x86_64"
target_pointer_width = "64"
no_std = false
```

## 初始化选项

```jsonc
{
  "cwind": {
    "std_path": "path/to/cwind/libs",  // libs/ 所在安装根
    "target_os": null,                // 固定 #[cfg] 目标平台, 默认宿主机
    "target_arch": null,
    "target_vendor": null,
    "target_pointer_width": null,
    "no_std": false,
    "debounce_ms": 350,               // 诊断防抖
    "diagnostics": true,
    "semantic_tokens": true,
    "completion": true,
    "hover": true,
    "max_problems": 500,
    "entry_overrides": { "path/to/file.wind": "path/to/entry.wind" }
  }
}
```

## 设计

- **快照 (Snapshot)**: 一次 `parse_with_errors + run_sa_with_errors` 的结果 +
  每文件的 `FileIndex`; 内容指纹命中时所有请求零成本复用。
- **请求并发**: pygls 线程池并发处理请求; 前端缓存是进程全局的, 因此真正
  的分析段用全局锁串行, 但同一个 entry 的并发请求会合并 (in-flight
  coalescing), 已缓存 entry 的请求完全不碰锁。
- **last-good 回退**: 编辑到一半 (语法错误) 时, 补全使用最近一次干净快照, 
  避免 "打点即空" 的体验; 同时后台防抖任务会持续分析最新内容。
- **内建声明面**: SA 会剪除不可达 std 声明, 导致 `Vector` 等方法丢失; 
  LSP 以纯语法方式解析 `libs/builtins`、`libs/expansion`、`libs/traits`
  (按指纹缓存), 补齐内置类型的成员与跳转。
- **未保存缓冲区**: `Path.read_text/read_bytes` 走进程内 overlay, import
  解析会读到编辑器里未落盘的内容; `Path.resolve` 做了记忆化 (Windows 上
  收益显著)。

## 已知限制

- 每次编辑后需要重跑整程序分析 (前端目前没有细粒度增量), 典型项目
  1~3 秒; 已用防抖、缓存与 last-good 回退掩盖。
- 项目入口遵循 `Breeze.toml` (`[entry].source/module`), 无 manifest 时以
  打开的文件为入口; `entry_overrides` 可手动指定。
- 标准库代码默认只读 (诊断会显示, 重命名/编辑被拒绝)。
- 导入项的 `use` 行重命名依赖名称唯一性; 同名歧义时不会跨文件改。

## 测试

```shell
python -m pytest tests -n 8
```

单测覆盖索引/分析/补全, `tests/test_protocol.py` 通过 `pytest-lsp` 起真实
stdio 服务器跑端到端协议流程。

