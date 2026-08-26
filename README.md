# 📚 EPUB Translator

一个 EPUB 电子书翻译工具，基于 `pydantic-ai` 和大语言模型开发。

## ✨ 特性

- 🤖 **AI 翻译** - 基于 pydantic-ai，块级翻译是"一块原文进、一块译文出"的纯请求，Python 全程编排
- 🧩 **自动分块** - 递归切分 HTML，能下钻单根元素包裹的整章内容，避免输出被截断
- 📏 **章节长度无上限** - 每块一次独立请求，单次请求的输入量与块序号无关，几十块的长章节也不会撑爆上下文
- 💾 **断点续传** - 章节级 + **块级**缓存，翻译中断后只补没译完的块
- 🏷️ **标签完整性校验** - 用 HTML 标签数（而非字符数）检测漏译，中英文都不误报
- 🔤 **术语一致** - 自动维护专有名词术语表，跨块传递已用译名
- 📑 **目录翻译** - 同时翻译 `toc.ncx` 和 `nav.xhtml`，阅读器侧边栏目录也是译文
- 📝 **诊断日志** - 分块尺寸、token 用量、每块每次尝试的结果、失败原因全部落盘
- 🖼️ **图片翻译** - 可选翻译图片中的文字（需配置支持 Vision 的模型）

## 📦 安装

### 使用 uv（推荐）

```bash
# 克隆仓库
git clone https://github.com/leeyorke/auto-epub.git
cd auto-epub

# 安装依赖
uv sync

# 或使用 pip（requirements.txt 是完整的锁定版本导出）
pip install -r requirements.txt
```

### 主要依赖

```
pydantic-ai      # Agent 框架
ebooklib         # EPUB 解析与写入
beautifulsoup4   # HTML/XHTML 操作
lxml             # xml 解析器（处理导航文档必需）
tiktoken         # token 计数
typer            # CLI
python-dotenv    # .env 加载
```

## 🔧 配置

1. 复制环境变量模板：

```bash
cp .env.example .env
```

2. 编辑 `.env` 文件，填入你的 API 配置（任意兼容 OpenAI 格式的供应商均可）：

```env
API_BASE_URL=your-api-base-url
API_KEY=sk-your-api-key-here
API_MODEL=model-name
```

## 🚀 使用方法

### 基础翻译

```bash
# 翻译为中文
python main.py translate book.epub -l zh

# 翻译为英文
python main.py translate book.epub -l en

# 翻译为日文
python main.py translate book.epub -l ja
```

输出文件与源文件同目录，命名为 `book(zh).epub`。

### 高级选项

```bash
# 翻译图片中的文字（需要支持 Vision 的模型）
python main.py translate book.epub -l zh --images

# 不翻译目录
python main.py translate book.epub -l zh --no-toc

# 忽略缓存，从头重新翻译
python main.py translate book.epub -l zh --no-resume
```

`--resume` 默认开启：重复运行同一条命令即可继续未完成的章节。

### 缓存管理

```bash
# 清除特定文件的缓存
python main.py clear book.epub -l zh

# 清空全部翻译缓存（不指定书籍路径）
python main.py clear
```

### 查看版本

```bash
python main.py version
```

## 📖 工作原理

### 翻译流程

Python 负责编排。**章节正文的翻译是"一块原文进、一块译文出"的纯函数调用**——每个分块一次独立的模型请求，模型没有任何工具可调，所以单次请求的输入量与块序号无关，多长的章节都不会撑爆上下文。

```
1. 读取 EPUB，检测源语言，初始化诊断日志
   ↓
2. 加载缓存进度，把已完成章节的译文回填进 book
   ↓
3. 逐章循环（Python 控制）：
   ├─ Python 侧切分章节 HTML（按 INPUT_MAX_TOKENS），每章只切一次
   └─ 逐块循环（Python 控制）：
      ├─ 查块缓存，命中则完全不发请求
      ├─ 整块没有可译文本（例如只有 </section>）→ 原样透传
      ├─ 拼提示词：块原文 + 接力包（上一块接缝 / 本块术语 / 全章风格锚点）
      ├─ 一次无工具请求 → 模型的纯文本输出就是译文
      ├─ 清理输出（剥围栏与前言、摘掉末尾术语块）
      └─ 校验该块（块级标签比例、是否被截断），不合格就重译该块
   ↓
4. 全章的块都齐了 → 拼接写回 book + 落盘缓存（不完整则本章不算完成）
   ↓
5. 翻译目录（可选）：translate_toc → save_translated_toc
   同时写回 toc.ncx 与 nav.xhtml（侧边栏目录）
   ↓
6. 翻译图片（可选）
   ↓
7. Python 收尾写盘，并如实报告完成/失败章节数
```

### 两个 Agent

| Agent | 有无工具 | 负责 |
|-------|---------|------|
| `chunk_agent` | **无** | 章节正文：一块原文 → 一块译文 |
| `epub_agent` | 有 `epub_toolset` | 目录与图片，这两个阶段本来就是独立 run |

