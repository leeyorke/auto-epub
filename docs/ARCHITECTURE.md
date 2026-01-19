# 架构设计文档

## 总体架构

本项目采用 **Agent + Toolsets** 架构，基于 pydantic-ai 框架开发。

```
┌─────────────────────────────────────────────────────────┐
│                       CLI / API                          │
│                     (cli.py, client.py)                  │
└──────────────────────┬──────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────┐
│                   EpubTranslator                         │
│              (translator.py - 协调器)                    │
│                                                          │
│  - 加载 EPUB                                             │
│  - 初始化缓存                                            │
│  - 构建任务提示词                                        │
│  - 调用 Agent 执行翻译                                   │
└──────────────────────┬──────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────┐
│                 pydantic-ai Agent                        │
│                   (智能决策层)                           │
│                                                          │
│  - 理解翻译任务                                          │
│  - 决定工具调用顺序                                      │
│  - 处理翻译逻辑                                          │
│  - 维护术语一致性                                        │
└──────────────────────┬──────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────┐
│                  EPUB Toolsets                           │
│                  (tools.py - 工具层)                     │
│                                                          │
│  📖 书籍信息工具:                                        │
│     - get_book_info                                      │
│     - list_chapters                                      │
│     - get_translation_progress                           │
│                                                          │
│  📝 章节翻译工具:                                        │
│     - get_chapter_content                                │
│     - save_translated_chapter                            │
│                                                          │
│  📚 术语管理工具:                                        │
│     - get_glossary                                       │
│     - update_glossary                                    │
│                                                          │
│  📑 目录翻译工具:                                        │
│     - translate_toc                                      │
│     - save_translated_toc                                │
│                                                          │
│  🖼️ 图片翻译工具:                                        │
│     - list_images                                        │
│     - get_image_base64                                   │
│     - save_translated_image                              │
│                                                          │
│  💾 文件操作工具:                                        │
│     - finalize_epub                                      │
└──────────────────────┬──────────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────────┐
│                  支持模块                                 │
│                                                          │
│  📦 EpubTools (epub_tools.py)                            │
│     - EPUB 文件解析                                       │
│     - HTML 处理                                           │
│     - Token 计数                                          │
│                                                          │
│  💾 CacheManager (cache_manager.py)                      │
│     - 翻译进度缓存                                        │
│     - 章节缓存                                            │
│     - 图片缓存                                            │
│                                                          │
│  📋 Models (models.py)                                   │
│     - Pydantic 数据模型                                   │
│     - 类型安全                                            │
└─────────────────────────────────────────────────────────┘
```

## 核心组件

### 1. Agent（智能决策层）

**职责：**
- 接收翻译任务描述
- 理解任务需求
- 智能决策调用哪些工具、以什么顺序调用
- 处理翻译逻辑（真正的翻译工作）
- 维护上下文和术语一致性

**特点：**
- 基于模型的智能决策
- 可自适应不同的 EPUB 结构
- 自动处理错误和重试

### 2. Toolsets（工具层）

**设计原则：**
- 每个工具职责单一
- 工具之间相互独立
- 通过 `RunContext[EpubContext]` 共享状态

**工具分类：**

**信息查询类：**
```python
get_book_info()           # 获取书籍元信息
list_chapters()           # 列出所有章节
get_chapter_content()     # 读取章节内容
list_images()             # 列出所有图片
get_glossary()            # 获取术语表
get_translation_progress() # 获取翻译进度
```

**翻译操作类：**
```python
save_translated_chapter()  # 保存翻译后的章节
update_glossary()          # 更新术语表
save_translated_toc()      # 保存翻译后的目录
save_translated_image()    # 保存翻译后的图片
```

**文件操作类：**
```python
finalize_epub()            # 保存最终的 EPUB 文件
```

### 3. EpubContext（上下文）

**作用：**
- 作为 Agent 的 `deps`，在所有工具间共享
- 包含 EPUB 对象、缓存管理器、术语表等

**结构：**
```python
class EpubContext:
    book: epub.EpubBook          # EPUB 对象
    target_language: str         # 目标语言
    cache_key: str               # 缓存键
    cache_manager: CacheManager  # 缓存管理器
    glossary: Dict[str, str]     # 术语表
    source_language: str         # 源语言
    chapters: List               # 章节列表
    images: List                 # 图片列表
```

## 数据流

### 翻译流程

