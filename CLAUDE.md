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

# 显示诊断细节（工具调用序列、被拒译文路径等）
python main.py translate book.epub -l zh -v

# 只输出错误（静默进度与摘要）
python main.py translate book.epub -l zh -q

# 清理缓存
python main.py clear-cache book.epub -l zh

# 运行示例脚本
python example.py

# 代码检查与格式化
uv run ruff check .
uv run ruff format .
```

## 项目定位

EPUB 电子书多语言翻译工具，基于 **pydantic-ai** 的 **Agent + FunctionToolset** 架构。**Python 负责流程编排与内容切分，Agent 只在"翻译一个章节"这一粒度上自主调度工具。**

**技术栈**: Python >= 3.10、pydantic-ai、ebooklib、BeautifulSoup4 + lxml、typer、tiktoken、ruff、uv

## 目录结构

```
auto_epub/
├── agent_tools.py   # Agent 工具集 + EpubContext + 目录/导航同步辅助
├── translator.py    # 编排器：章节循环、重试、目录/图片阶段、收尾写盘
├── client.py        # Agent 创建工厂（Model/Provider/Settings/Toolsets）
├── epub_tools.py    # EPUB 底层操作（章节提取、递归分块、导航文档读写）
├── logger.py        # 诊断日志
├── cache_manager.py # 断点续传缓存（加锁 + 原子写入）
├── models.py        # Pydantic 数据模型
├── cli.py           # Typer CLI 入口
├── config.py        # .env 加载
├── settings.py      # 全局常量 + Agent 系统提示词
└── concurrent_manager.py  # 并发控制器（当前未被主流程使用）
main.py / example.py     # CLI 入口 / 编程式调用示例
```

## 动手前必须知道的红线

以下每条都有"错了会导致整章甚至整本书白翻"的实测记录，**改动前先读 `docs/ARCHITECTURE.md` 的「关键设计决策与不变量」**：

1. **切分只能由 Python 做**，不能暴露成 Agent 工具
2. **分块发放必须非破坏性**：只发放"还没有译文的块"，写入靠 `chunk_index` 定位，不用队列 pop；作废重发只清译文、不动原文分块
3. **每次 `agent.run` 必须包在 `sequential_tool_calls()` 里**：同响应内的多个同步工具会被真正并行，共享状态会互相覆盖
4. **进度文件必须原子写入，"读—改—写"必须走 `update_progress` 留在锁内**
5. **判定不完整的章节不许标记完成**，否则 `--resume` 会永久跳过残章
6. **漏译只能用块级标签判定**，内联标签（`a`/`em`/`span`）模型会系统性吞掉，按全标签口径会把译完的章节误杀
7. **保存过的章节必须让所有章节级工具闭嘴**，否则工具之间互相打脸、模型没有合法出口，会转圈到 `request_limit`
8. **工具返回值不许承诺机制上做不到的事**：说了"请补译"就必须真能把原文发回给模型
9. **每一次工具调用都要留一行日志**：工具的错误是 `return` 字符串而非 `raise ModelRetry`，pydantic-ai 视为成功，可以无限循环
10. **`INPUT_MAX_TOKENS` 必须显著小于 `OUTPUT_MAX_TOKENS`**（当前 5000 / 16384）

## 项目特有约定

- **依赖管理**: 使用 uv，镜像源为清华大学 PyPI 镜像（在 pyproject.toml 中配置）
- **API 配置**: 通过 `.env` 文件加载，支持任意兼容 OpenAI API 格式的供应商（base_url, api_key, model）
- **版本**: 定义在 `auto_epub/__init__.py` 的 `__version__`
- **无测试文件**: 当前没有单元测试或集成测试，结论靠真实翻译的诊断日志验证
