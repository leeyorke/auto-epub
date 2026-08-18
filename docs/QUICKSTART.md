# 🚀 快速开始指南

## 1. 安装

```bash
# 克隆项目
git clone https://github.com/leeyorke/auto-epub.git
cd auto-epub

# 安装依赖（使用 uv）
uv sync

# 或使用 pip
pip install -r requirements.txt
```

## 2. 配置

创建 `.env` 文件：

```bash
cp .env.example .env
```

编辑 `.env`，填入你的模型供应商（任意兼容 OpenAI API 格式的供应商均可）：

```env
API_BASE_URL=your-api-base-url
API_KEY=sk-your-api-key-here
API_MODEL=model-name
```

## 3. 基础使用

### 方式一：命令行（推荐）

```bash
# 翻译为中文
python main.py translate book.epub -l zh

# 查看所有选项
python main.py translate --help
```

输出文件与源文件同目录，命名为 `book(zh).epub`。运行时控制台会打印本次的诊断日志路径。

### 方式二：Python 脚本

```python
import asyncio
from auto_epub import create_translator

async def main():
    translator = create_translator(
        target_language="zh",
        cache_enabled=True
    )

    output = await translator.translate_epub(
        input_file="book.epub",
        target_language="zh",
        translate_images=False,
        translate_toc=True,
        resume=True
    )

    print(f"完成: {output}")

asyncio.run(main())
```

更多用法见 [example.py](../example.py)。

## 4. 常用命令

```bash
# 基础翻译（中文）
python main.py translate book.epub -l zh

# 翻译为英文 / 日文
python main.py translate book.epub -l en
python main.py translate book.epub -l ja

# 翻译图片中的文字（需要支持 Vision 的模型）
python main.py translate book.epub -l zh --images

# 不翻译目录
python main.py translate book.epub -l zh --no-toc

# 忽略缓存，从头重新翻译
python main.py translate book.epub -l zh --no-resume

# 清除缓存
python main.py clear-cache book.epub -l zh

# 查看版本
python main.py version
```

`--resume` 默认开启，所以**续译不需要额外参数**：重复运行同一条翻译命令即可。

## 5. 运行时你会看到什么

```
📝 诊断日志: .epub_translation_logs\book_20260811_143022.log

[3/27] 章节 3: Chapter_2.xhtml
    最大分块 2183 tokens，合计 6420 tokens
  切分为 3 块
正在翻译章节3...（剩余 2 块）
正在翻译章节3...（剩余 1 块）
正在翻译章节3...（剩余 0 块）
正在保存章节[3]...
```

偶尔会出现保存被拒——这是正常的护栏机制，Agent 会按提示补译后重试：

```
  ⤺ 章节 12 保存被拒：标签数 41/68，疑似漏译
```

某章连续失败超过 `MAX_CHAPTER_RETRIES` 次会被记入失败列表，该章在输出文件里保持原文，其余章节照常翻译。修完配置后重跑同一条命令，只会重译这些失败章节。

## 6. 进阶配置

编辑 `auto_epub/settings.py`：

```python
# 分块与输出：INPUT 必须显著小于 OUTPUT
# 译文 + HTML 标签 + JSON 转义叠加后，输出通常是输入的 1.5~2 倍
INPUT_MAX_TOKENS = 2500
OUTPUT_MAX_TOKENS = 8192

# 单章失败后的重试次数
MAX_CHAPTER_RETRIES = 2

# 漏译判定：块级标签（p/div/h*/li…）比例下限，低于此判漏译
MIN_BLOCK_TAG_RATIO = 0.8
# 内联标签（a/em/span…）比例下限，低于此只告警，不阻塞保存
MIN_INLINE_TAG_RATIO = 0.8

# 启用图片翻译（也可用 --images 覆盖）
TRANSLATE_IMAGES = True  # 默认 False

# 翻译温度，越低越稳定
TEMPERATURE = 0.1

# 控制台是否打印诊断细节（不影响日志文件）
DEBUG_MODE = True
```

自定义翻译风格与规则：修改 `settings.py` 中的 `AGENT_SYSTEM_PROMPT`。其中的 `{target_language}` 由 `client.py` 注入，改写时要保留这个占位符。

## 7. 项目结构

```
auto-epub/
├── auto_epub/
│   ├── __init__.py
│   ├── models.py              # 数据模型
│   ├── agent_tools.py         # Agent 工具集 + EpubContext
│   ├── epub_tools.py          # EPUB 底层工具（分块、导航文档）
│   ├── translator.py          # 翻译编排器
│   ├── client.py              # Agent 工厂
│   ├── logger.py              # 诊断日志
│   ├── cache_manager.py       # 缓存管理
│   ├── concurrent_manager.py  # 并发控制（当前未使用）
│   ├── cli.py                 # 命令行接口
│   ├── config.py              # 配置加载
│   └── settings.py            # 常量配置 + 系统提示词
├── docs/                      # 文档
├── main.py                    # CLI 入口
├── example.py                 # 使用示例
├── .env                       # API 配置（需创建）
├── requirements.txt           # 依赖列表
└── README.md                  # 完整文档
```

各文件职责详见 [FILE_MAPPING.md](FILE_MAPPING.md)，设计取舍详见 [ARCHITECTURE.md](ARCHITECTURE.md)。

## 8. 常见问题

排查任何问题的第一步都是看 `.epub_translation_logs/` 下的日志文件。

**Q: 报错「模型输出了文本形式的工具调用」？**

通常是输出被 `max_tokens` 截断，导致工具调用参数的 JSON 不完整，模型退化成把 `<tool_call>` 当普通文本吐出来。查日志里该章的分块 tokens：

```python
INPUT_MAX_TOKENS = 2000   # 调小分块
OUTPUT_MAX_TOKENS = 8192  # 或调大输出上限
```

**Q: 提示「保存被拒：标签数 X/Y，疑似漏译」？**

模型省略了部分内容，工具会要求它补译。日志目录下的 `*_chN_rejected.html` 是被拒的译文，可据此确认漏了哪一段。频繁出现说明分块偏大。

**Q: API 超时？**

```python
TIMEOUT = 120            # 增加超时
INPUT_MAX_TOKENS = 1500  # 或减小分块
```

**Q: 翻译中断了？**

直接重跑同一条命令，已完成的章节会跳过：

```bash
python main.py translate book.epub -l zh
```

**Q: 侧边栏目录还是原文？**

侧边栏来自 EPUB3 导航文档（nav.xhtml）而非 `toc.ncx`，两者都会被翻译。若仍是原文，检查日志里「导航文档 ... 同步 N 条目录译文」这一行；同步为 0 说明导航文档的标题文本与 `book.toc` 对不上。

**Q: 译后书籍样式变了？**

已知限制：保存章节时写入的是 body 级分块的拼接结果，`<html>` / `<head>` 外壳及其中的 CSS 链接会丢失。

**Q: 如何翻译图片？**

```bash
# 需要支持 Vision 的模型
python main.py translate manga.epub -l zh --images
```

## 9. 下一步

- 阅读完整 [README.md](../README.md)
- 查看 [ARCHITECTURE.md](ARCHITECTURE.md) 了解为什么切分和写盘都放在 Python 侧
- 查看 [example.py](../example.py) 了解编程式用法
- 自定义 `settings.py` 中的提示词

## 📮 获取帮助

- 🐛 Issues: https://github.com/leeyorke/auto-epub/issues

祝使用愉快！🎉