```
1. 用户调用
   ↓
2. CLI 解析参数
   ↓
3. create_translator() 创建翻译器
   ├─ create_epub_agent() 创建 Agent
   │  └─ 注册 epub_toolset
   └─ new EpubTranslator(agent)
   ↓
4. translator.translate_epub()
   ├─ 读取 EPUB 文件
   ├─ 加载/创建缓存
   ├─ 创建 EpubContext
   └─ 构建任务提示词
   ↓
5. agent.run(task_prompt, deps=context)
   ↓
6. Agent 执行任务
   ├─ 调用 list_chapters 了解结构
   ├─ 循环翻译每个章节:
   │  ├─ get_chapter_content 读取
   │  ├─ Agent 自己翻译内容
   │  ├─ update_glossary 记录术语
   │  └─ save_translated_chapter 保存
   ├─ 翻译目录 (可选)
   ├─ 翻译图片 (可选)
   └─ finalize_epub 保存文件
   ↓
7. 返回输出文件路径
```

### 缓存机制

```
.epub_translation_cache/
├── {md5_hash}.json              # 翻译进度
│   ├─ source_lang
│   ├─ target_lang
│   ├─ total_chapters
│   ├─ completed_chapters []     # 已完成章节ID
│   ├─ failed_chapters []
│   ├─ glossary {}               # 术语表
│   ├─ toc_translated
│   └─ images_translated {}
│
└── {md5_hash}/
    ├── chapters/
    │   ├── {chapter_id_hash}.html
    │   └── ...
    └── images/
        ├── {image_name_hash}
        └── ...
```

**断点续传逻辑：**
1. 计算缓存 key (md5(file_path + target_lang))
2. 加载进度文件
3. 获取 completed_chapters 列表
4. 跳过已完成的章节
5. 继续翻译未完成的章节

## 设计优势

### 1. 灵活性

**Agent 自主决策：**
- 可以根据 EPUB 结构调整策略
- 可以处理异常情况（如缺少目录）
- 可以优化翻译顺序

**工具可扩展：**
- 添加新工具不影响现有代码
- 工具间相互独立

### 2. 可维护性

**职责清晰：**
- Agent: 决策和翻译
- Toolsets: EPUB 操作
- Translator: 协调
- CacheManager: 持久化

**类型安全：**
- 使用 Pydantic 模型
- RunContext 提供类型提示

### 3. 鲁棒性

**错误处理：**
- Agent 自动重试（retries=3）
- 工具返回错误信息，Agent 可理解并处理
- 缓存保证不会丢失进度

**状态管理：**
- EpubContext 统一管理状态
- 术语表自动同步
- 进度实时保存

## 与传统方式对比

### 传统方式（硬编码流程）

```python
def translate_epub():
    chapters = get_chapters()
    for chapter in chapters:
        content = read_chapter(chapter)
        translated = llm.translate(content)
        save_chapter(chapter, translated)
```

**缺点：**
- 流程固定，无法适应不同情况
- 错误处理复杂
- 难以扩展

### Toolsets 方式（Agent 决策）

```python
def translate_epub():
    agent.run("""
    你是翻译助手，请翻译这本书。
    可用工具: get_chapters, read_chapter, save_chapter, ...
    """)
```

**优点：**
- Agent 智能决策流程
- 自适应各种情况
- 易于扩展（添加工具即可）
- Agent 可自我纠错

## 未来扩展方向

### 1. 添加新工具

```python
@epub_toolset.tool
def extract_footnotes(ctx: RunContext[EpubContext]) -> str:
    """提取所有脚注"""
    pass

@epub_toolset.tool
def translate_metadata(ctx: RunContext[EpubContext]) -> str:
    """翻译书籍元数据（标题、作者等）"""
    pass
```

### 2. 多 Agent 协作

```python
# 专门的术语管理 Agent
glossary_agent = Agent(...)

# 专门的图片翻译 Agent
image_agent = Agent(...)

# 主 Agent 协调子 Agent
```

### 3. 增强缓存

```python
# 跨书籍共享术语表
global_glossary = load_global_glossary()

# 预翻译常见词汇
common_terms = preload_common_terms()
```

### 4. 质量检查

```python
@epub_toolset.tool
def validate_translation(
    ctx: RunContext[EpubContext],
    chapter_index: int
) -> str:
    """检查翻译质量"""
    # 检查术语一致性
    # 检查 HTML 完整性
    # 检查长度是否合理
    pass
```

## 总结

本架构充分利用了 pydantic-ai 的 Toolsets 特性，将复杂的 EPUB 翻译任务分解为：

1. **Agent**：智能决策和翻译
2. **Tools**：原子化的 EPUB 操作
3. **Context**：统一的状态管理
4. **Cache**：可靠的进度保存

这种设计使得系统：
- ✅ 灵活应对各种 EPUB 结构
- ✅ 易于维护和扩展
- ✅ 鲁棒性强，可靠性高
- ✅ 类型安全，开发体验好
