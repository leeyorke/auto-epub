# 🚀 快速开始指南

## 1. 安装

```bash
# 克隆项目
git clone https://github.com/leeyorke/auto-epub.git
cd epub-translator

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

编辑 `.env`，填入你的模型供应商：

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

## 4. 常用命令

```bash
# 基础翻译（中文）
python main.py translate book.epub -l zh

# 翻译为英文
python main.py translate book.epub -l en

# 翻译为日文
python main.py translate book.epub -l ja

# 翻译图片
python main.py translate book.epub -l zh --images

# 不使用缓存（重新翻译）
python main.py translate book.epub -l zh --no-resume

# 清除缓存
python main.py clear-cache book.epub -l zh
```

## 5. 进阶配置

编辑 `auto_epub/settings.py`：

```python
# 启用图片翻译
TRANSLATE_IMAGES = True  # 默认 False

# 翻译温度
TEMPERATURE = 0.1  # 0-1，越低越稳定
```

## 6. 目录结构

完整的项目结构应该是：

```
epub-translator/
├── auto_epub/
│   ├── __init__.py
│   ├── models.py              # 数据模型
│   ├── agent_tools.py         # agent 工具集
│   ├── epub_tools.py          # EPUB 工具
│   ├── cache_manager.py       # 缓存管理
│   ├── concurrent_manager.py  # 并发控制
│   ├── translator.py          # 核心翻译器
│   ├── client.py              # Agent 客户端
│   ├── cli.py                 # 命令行接口
│   ├── config.py              # 配置加载
│   └── settings.py            # 常量配置
├── main.py                    # 主入口
├── example.py                 # 使用示例
├── .env                       # API 配置（需创建）
├── .env.example               # 配置模板
├── requirements.txt           # 依赖列表
├── pyproject.toml             # 项目配置
├── QUICKSTART.md              # 快速开发文档
└── README.md                  # 完整文档
```

## 7. 常见问题

**Q: 翻译速度慢？**
```bash
# 调整输入输出token数
OUTPUT_MAX_TOKENS = 6000
INPUT_MAX_TOKENS = 4000
```

**Q: API 超时？**
```python
# 在 settings.py 中调整
TIMEOUT = 120  # 增加到 120 秒
INPUT_MAX_TOKENS = 1500  # 减少输入大小
```

**Q: 翻译中断了？**
```bash
# 使用 --resume 继续
python main.py translate book.epub -l zh --resume
```

**Q: 如何翻译图片？**
```bash
# 需要 image 模型
python main.py translate manga.epub -l zh --images
```

## 8. 下一步

- 阅读完整 [README.md](README.md)
- 查看 [example.py](example.py) 了解更多用法
- 自定义 `settings.py` 中的提示词
- 贡献代码或报告问题

## 📮 获取帮助

- 🐛 Issues: https://github.com/leeyorke/auto-epub/issues

祝使用愉快！🎉
