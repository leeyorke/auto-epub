# 📚 EPUB Translator

一个 EPUB 电子书翻译工具，基于 `pydantic-ai` 和大语言模型开发。

## ✨ 特性

- 🤖 **AI 翻译** - 基于 pydantic-ai，Agent 自主调度 EPUB 操作工具完成单章翻译
- 🧩 **自动分块** - 递归切分 HTML，能下钻单根元素包裹的整章内容，避免输出被截断
- 💾 **断点续传** - 章节级缓存，翻译中断后可继续
- 🏷️ **标签完整性校验** - 用 HTML 标签数（而非字符数）检测漏译，中英文都不误报
- 🔤 **术语一致** - 自动维护专有名词术语表
- 📑 **目录翻译** - 同时翻译 `toc.ncx` 和 `nav.xhtml`，阅读器侧边栏目录也是译文
- 📝 **诊断日志** - 分块尺寸、token 用量、工具调用序列、失败原因全部落盘
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
python main.py clear-cache book.epub -l zh
```

### 查看版本

```bash
python main.py version
```

## 📖 工作原理

### 翻译流程

Python 负责编排，Agent 只在"翻译一个章节"这一粒度上工作——每章一次独立的 Agent run，避免上下文无限累积。

```
1. 读取 EPUB，检测源语言，初始化诊断日志
   ↓
2. 加载缓存进度，把已完成章节的译文回填进 book
   ↓
3. 逐章循环（Python 控制）：
   ├─ Python 侧切分章节 HTML（按 INPUT_MAX_TOKENS）
   └─ Agent run：
      ├─ get_untranslated_content   - 取出一个分块
      ├─ 翻译该块（保留全部 HTML 标签）
      ├─ store_translation_chunk    - 写入译文，可多次调用
      ├─ update_glossary            - 记录新术语（可选）
      └─ save_translated_chapter    - 保存本章（含完整性校验）
   ↓
4. 翻译目录（可选）：translate_toc → save_translated_toc
   同时写回 toc.ncx 与 nav.xhtml（侧边栏目录）
   ↓
5. 翻译图片（可选）
   ↓
6. Python 收尾写盘，并如实报告完成/失败章节数
```

### Agent + Toolsets 架构

使用 pydantic-ai 的 `FunctionToolset` 把 EPUB 操作注册为 Agent 可调用的工具，共享状态集中在 `EpubContext` 中作为 `deps` 传入。

主要工具：`get_book_info`、`list_chapters`、`get_untranslated_content`、`store_translation_chunk`、`save_translated_chapter`、`is_untranslated_buffer_empty`、`get_glossary`、`update_glossary`、`get_translation_progress`、`translate_toc`、`save_translated_toc`、`list_images`、`get_image_base64`、`save_translated_image`。

`finalize_epub` **不是** Agent 工具，而是普通函数：写盘时机由 Python 在校验完成度后决定，避免模型漏章时提前落盘。

### 分块与完整性校验

- 分块器递归下钻：整章被单个 `<section>` 包裹时也能切开，且所有分块拼接后与原 body 内容完全一致
- `INPUT_MAX_TOKENS` 必须显著小于 `OUTPUT_MAX_TOKENS`：译文 + HTML 标签 + JSON 转义叠加后，输出通常是输入的 1.5~2 倍
- 保存前比对译文与原文的 HTML 标签数：**块级**标签（`p`/`div`/`h*`/`li`…）低于 `MIN_BLOCK_TAG_RATIO` 判定漏译，该块会被作废重发一次，全章不达标则本章不算完成、下次 `--resume` 重译；**内联**标签（`a`/`em`/`span`…）低于 `MIN_INLINE_TAG_RATIO` 只告警，不阻塞保存

### 缓存机制

缓存存储在 `.epub_translation_cache/` 目录，缓存键为文件绝对路径 + 目标语言的 MD5：

```
.epub_translation_cache/
├── {cache_key}.json          # 翻译进度（含术语表、目录译文）
└── {cache_key}/
    ├── chapters/              # 已翻译章节
    └── images/                # 已翻译图片
```

### 术语表

自动维护专有名词一致性：

- 第一次出现：`于连·索雷尔(Julien Sorel)`
- 后续出现：`于连·索雷尔`

## ⚙️ 配置选项

在 `auto_epub/settings.py` 中可调整：

```python
# API 设置
TIMEOUT = 60              # 请求超时（秒）
MAX_RETRIES = 10          # HTTP 层重试次数
OUTPUT_MAX_TOKENS = 8192  # 单次输出最大 token
INPUT_MAX_TOKENS = 2500   # 单个待翻译分块的 token 上限
TEMPERATURE = 0.1         # 温度（越低越稳定）
MAX_REQUESTS = 60         # 单章 Agent run 的最大 API 请求数
MAX_CHAPTER_RETRIES = 2   # 单章翻译失败后的重试次数
MIN_BLOCK_TAG_RATIO = 0.8   # 块级标签比例下限，低于此判定漏译（硬指标）
MIN_INLINE_TAG_RATIO = 0.8  # 内联标签比例下限，低于此只告警，不阻塞保存

# 功能开关
TRANSLATE_IMAGES = False  # 是否翻译图片
TRANSLATE_TOC = True      # 是否翻译目录
ENABLE_CACHE = True       # 是否启用缓存
DEBUG_MODE = True         # 控制台是否打印诊断细节

# 诊断日志
LOG_TO_FILE = True
LOG_DIR = ".epub_translation_logs"
LOG_EXCERPT_CHARS = 400   # 日志中模型输出片段的最大长度
```

自定义 Agent 系统提示词：

```python
# 在 settings.py 中修改 AGENT_SYSTEM_PROMPT
AGENT_SYSTEM_PROMPT = """
你的自定义翻译规则...
"""
```

## 📝 诊断日志

每次运行会在 `.epub_translation_logs/` 下生成一个日志文件（`{书名}_{时间戳}.log`），启动时控制台会打印其路径。日志包含：

- 每章每次尝试的分隔行，以及逐块的 `tokens / chars / tags`
- 每次 Agent run 的输出长度、输出片段、token 用量、实际发起的工具调用序列
- 模型把工具调用写成纯文本时的原始输出（用于判断是否为输出截断所致）
- 每次保存被拒的原因；标签数不达标时把被拒译文另存为 `*_chN_rejected.html` 供人工比对
- `DATA` 前缀的 JSON 行（`save_chapter` / `chapter_failed` / `finish`），便于脚本统计

`DEBUG_MODE` 只控制控制台摘要，文件始终记录完整信息。

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

排查任何问题的第一步都是看 `.epub_translation_logs/` 下的日志文件。

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
