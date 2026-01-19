"""
EPUB Translator 使用示例
"""

import asyncio

from auto_epub import create_translator


async def example_basic_translation():
    """示例 1: 基础翻译"""
    print("=" * 50)
    print("示例 1: 基础翻译")
    print("=" * 50)

    translator = create_translator(target_language="zh", cache_enabled=True)

    output = await translator.translate_epub(
        input_file="example.epub",
        target_language="zh",
        translate_images=False,
        translate_toc=True,
        resume=True,
    )

    print(f"✓ 翻译完成: {output}")


async def example_with_images():
    """示例 2: 翻译包含图片的 EPUB"""
    print("=" * 50)
    print("示例 2: 翻译图片")
    print("=" * 50)

    translator = create_translator(target_language="zh", cache_enabled=True)

    output = await translator.translate_epub(
        input_file="manga.epub",
        target_language="zh",
        translate_images=True,  # 启用图片翻译
        translate_toc=True,
        resume=True,
    )

    print(f"✓ 翻译完成: {output}")


async def example_custom_settings():
    """示例 3: 自定义设置"""
    print("=" * 50)
    print("示例 3: 自定义设置")
    print("=" * 50)

    translator = create_translator(
        target_language="ja",
        cache_enabled=False,  # 不使用缓存
    )

    output = await translator.translate_epub(
        input_file="book.epub",
        target_language="ja",
        translate_images=False,
        translate_toc=True,
        resume=False,  # 重新翻译
    )

    print(f"✓ 翻译完成: {output}")


async def example_programmatic_usage():
    """示例 4: 编程式使用（不用 CLI）"""
    print("=" * 50)
    print("示例 4: 编程式使用")
    print("=" * 50)

    from auto_epub import EpubTranslator, create_epub_agent

    # 创建自定义 Agent
    agent = create_epub_agent("zh")

    # 创建 translator
    translator = EpubTranslator(agent=agent, cache_enabled=True)

    # 翻译
    output = await translator.translate_epub(
        input_file="book.epub",
        target_language="zh",
        translate_images=False,
        translate_toc=True,
        resume=True,
    )

    print(f"✓ 翻译完成: {output}")


async def example_batch_translation():
    """示例 5: 批量翻译多个文件"""
    print("=" * 50)
    print("示例 5: 批量翻译")
    print("=" * 50)

    files = ["book1.epub", "book2.epub", "book3.epub"]

    translator = create_translator(target_language="zh", cache_enabled=True)

    for i, file_path in enumerate(files, 1):
        print(f"\n[{i}/{len(files)}] 翻译: {file_path}")

        try:
            output = await translator.translate_epub(
                input_file=file_path,
                target_language="zh",
                translate_images=False,
                translate_toc=True,
                resume=True,
            )
            print(f"✓ 完成: {output}")

        except Exception as e:
            print(f"✗ 失败: {e}")
            continue

    print("\n✓ 批量翻译完成！")


async def main():
    """运行示例"""

    # 选择要运行的示例
    examples = {
        "1": ("基础翻译", example_basic_translation),
        "2": ("翻译图片", example_with_images),
        "3": ("自定义设置", example_custom_settings),
        "4": ("编程式使用", example_programmatic_usage),
        "5": ("批量翻译", example_batch_translation),
    }

    print("\n📚 EPUB Translator 使用示例\n")
    for key, (name, _) in examples.items():
        print(f"{key}. {name}")

    choice = input("\n请选择示例 (1-5): ").strip()

    if choice in examples:
        _, func = examples[choice]
        await func()
    else:
        print("无效选择")


if __name__ == "__main__":
    # 运行示例
    asyncio.run(main())

    # 或者直接运行某个示例
    # asyncio.run(example_basic_translation())
