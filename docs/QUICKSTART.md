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

# 清除指定书的缓存
python main.py clear book.epub -l zh

# 清空全部翻译缓存（不指定书籍路径）
python main.py clear

# 查看版本
python main.py version
```

`--resume` 默认开启，所以**续译不需要额外参数**：重复运行同一条翻译命令即可。

## 5. 运行时你会看到什么

默认只打印进度：

```
📝 诊断日志: ~/.auto-epub/logs/book_20260811_143022.log

[3/27] 章节 3: Chapter_2.xhtml
切分为 3 块
正在保存章节[3]...
```

加 `-v` 会多出每块每次尝试的一行（这一行在日志文件里始终都有）：

```
[3/27] 章节 3: Chapter_2.xhtml
    最大分块 2183 tokens，合计 6420 tokens
切分为 3 块
    ✔ 块 1/3 第 1 次，chars=8431→3902，block_tags=22/22(1.00)，输入=3140、输出=5218、请求数=1，结束原因=stop
    ✔ 块 2/3 第 1 次，chars=7118→3245，block_tags=19/19(1.00)，…
    ✔ 块 3/3 第 0 次，命中缓存，chars=6205→2871，block_tags=17/17(1.00)
正在保存章节[3]...
```

`✘` 开头的行是这一块本次尝试没通过，会自动重译（每块最多 3 次）；`原因=` 后面写明是漏译、被截断，还是超时 / 内容审查这类 API 层异常。整章有块最终没过时会打印：

```
❌ 章节 12 第 1 次尝试未通过：还有 2/2 块没有译文（0/2 块通过校验）
```

坏块的重试额度用尽后不会再做无意义的章级重试，该章记入失败列表、在输出文件里保持原文，其余章节照常翻译。修完配置后重跑同一条命令，**已经通过校验的块不会重翻**。

## 6. 进阶配置

编辑 `auto_epub/settings.py`：

```python
# 分块与输出：INPUT 必须显著小于 OUTPUT
# 译文 + HTML 标签叠加后，输出通常是输入的 1.3~2 倍
INPUT_MAX_TOKENS = 5000
OUTPUT_MAX_TOKENS = 16384

# 单章失败后的重试次数（只重跑没进缓存的坏块）
MAX_CHAPTER_RETRIES = 2
# 单块失败后的重试次数（每块共 3 次尝试，跨章级重试累计）
MAX_CHUNK_RETRIES = 2

# 块与块之间传递上下文的"接力包"token 硬上限
CARRYOVER_MAX_TOKENS = 2000

# 漏译判定：块级标签（p/div/h*/li…）比例下限，低于此判漏译
MIN_BLOCK_TAG_RATIO = 0.8
# 内联标签（a/em/span…）比例下限，低于此只告警，不阻塞保存
MIN_INLINE_TAG_RATIO = 0.8

# 启用图片翻译（也可用 --images 覆盖）
TRANSLATE_IMAGES = True  # 默认 False

# 翻译温度，越低越稳定
TEMPERATURE = 0.1
```

控制台详细程度用命令行的 `-v` / `-q` 控制，不影响日志文件（文件始终完整）。

自定义翻译风格与规则：章节正文改 `settings.py` 中的 `CHUNK_SYSTEM_PROMPT`，目录与图片阶段改 `AGENT_SYSTEM_PROMPT`。两者里的 `{target_language}` 由 `client.py` 注入，改写时要保留这个占位符。

## 7. 项目结构

```
auto-epub/
├── auto_epub/
│   ├── __init__.py
│   ├── models.py              # 数据模型
│   ├── agent_tools.py         # 目录/图片工具集 + EpubContext + finalize_chapter
│   ├── chunk_translator.py    # 块级翻译核心（章节正文，无工具）
│   ├── epub_tools.py          # EPUB 底层工具（分块、导航文档）
│   ├── translator.py          # 翻译编排器
│   ├── client.py              # Agent 工厂（带工具 / 无工具各一个）
│   ├── logger.py              # 诊断日志
│   ├── cache_manager.py       # 缓存管理
│   ├── concurrent_manager.py  # 并发控制（当前未使用）
│   ├── cli.py                 # 命令行接口
│   ├── config.py              # 配置加载
│   └── settings.py            # 常量配置 + 两份系统提示词
├── docs/                      # 文档
├── main.py                    # CLI 入口
├── example.py                 # 使用示例
├── .env                       # API 配置（需创建）
├── requirements.txt           # 依赖列表
└── README.md                  # 完整文档
```

各文件职责详见 [FILE_MAPPING.md](FILE_MAPPING.md)，设计取舍详见 [ARCHITECTURE.md](ARCHITECTURE.md)。

## 8. 常见问题

排查任何问题的第一步都是看 `~/.auto-epub/logs/` 下的日志文件。

**Q: 报错「模型输出了文本形式的工具调用」？**

章节正文这条路上已经没有工具了，出现这个标记纯属模型自己编戏，该块会被作废重译（每块最多 3 次）。若在目录 / 图片阶段出现，通常是输出被 `max_tokens` 截断、工具调用参数的 JSON 不完整所致。

**Q: 提示「块级标签 X/Y，有整段没译到」？**

模型省略了部分内容，Python 会让该块重译。被拒的原始输出会完整存成 `{日志名}_ch{章号}_try{章级尝试}_chunk{块号}_a{块级尝试}.html`（块号从 0 数），可据此确认漏了哪一段、或者结尾是不是被截断了。频繁出现说明分块偏大：

```python
INPUT_MAX_TOKENS = 4000   # 调小分块
```

**Q: 提示「保存被拒：还有 N 块没有译文」？**

有块三次尝试都没通过，整章不算完成——这是刻意的：残章不许标记完成，否则 `--resume` 会永久跳过它。看日志里那几块每次尝试的失败原因（超时、内容审查、漏译各有不同处置）。

**Q: API 超时？**

`TIMEOUT` 是单次 HTTP 请求的超时，而 SDK 自己还会重试两次，所以一次"块级尝试失败"的实际耗时约 3 倍。输出上万 token 的大块在 60 秒线上比较紧：

```python
TIMEOUT = 120            # 增加超时
INPUT_MAX_TOKENS = 4000  # 或减小分块，让单次输出变小
```

注意超时会占用该块的重试额度（每块共 3 次），三次全超时的块会从头到尾拿不到译文。

**Q: 某块稳定报 451 / censorship_blocked？**

供应商侧的内容过滤，重试必然是同样结果，换模型或换供应商才有用。当前行为是这块耗尽尝试次数、整章拒绝保存并如实报失败。

**Q: 翻译中断了？**

直接重跑同一条命令。已完成的章节整章跳过，**没译完的章节只补没通过校验的块**（通过的块已经进了块缓存）：

```bash
python main.py translate book.epub -l zh
```

**Q: 调整了 `INPUT_MAX_TOKENS`，旧的块缓存会不会错位？**

不会。块缓存按块原文的哈希寻址，切分一变哈希就对不上，旧缓存自动失效重译。

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
