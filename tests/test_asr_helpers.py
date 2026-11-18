"""Юнит-тесты помощников распознавания: точка разреза, чистка логов.

Модель здесь не грузится: проверяются только чистые функции asr.py.
"""

from __future__ import annotations

import types
import unittest

import numpy as np

from app.infra.asr import (
    CUT_FRAME_SAMPLES,
    CUT_SEARCH_SAMPLES,
    SAMPLE_RATE,
    CHUNK_SAMPLES,
    Recognizer,
    _scrub,
)

_cut_point = Recognizer._cut_point
_extract_text = Recognizer._extract_text
_resample = Recognizer._resample


class ScrubTests(unittest.TestCase):
    def test_token_url_is_replaced(self):
        scrubbed = _scrub("ошибка чтения https://api.telegram.org/file/bot1:SECRET/x.ogg")
        self.assertNotIn("SECRET", scrubbed)
        self.assertNotIn("https://", scrubbed)
        self.assertIn("<url>", scrubbed)

    def test_plain_text_untouched(self):
        self.assertEqual(_scrub("обычное сообщение без ссылок"), "обычное сообщение без ссылок")


class CutPointTests(unittest.TestCase):
    def test_short_buffer_is_returned_whole(self):
        buf = np.ones(100_000, dtype=np.float32)
        self.assertEqual(_cut_point(buf, 0, CHUNK_SAMPLES), len(buf))

    def test_loud_tail_cuts_at_limit(self):
        """Ровный звук режется точно по границе: берём самый поздний кадр."""
        buf = np.ones(CHUNK_SAMPLES + CUT_SEARCH_SAMPLES, dtype=np.float32)
        self.assertEqual(_cut_point(buf, 0, CHUNK_SAMPLES), CHUNK_SAMPLES)

    def test_quietest_frame_is_chosen(self):
        buf = np.ones(CHUNK_SAMPLES + CUT_SEARCH_SAMPLES, dtype=np.float32)
        low = CHUNK_SAMPLES - CUT_SEARCH_SAMPLES
        quiet_frame = 5
        start = low + quiet_frame * CUT_FRAME_SAMPLES
        # Тишина обрамлена речью, чтобы точка реза была внутри окна.
        buf[start : start + CUT_FRAME_SAMPLES] = 0.0
        cut = _cut_point(buf, 0, CHUNK_SAMPLES)
        self.assertEqual(cut, start + CUT_FRAME_SAMPLES)
        self.assertLessEqual(cut, CHUNK_SAMPLES)

    def test_cut_never_exceeds_limit(self):
        rng = np.random.default_rng(42)
        buf = rng.standard_normal(CHUNK_SAMPLES + CUT_SEARCH_SAMPLES).astype(np.float32)
        cut = _cut_point(buf, 0, CHUNK_SAMPLES)
        self.assertGreater(cut, 0)
        self.assertLessEqual(cut, CHUNK_SAMPLES)


class ResampleTests(unittest.TestCase):
    def test_same_rate_returns_same_buffer(self):
        buf = np.ones(SAMPLE_RATE, dtype=np.float32)
        self.assertIs(_resample(buf, SAMPLE_RATE, SAMPLE_RATE), buf)

    def test_downsample_length(self):
        buf = np.zeros(32_000, dtype=np.float32)
        result = _resample(buf, 32_000, SAMPLE_RATE)
        self.assertEqual(len(result), SAMPLE_RATE)

    def test_upsample_length(self):
        buf = np.zeros(SAMPLE_RATE, dtype=np.float32)
        result = _resample(buf, SAMPLE_RATE, 48_000)
        self.assertEqual(len(result), 48_000)

    def test_empty_buffer(self):
        result = _resample(np.zeros(0, dtype=np.float32), 44_100, SAMPLE_RATE)
        self.assertEqual(len(result), 0)


class ExtractTextTests(unittest.TestCase):
    def test_plain_string(self):
        self.assertEqual(_extract_text("  привет  "), "привет")

    def test_blank_string_is_none(self):
        self.assertIsNone(_extract_text("   "))

    def test_none_is_none(self):
        self.assertIsNone(_extract_text(None))

    def test_object_with_text(self):
        result = types.SimpleNamespace(text="  привет  ")
        self.assertEqual(_extract_text(result), "привет")

    def test_unexpected_shape_is_none(self):
        self.assertIsNone(_extract_text(types.SimpleNamespace(text=42)))


class ModelForTests(unittest.TestCase):
    def setUp(self):
        self.recognizer = Recognizer("gigaam", "whisper-base")

    def test_russian_by_default(self):
        self.assertEqual(self.recognizer._model_for(""), ("gigaam", False))
        self.assertEqual(self.recognizer._model_for("ru"), ("gigaam", False))

    def test_english_asks_for_language(self):
        self.assertEqual(self.recognizer._model_for("en"), ("whisper-base", True))
        self.assertEqual(self.recognizer._model_for("EN"), ("whisper-base", True))


if __name__ == "__main__":
    unittest.main()
