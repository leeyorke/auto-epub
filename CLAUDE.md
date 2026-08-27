# CLAUDE.md

本文件为 Claude Code (claude.ai/code) 在处理此存储库中的代码时提供指导。

## 文档分工（重要）

**架构、设计决策、不变量、诊断日志一律写进 `docs/ARCHITECTURE.md`，不要往本文件堆积。** 本文件只保留命令、导航和红线索引。

| 文件 | 内容 |
|------|------|
| `docs/ARCHITECTURE.md` | 架构分层、数据流、**关键设计决策与不变量**、诊断日志。改代码前先读，新结论追加到这里 |
| `docs/FILE_MAPPING.md` | 文件与符号对照表 |
| `docs/QUICKSTART.md`、`README.md` | 使用文档 |

## 常用命令

```bash
# 安装依赖
uv sync

# 运行翻译（CLI）
python main.py translate book.epub -l zh

# 续译（默认开启，可显式指定）
python main.py translate book.epub -l zh --resume

# 显示诊断细节（每块每次尝试一行、被拒译文路径等）
python main.py translate book.epub -l zh -v

# 只输出错误（静默进度与摘要）
python main.py translate book.epub -l zh -q

# 清理指定书的翻译缓存
python main.py clear book.epub -l zh

# 清空全部翻译缓存（不指定书籍路径）
python main.py clear

# 运行示例脚本
python example.py

# 运行单元测试（套件不联网、不写真实缓存目录）
python -m pytest
# 只跑某一个模块的测试 / 带详细输出
python -m pytest tests/test_chunk_translator.py -v

# 启用提交前测试闸门（每个克隆执行一次；core.hooksPath 不随仓库分发）
git config core.hooksPath .githooks

# 代码检查与格式化
uv run ruff check .
uv run ruff format .
```

## 项目定位

EPUB 电子书多语言翻译工具，基于 **pydantic-ai**。**章节正文的翻译是"一块原文进、一块译文出"的纯函数调用**：Python 负责流程编排、内容切分和块循环，块级 Agent 没有任何工具，模型的纯文本输出就是译文。**FunctionToolset 只剩目录与图片两个阶段在用。**

**技术栈**: Python >= 3.10、pydantic-ai、ebooklib、BeautifulSoup4 + lxml、typer、tiktoken、ruff、uv

## 目录结构

```
auto_epub/
├── chunk_translator.py  # 块级翻译核心：接力包、输出清理、块级校验与重试
├── agent_tools.py   # 目录/图片工具集 + EpubContext + finalize_chapter
├── translator.py    # 编排器：章节循环、章级重试、目录/图片阶段、收尾写盘
├── client.py        # Agent 创建工厂（epub_agent 带工具 / chunk_agent 无工具）
├── epub_tools.py    # EPUB 底层操作（章节提取、递归分块、导航文档读写）
├── logger.py        # 诊断日志
├── cache_manager.py # 断点续传缓存：进度 / 章节 / 块（加锁 + 原子写入）
├── models.py        # Pydantic 数据模型
├── cli.py           # Typer CLI 入口
├── config.py        # .env 加载
├── settings.py      # 全局常量 + 两份系统提示词
└── concurrent_manager.py  # 并发控制器（当前未被主流程使用）
main.py / example.py     # CLI 入口 / 编程式调用示例
```

## 动手前必须知道的红线

以下每条都有"错了会导致整章甚至整本书白翻"的实测记录，**改动前先读 `docs/ARCHITECTURE.md` 的「关键设计决策与不变量」**：

1. **切分只能由 Python 做**，不能暴露成 Agent 工具
2. **分块发放必须非破坏性**：切分每章只做一次，写入靠 `chunk_index` 定位；重试只清"判定漏译的块"的译文，绝不重切、不动原文分块
3. **单请求输入必须与块序号无关**：每块一次独立 `agent.run`，上下文只走常量级的接力包。让它随块号增长就会重演 37 块章节撞 400 的故障
4. **目录 / 图片的 `agent.run` 必须包在 `sequential_tool_calls()` 里**：同响应内的多个同步工具会被真正并行，共享进度文件会互相覆盖
5. **进度文件必须原子写入，"读—改—写"必须走 `update_progress` 留在锁内**
6. **判定不完整的章节不许标记完成**，否则 `--resume` 会永久跳过残章；块粒度上的同一条：**只有通过 `validate_chunk` 的块才写进块缓存**
7. **漏译只能用块级标签判定**，内联标签（`a`/`em`/`span`）模型会系统性吞掉，按全标签口径会把译完的内容误杀。也**不许做标签配平检查**——分块本来就可能是半截片段
8. **块级与章级重试不许相乘**：`chunk_attempts` 跨章级重试累计，**绝不能在 `prepare_chapter` 里重置**
9. **每块每次尝试都要留一行日志**（`logger.chunk_result`），包括"额度已用尽、这次不发请求"这种什么都没干的情况；否则失败在日志里是一片空白
10. **`INPUT_MAX_TOKENS` 必须显著小于 `OUTPUT_MAX_TOKENS`**（当前 5000 / 32768）

只对目录 / 图片阶段成立的两条（章节正文已随工具下线一并消失，但回退成工具型就会复活）：**工具返回值不许承诺机制上做不到的事**；**保存过的对象不能让任何入口再对它发号施令**，否则工具互相打脸、模型没有合法出口，会转圈到 `request_limit`。

## 项目特有约定

- **依赖管理**: 使用 uv，镜像源为清华大学 PyPI 镜像（在 pyproject.toml 中配置）
- **API 配置**: 通过 `.env` 文件加载，支持任意兼容 OpenAI API 格式的供应商（base_url, api_key, model）
- **应用数据**: 缓存与日志默认在 `~/.auto-epub/`（`cache/`、`logs/`），首次运行会自动把工作目录里的旧版 `.epub_translation_cache` / `.epub_translation_logs` 整体搬过去
- **版本**: 定义在 `auto_epub/__init__.py` 的 `__version__`
- **测试**: 单元测试在 `tests/`，`python -m pytest` 即可运行；**不发 API、不落盘**——cache/log 一律 Mock 或 None，EPUB 对象在内存里构造。布局与红线覆盖关系见 `docs/ARCHITECTURE.md`「单元测试」一节