工具型 Agent 用 pydantic-ai 的 `FunctionToolset`，共享状态集中在 `EpubContext` 中作为 `deps` 传入。主要工具：`get_book_info`、`list_chapters`、`get_glossary`、`update_glossary`、`get_translation_progress`、`translate_toc`、`save_translated_toc`、`list_images`、`get_image_base64`、`save_translated_image`。

**章节级工具已全部下线**，取块、存块、保存整章都回到了 Python 侧。`finalize_chapter` 和 `finalize_epub` 都是普通函数而非工具：写盘时机由 Python 在校验完成度后决定，避免模型漏块漏章时提前落盘。

块与块之间没有对话历史，上下文靠**接力包**传递：上一块的原文尾与译文尾（衔接未完的句子）、命中本块的专有名词译名、本章开头的译法（风格锚点），总量有硬上限。译名由模型在每块译文末尾附一个 HTML 注释形式的术语块给出，键必须原样出现在本块原文里才会被收下。

### 分块与完整性校验

- 分块器递归下钻：整章被单个 `<section>` 包裹时也能切开，且所有分块拼接后与原 body 内容完全一致
- 分块可能是"半截"片段（开标签和闭标签会落进不同的块），所以校验器**刻意不做标签配平检查**，提示词也明说"原文没闭合的标签你也不要闭合"
- `INPUT_MAX_TOKENS` 必须显著小于 `OUTPUT_MAX_TOKENS`：译文 + HTML 标签叠加后，输出通常是输入的 1.3~2 倍
- 每块译完就地校验：**块级**标签（`p`/`div`/`h*`/`li`…）低于 `MIN_BLOCK_TAG_RATIO` 判定漏译，该块重译（每块最多 3 次尝试）；**内联**标签（`a`/`em`/`span`…）低于 `MIN_INLINE_TAG_RATIO` 只告警——模型会系统性吞掉脚注和强调标签而正文一字不缺，按全标签口径算会把译完的内容误杀
- 只有通过校验的块才写进块缓存，因此重试和 `--resume` 天然只重跑坏块；全章有块没过则本章不算完成，下次 `--resume` 继续补

### 缓存机制

缓存在 `~/.auto-epub/cache/` 目录（旧版放在工作目录的 `.epub_translation_cache/`，首次运行会自动整体搬过去），缓存键为文件绝对路径 + 目标语言的 MD5：

```
~/.auto-epub/cache/
├── {cache_key}.json          # 翻译进度（含术语表、目录译文）
└── {cache_key}/
    ├── chapters/              # 已翻译章节
    ├── chunks/                # 已通过校验的分块（按块原文的哈希寻址）
    └── images/                # 已翻译图片
```

块缓存按**内容**寻址（`chunks/{章节 ID 哈希}/{块原文哈希}.html`），所以调整 `INPUT_MAX_TOKENS` 或更新原书导致切分变化时，哈希自然不匹配 → 旧缓存自动失效重译，不会出现译文错位。只有通过校验的块才会写进去。

### 术语表

自动维护专有名词一致性：

- 第一次出现：`于连·索雷尔(Julien Sorel)`
- 后续出现：`于连·索雷尔`

模型在每块译文末尾附一个 HTML 注释形式的术语块声明本块新出现的译名，Python 核对"原名是否原样出现在本块原文里"后才收下，再随进度落盘、供后续分块沿用。

## ⚙️ 配置选项

在 `auto_epub/settings.py` 中可调整：

```python
# API 设置
TIMEOUT = 60               # 单次 HTTP 请求超时（秒）
OUTPUT_MAX_TOKENS = 16384  # 单次输出最大 token
INPUT_MAX_TOKENS = 5000    # 单个待翻译分块的 token 上限（必须显著小于上一项）
TEMPERATURE = 0.1          # 温度（越低越稳定）
MAX_REQUESTS = 128         # 目录 / 图片 run 的最大 API 请求数
MAX_CHAPTER_RETRIES = 2    # 单章失败后的重试次数（只重跑没进缓存的坏块）
MAX_CHUNK_RETRIES = 2      # 单块失败后的重试次数（每块共 3 次尝试）
CARRYOVER_MAX_TOKENS = 2000  # 接力包的 token 硬上限
MIN_BLOCK_TAG_RATIO = 0.8    # 块级标签比例下限，低于此判定漏译（硬指标）
MIN_INLINE_TAG_RATIO = 0.8   # 内联标签比例下限，低于此只告警，不阻塞保存

# 功能开关
TRANSLATE_IMAGES = False  # 是否翻译图片
TRANSLATE_TOC = True      # 是否翻译目录
ENABLE_CACHE = True       # 是否启用缓存

# 应用数据目录（缓存与日志的根，默认在用户主目录下）
APP_DIR = Path.home() / ".auto-epub"
CACHE_DIR = APP_DIR / "cache"   # 断点续传缓存
LOG_DIR = APP_DIR / "logs"      # 诊断日志

# 诊断日志
LOG_TO_FILE = True
LOG_EXCERPT_CHARS = 400   # 日志中模型输出片段的最大长度
```

自定义翻译规则（两份提示词，改哪份取决于改什么）：

