"""Tests for language support."""
import pytest


class TestLanguage:
    def test_lang_en_returns_english(self, game):
        state = game.start(seed="lang_en1", lang="en")
        # Neow event — card names should be English
        player = state.get("player", {})
        deck = player.get("deck", [])
        assert any("Strike" in str(c.get("name", "")) for c in deck), \
            f"Expected English card names, got: {[c['name'] for c in deck[:3]]}"

    def test_lang_zh_returns_chinese(self, game):
        state = game.start(seed="lang_zh1", lang="zh")
        player = state.get("player", {})
        deck = player.get("deck", [])
        # Check for Chinese characters (unicode > 0x4e00)
        names = [c.get("name", "") for c in deck]
        has_chinese = any(any(ord(ch) > 0x4e00 for ch in name) for name in names)
        assert has_chinese, f"Expected Chinese card names, got: {names[:3]}"

    def test_default_lang_is_english(self, game):
        """Without lang param, should default to English."""
        state = game.send({"cmd": "start_run", "character": "Ironclad", "seed": "lang_def1"})
        player = state.get("player", {})
        deck = player.get("deck", [])
        names = [c.get("name", "") for c in deck]
        # Should be English (no Chinese characters)
        has_chinese = any(any(ord(ch) > 0x4e00 for ch in name) for name in names)
        assert not has_chinese, f"Expected English by default, got: {names[:3]}"
