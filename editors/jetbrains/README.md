# CWind on JetBrains IDEs

JetBrains 系列 (IntelliJ IDEA / PyCharm / CLion / RustRover / WebStorm ...)
通过 **LSP4IJ** 插件接入 `cwind-lsp`. LSP4IJ 是 Red Hat 维护的通用 LSP
客户端, 支持大多数的 JetBrains IDE. 

> JetBrains 官方没有类似 `package.json` 的仓库级 LSP 配置文件, 服务器
> 定义保存在 IDE 设置里；项目相关参数建议写进仓库根目录的
> `cwind-lsp.toml` (见文末), 这样 VSCode 与 JetBrains 可以共用一份配置. 

## 1. 安装插件

`Settings` → `Plugins` → Marketplace 搜索 **LSP4IJ** → Install → 重启 IDE. 

## 2. 添加 CWind 语言服务器

`Settings` → `Languages & Frameworks` → `Language Servers` → `+` →
`New User-defined Language Server`, 填写：

| 字段 | 值 |
|------|-----|
| Name | `CWind` |
| Command | `cwind-lsp` (或虚拟环境可执行文件的绝对路径|
| Arguments | `--stdio` |

在同一个对话框的 **Mappings** 标签里添加文件映射：

| File name patterns | Language Id |
|--------------------|-------------|
| `*.wind` | `cwind` |
| `*.wd` | `cwind` |
| `*.cwind` | `cwind` |
| `*.cwd` | `cwind` |

如服务器无法自动定位安装根 (报 `std not found`), 把 **Command** 换成
带 `--std-path` 的包装, 或在 IDE 启动环境里设置 `CWIND_HOME`：

```text
cwind-lsp --stdio --std-path "path/to/cwind/libs/parent" 
```

## 3. 客户端初始化选项 (可选)

`Client configuration` → `Initialization options` 粘贴：

```json
{
  "cwind": {
    "std_path": "path/to/cwind/libs/parent",
    "target_os": "windows",
    "target_pointer_width": "64",
    "no_std": false,
    "debounce_ms": 350,
    "diagnostics": true,
    "semantic_tokens": true,
    "completion": true,
    "hover": true,
    "max_problems": 500
  }
}
```

不需要的键可以整行删除；`std_path` 与 `target_os` 留空也能工作 (按宿主机
自动检测). 

## 4. 项目级配置 (推荐)

在仓库根目录放一份 `cwind-lsp.toml`, JetBrains 与 VSCode 都会读取
(支持 `target_os` / `target_arch` / `target_vendor` /
`target_pointer_width` / `no_std`；`std_path`、`debounce_ms` 等属于
编辑器侧设置)：

```toml
# cwind-lsp.toml
target_os = "linux"
target_arch = "x86_64"
target_pointer_width = "64"
no_std = false
```

## 5. 验证

打开任意 `.wind` 文件：

- 悬停函数名应出现签名；
- 鼠标中键跳转定义；
- 输入 `x.` 触发成员补全；
- 改坏一行源码, 编辑器底部/编辑区应出现 `cwind` 来源的诊断. 

若没有反应, `Settings` → `Language Servers` → 选中 `CWind` →
`Server trace` 开 `verbose`, 再在 `LSP Console` 工具窗口查看握手与
`--stdio` 日志. 

