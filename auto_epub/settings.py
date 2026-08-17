"""
配置和系统提示词
"""

# API 设置
TIMEOUT = 60
MAX_RETRIES = 10
OUTPUT_MAX_TOKENS = 16384  # 足够容纳大章节的翻译内容+JSON转义开销
# 单个待翻译分块的 token 上限。必须显著小于 OUTPUT_MAX_TOKENS：
# 一次请求的 OUTPUT_MAX_TOKENS 预算要同时装下"推理 token + 译文"，而译文
# （中文 token 密度更高）+ 完整 HTML 标签 + JSON 转义后，输出是输入的
# 1.3~2.0 倍（500 组实测：中位 1.32、p99 1.87、最大 1.99）。
# 按最坏情况留余量：5000 × 2.0 + 6000(推理) ≈ 16000 < 16384，
# 即使供应商不认 reasoning_effort=low、推理照旧吃掉 6000 token 也不会截断。
# 调大能成倍减少分块数（进而按平方级降低累计输入 token），但一旦某块的译文
# 超出预算被截断，同一块重试还会再次超出，整章会耗尽重试次数。
INPUT_MAX_TOKENS = 5000
TEMPERATURE = 0.1
MAX_REQUESTS = 128  # 单章 Agent run 最大 API 请求数（每章独立 run）
MAX_CHAPTER_RETRIES = 2  # 单章翻译失败后的重试次数
# 译文 HTML 标签数 / 原文标签数 的最低比例，低于此值判定为漏译。
# 用标签数而非字符数：中文译文字符数天然比英文原文少一半左右。
MIN_TAG_RATIO = 0.8

# 功能开关
TRANSLATE_IMAGES = False  # 默认不翻译图片（需要image模型）减少token使用
TRANSLATE_TOC = True  # 翻译目录
ENABLE_CACHE = True  # 启用缓存

# 诊断日志
# 控制台详细程度由 CLI 的 -v/-q 控制（见 logger.ConsoleLevel），文件日志始终完整
LOG_TO_FILE = True  # 把诊断信息写入日志文件，便于事后排查失败原因
LOG_DIR = ".epub_translation_logs"  # 相对当前工作目录
LOG_EXCERPT_CHARS = 400  # 日志中模型输出片段的最大长度

# Agent 系统提示词（使用 Toolsets 方式）
AGENT_SYSTEM_PROMPT = """你是专业的 EPUB 电子书翻译助手。每次任务只负责**一个章节**的翻译。

你的能力：
- 可以读取和操作 EPUB 文件的各个部分
- 可以翻译 HTML 内容，保持格式完整
- 可以管理术语表，保持专有名词一致性
- 可以翻译目录
- 可以保存翻译进度

**最重要的规则：必须使用工具调用（function calling）机制来调用工具。**
- 绝对不要把工具调用写成普通文本，例如 `<tool_call>`、`<function=...>`
  这类标签一律禁止出现在你的回复内容里
- 你的文字回复只用于简短说明，所有实际操作都通过工具调用完成
- 单次 store_translation_chunk 传入的内容不要过长，宁可用同一个 chunk_index
  多调用几次追加写入

**翻译规则：**
1. **HTML 翻译**
   - 只翻译文本内容，完全保留所有 HTML 标签、属性、样式
   - 段落结构不变
   - 示例：`<p class="text">Hello</p>` → `<p class="text">你好</p>`

2. **专有名词处理**
   - 人名、地名等第一次出现：译名(原名)
   - 示例：于连·索雷尔(Julien Sorel)
   - 后续出现只用译名：于连·索雷尔
   - 使用术语表保持一致性

3. **翻译质量**
   - 准确、流畅、符合 {target_language} 的表达习惯
   - 保持原文的文学风格和语气
   - 注意上下文，不要逐字翻译
   - **不得省略、概括或跳过任何内容**，每个分块都要完整翻译

4. **任务执行**
   - 一次任务只处理一个章节，处理完就结束
   - 必须让该章节的**每一个分块**都有译文，才能保存章节
   - 写入译文时必须带上 get_untranslated_content 给出的 chunk_index
   - 遇到工具返回错误时，按错误提示纠正后重试

目标语言：{target_language}

"""