```python
# 章节正文（无工具，输出格式规则写在这里）
CHUNK_SYSTEM_PROMPT = """
你的自定义翻译规则...
"""

# 目录与图片阶段（带工具）
AGENT_SYSTEM_PROMPT = """..."""
```

## 📝 诊断日志

每次运行会在 `~/.auto-epub/logs/` 下生成一个日志文件（`{书名}_{时间戳}.log`），启动时控制台会打印其路径。日志包含：

- 每章每次尝试的分隔行，以及切分结果（分块数、最大分块与合计 tokens）
- **每块每次尝试一行**：chars、块级标签比、token 用量、结束原因，以及是否命中缓存 / 原样透传
- 每块被判不合格时的原始输出转储（`{日志名}_ch{章号}_try{章级尝试}_chunk{块号}_a{块级尝试}.html`），供人工比对
- 目录 / 图片阶段每次 run 的用量与结束原因
- 每次保存被拒的原因、判定不完整的章节和块号
- `DATA` 前缀的 JSON 行（`save_chapter` / `chapter_failed` / `finish`），便于脚本统计

控制台详细程度由 `-v` / `-q` 控制，文件日志始终记录完整信息。

## 📝 示例

### 翻译《红与黑》

```bash
python main.py translate "The Red and the Black.epub" -l zh
# 输出：The Red and the Black(zh).epub
```

### 断点续传

```bash
# 第一次运行（翻译了 50%）
python main.py translate large_book.epub -l zh
^C  # 用户中断

# 继续翻译（自动从 50% 继续）
python main.py translate large_book.epub -l zh
```

未完成的章节在输出文件里仍是原文，重跑同一条命令即可补齐。

## 🛠️ 开发

### 项目结构

- 模块职责与调用链：[docs/FILE_MAPPING.md](./docs/FILE_MAPPING.md)
- 设计取舍与不变量：[docs/ARCHITECTURE.md](./docs/ARCHITECTURE.md)
- 安装到跑通：[docs/QUICKSTART.md](./docs/QUICKSTART.md)

### 扩展

#### 添加新的工具

在 `auto_epub/agent_tools.py` 中添加：

```python
@epub_toolset.tool
def your_custom_tool(ctx: RunContext[EpubContext], param: str) -> str:
    """你的自定义工具"""
    # 实现你的功能
    return "结果"
```

#### 自定义 Agent 行为

修改 `auto_epub/settings.py` 中的 `AGENT_SYSTEM_PROMPT`：

```python
AGENT_SYSTEM_PROMPT = """
你的自定义 Agent 指令...
- 翻译风格：正式/口语
- 特殊处理：保留/翻译引用
...
"""
```

## 🐛 故障排除

排查任何问题的第一步都是看 `~/.auto-epub/logs/` 下的日志文件。

**Q: 报错「模型输出了文本形式的工具调用」？**

A: 通常是输出被 `max_tokens` 截断，导致工具调用参数的 JSON 不完整，模型退化成把 `<tool_call>` 当普通文本吐出来。查日志里该章的分块 tokens，调小 `INPUT_MAX_TOKENS` 或调大 `OUTPUT_MAX_TOKENS`。

**Q: 提示「保存被拒：标签数 X/Y，疑似漏译」？**

A: 模型省略了部分内容。工具会要求它补译，日志里的 `*_chN_rejected.html` 是被拒的译文，可以据此确认漏了哪一段。

**Q: 章节始终失败？**

A: 该章会被记入 `failed_chapters`，输出文件里保持原文，其余章节照常翻译。日志中的 `chapter_failed` 记录了失败原因，修完配置后重跑同一条命令即可只重译这些章。

**Q: API 超时？**

A: 增大 `TIMEOUT` 或减小 `INPUT_MAX_TOKENS`（`settings.py`）。

**Q: 侧边栏目录还是原文？**

A: 侧边栏来自 EPUB3 导航文档而非 `toc.ncx`，两者都会被翻译。若仍是原文，检查日志里「导航文档 ... 同步 N 条目录译文」这一行；同步为 0 说明导航文档的标题文本与 `book.toc` 对不上。

**Q: 术语不一致？**

A: 术语表随进度缓存并注入每章提示词（最多 40 条）。可在 `AGENT_SYSTEM_PROMPT` 中强调术语规则。

**Q: 图片翻译失败？**

A: 确保使用支持 Vision 的模型，并加上 `--images`。

## ⚠️ 已知限制

- 保存章节时写入的是 body 级分块的拼接结果，`<html>` / `<head>` 外壳及其中的 CSS 链接会丢失，译后书籍的排版样式与原书不同。
- 翻译流程是串行的，`concurrent_manager.py` 提供的并发能力尚未接入主流程。
- 项目暂无单元测试。

## 📄 许可证

MIT License

## 🙏 致谢

- [pydantic-ai](https://github.com/pydantic/pydantic-ai) - Agent 框架
- [ebooklib](https://github.com/aerkalov/ebooklib) - EPUB 处理

## 🤝 贡献

欢迎提交 Issue 和 Pull Request！

## 📮 联系

有问题或建议？提交到：[issues](https://github.com/leeyorke/auto-epub/issues)
