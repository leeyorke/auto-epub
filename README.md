# 📚 EPUB Translator

一个强大的 EPUB 电子书翻译工具，基于 `pydantic_ai` 和 大模型 开发。

## ✨ 特性

- 🤖 **AI 智能翻译** - 基于 pydantic-ai 和 大语言模型，Agent 自主决策翻译策略
- 🛠️ **工具集架构** - 使用 Toolsets 方式，Agent 可调用丰富的 EPUB 操作工具
- 💾 **断点续传** - 支持缓存，翻译中断后可继续
- 📖 **格式保留** - 完整保留 HTML 结构、CSS 样式
- 🔤 **术语一致** - 自动维护专有名词术语表
- 📑 **目录翻译** - 支持翻译 EPUB 目录
- 🖼️ **图片翻译** - 可选翻译图片中的文字（需配置image模型）

## 📦 安装

### 使用 uv（推荐）

```bash
# 克隆仓库
git clone https://github.com/leeyorke/auto-epub.git
cd epub-translator

# 安装依赖
uv sync

# 或使用 pip
pip install -r requirements.txt
```

### 依赖项

```
pydantic-ai
ebooklib
beautifulsoup4
tiktoken
typer
python-dotenv
```

## 🔧 配置

1. 复制环境变量模板：

```bash
cp .env.example .env
```

2. 编辑 `.env` 文件，填入你的 API 配置：

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

### 高级选项

```bash
# 翻译为中文
python main.py translate book.epub -l zh

# 翻译图片中的文字（需要 GPT-4V）
python main.py translate book.epub -l zh --images

# 不翻译目录
python main.py translate book.epub -l zh --no-toc

# 重新翻译（不使用缓存）
python main.py translate book.epub -l zh --no-resume
```

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

```
1. 读取 EPUB 文件
   ↓
2. 检测源语言
   ↓
3. 加载缓存（如果启用）
   ↓
4. Agent 接收翻译任务
   ↓
5. Agent 调用工具逐章翻译
   ├─ get_chapter_content - 读取章节
   ├─ 翻译 HTML 内容
   ├─ update_glossary - 更新术语表
   └─ save_translated_chapter - 保存结果
   ↓
6. 翻译目录（可选）
   ↓
7. 翻译图片（可选）
   ↓
8. finalize_epub - 保存 EPUB 文件
```

### Agent + Toolsets 架构

本项目使用 pydantic-ai 的 Toolsets 方式：

- **Agent**: 智能翻译助手，根据任务需求调用工具
- **Toolsets**: 提供 EPUB 操作的所有工具函数
  - 读取章节、图片
  - 保存翻译结果
  - 管理术语表
  - 更新目录
  - 保存文件

**优势**：
- Agent 可自主决策翻译策略
- 灵活应对各种 EPUB 结构
- 易于扩展新功能

### 缓存机制

缓存存储在 `.epub_translation_cache/` 目录：

```
.epub_translation_cache/
├── {cache_key}.json          # 翻译进度
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
# Token 限制
OUTPUT_MAX_TOKENS = 4096 # 输出最大 token
INPUT_MAX_TOKENS = 2000  # 输入最大 token

# 功能开关
TRANSLATE_IMAGES = False # 是否翻译图片
TRANSLATE_TOC = True     # 是否翻译目录
ENABLE_CACHE = True      # 是否启用缓存

# LLM 参数
TEMPERATURE = 0.1        # 温度（0-1，越低越稳定）
MAX_RETRIES = 3          # 失败重试次数
```

自定义 Agent 系统提示词：

```python
# 在 settings.py 中修改 AGENT_SYSTEM_PROMPT
AGENT_SYSTEM_PROMPT = """
你的自定义翻译规则...
"""
```

## 📝 示例

### 翻译《红与黑》

```bash
# 基础翻译
python main.py translate "The Red and the Black.epub" -l zh

# 输出：The Red and the Black(zh).epub
```

### 翻译图片

```bash
# 翻译文字和图片
python main.py translate manga.epub -l zh --images
```

### 断点续传

```bash
# 第一次运行（翻译了 50%）
python main.py translate large_book.epub -l zh
^C  # 用户中断

# 继续翻译（自动从 50% 继续）
python main.py translate large_book.epub -l zh --resume
```

## 🛠️ 开发

### 项目结构

见: [QUICKSTART](./docs/QUICKSTART.md)

### 扩展

#### 添加新的工具

在 `tools.py` 中添加：

```python
@epub_toolset.tool
def your_custom_tool(ctx: RunContext[EpubContext], param: str) -> str:
    """你的自定义工具"""
    # 实现你的功能
    return "结果"
```

#### 自定义 Agent 行为

修改 `settings.py` 中的 `AGENT_SYSTEM_PROMPT`：

```python
AGENT_SYSTEM_PROMPT = """
你的自定义 Agent 指令...
- 翻译风格：正式/口语
- 特殊处理：保留/翻译引用
...
"""
```

## 🐛 故障排除

### 常见问题

**Q: Agent 翻译速度慢？**

A: Agent 会逐章翻译以保证质量。调整 `OUTPUT_MAX_TOKENS` 可以让单次处理更多内容。

**Q: API 超时？**

A: 增加 `TIMEOUT` 设置（`settings.py`）或减少 `INPUT_MAX_TOKENS`

**Q: 术语不一致？**

A: 检查术语表缓存，Agent 会自动维护术语一致性。可以在 `AGENT_SYSTEM_PROMPT` 中强调术语规则。

**Q: 图片翻译失败？**

A: 确保使用支持 Vision 的模型（gpt-4o, gpt-4-turbo）

**Q: Agent 卡住了？**

A: 检查 Agent 日志，可能是工具调用失败。使用 `--no-resume` 重新开始。

### 日志调试

启用详细日志：

```python
# settings.py
DEBUG_MODE = True
```

## 📄 许可证

MIT License

## 🙏 致谢

- [pydantic-ai](https://github.com/pydantic/pydantic-ai) - Agent 框架
- [ebooklib](https://github.com/aerkalov/ebooklib) - EPUB 处理

## 🤝 贡献

欢迎提交 Issue 和 Pull Request！

## 📮 联系

有问题或建议？提交到：[issues](https://github.com/leeyorke/auto-epub/issues)