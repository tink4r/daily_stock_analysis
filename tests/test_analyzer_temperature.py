# -*- coding: utf-8 -*-
import unittest
import sys
from types import SimpleNamespace

sys.modules.setdefault("json_repair", SimpleNamespace(repair_json=lambda value: value))

from src.analyzer import resolve_generation_temperature


class TestResolveGenerationTemperature(unittest.TestCase):
    def setUp(self):
        self.config = SimpleNamespace(
            gemini_temperature=0.7,
            openai_temperature=0.1,
        )

    def test_openai_uses_openai_temperature(self):
        self.assertEqual(resolve_generation_temperature(self.config, use_openai=True), 0.1)

    def test_gemini_uses_gemini_temperature(self):
        self.assertEqual(resolve_generation_temperature(self.config, use_openai=False), 0.7)


if __name__ == "__main__":
    unittest.main()
