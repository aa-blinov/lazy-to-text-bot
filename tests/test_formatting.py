"""Юнит-тесты нарезки и экранирования текста для Telegram.

Регрессии здесь уже случались: кусок, разрезанный посреди `&amp;`, и
лимит, превышенный уже после экранирования. Оба сценария закрыты
свойством «склейка равна исходнику» и проверкой границ сущностей.
"""

from __future__ import annotations

import html
import unittest

from app.bot.formatting import (
    MAX_MESSAGE_CHARS,
    escape,
    split_escaped,
    split_text,
    transcript_blocks,
)

TELEGRAM_LIMIT = 4096


class EscapeTests(unittest.TestCase):
    def test_html_is_escaped(self):
        self.assertEqual(escape("<b>&"), "&lt;b&gt;&amp;")

    def test_empty_values(self):
        self.assertEqual(escape(None), "")
        self.assertEqual(escape(0), "")

    def test_quotes(self):
        self.assertEqual(escape('"x"'), "&quot;x&quot;")


class SplitEscapedTests(unittest.TestCase):
    def test_short_text_is_single_chunk(self):
        self.assertEqual(split_escaped("привет"), ["привет"])

    def test_empty_text(self):
        self.assertEqual(split_escaped(""), [""])

    def test_roundtrip_with_newlines_and_spaces(self):
        text = "строка с текстом\n" * 800
        chunks = split_escaped(text)
        self.assertGreater(len(chunks), 1)
        self.assertEqual("".join(chunks), text)

    def test_chunk_limit(self):
        text = ("слово " * 5000).strip()
        chunks = split_escaped(text)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), MAX_MESSAGE_CHARS)

    def test_hard_cut_without_separators(self):
        text = "а" * (MAX_MESSAGE_CHARS * 2 + 5)
        chunks = split_escaped(text)
        self.assertGreater(len(chunks), 1)
        self.assertEqual("".join(chunks), text)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), MAX_MESSAGE_CHARS)

    def test_entity_is_never_broken(self):
        """Рез не рвёт `&amp;`: точки с запятой хватает в каждом куске."""
        text = html.escape("строка с <тегом> и & сущностью " * 200)
        chunks = split_escaped(text)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), MAX_MESSAGE_CHARS + 8)
            if "&" in chunk:
                self.assertGreater(chunk.rfind(";"), chunk.rfind("&"))

    def test_small_limit_moves_cut_past_semicolon(self):
        text = "a" * 45 + "&amp;" * 6
        chunks = split_escaped(text, limit=50)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            # Рез может отъехать за лимит не дальше хвоста сущности.
            self.assertLessEqual(len(chunk), 58)
            if "&" in chunk:
                self.assertGreater(chunk.rfind(";"), chunk.rfind("&"))
        self.assertEqual("".join(chunks), text)


class SplitTextTests(unittest.TestCase):
    def test_roundtrip(self):
        text = "первая\nвторая\n\nтретья строка подлиннее\n"
        self.assertEqual("".join(split_text(text)), text)

    def test_long_lines_are_cut(self):
        text = "x" * (MAX_MESSAGE_CHARS * 2)
        chunks = split_text(text)
        self.assertGreater(len(chunks), 1)
        self.assertEqual("".join(chunks), text)

    def test_empty(self):
        self.assertEqual(split_text(""), [""])


class TranscriptBlocksTests(unittest.TestCase):
    def test_each_block_is_pre_and_under_limit(self):
        blocks = transcript_blocks("строка с <тегом> " * 600)
        self.assertGreater(len(blocks), 1)
        for block in blocks:
            self.assertLess(len(block), TELEGRAM_LIMIT)
            self.assertTrue(block.startswith("<pre>"))
            self.assertTrue(block.endswith("</pre>"))

    def test_escaped_content_survives(self):
        text = "текст с <тегом>, & и \"кавычками\""
        blocks = transcript_blocks(text)
        joined = "".join(block[len("<pre>") : -len("</pre>")] for block in blocks)
        self.assertEqual(joined, html.escape(text))

    def test_short_text_single_block(self):
        blocks = transcript_blocks("привет")
        self.assertEqual(blocks, ["<pre>привет</pre>"])


if __name__ == "__main__":
    unittest.main()
