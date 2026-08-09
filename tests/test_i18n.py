import pytest

from csbox.locales import MissingLocaleKey, load_locale


def test_zh_cn_resource_loads_brand_and_chinese_copy() -> None:
    translator = load_locale("zh_CN")

    assert translator("brand.name") == "CSBox"
    assert translator("brand.subtitle") == "计算机实验与项目交付工具"
    assert translator("home.demo.tag") == "[DEMO]"
    assert translator("common.yes").startswith("●")
    assert translator("common.no").startswith("○")


def test_translator_formats_known_placeholders() -> None:
    translator = load_locale("zh_CN")

    assert translator("home.demo.count", count=3) == "3 条演示记录"


def test_translator_rejects_missing_keys() -> None:
    translator = load_locale("zh_CN")

    with pytest.raises(MissingLocaleKey, match="missing.key"):
        translator("missing.key")
