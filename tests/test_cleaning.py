"""Тесты для пайплайна очистки текста CryptoPanic."""
from scripts.cryptopanic.cleaning import clean_text_pipeline


def test_clean_text_pipeline_removes_blacklist_and_duplicates():
    raw = (
        "Teaser line we do not need:\n"
        "Read more: please subscribe!\n"
        "First meaningful sentence ends here.\n"
        "Second sentence is also quite meaningful and ends properly.\n"
        "Read more: please subscribe!\n"
        "Second sentence is also quite meaningful and ends properly.\n"
        "Third sentence provides additional context for the reader.\n"
    )

    cleaned = clean_text_pipeline(raw)

    lines = cleaned.split("\n")
    assert "read more" not in cleaned.lower()
    assert len(lines) == len(set(lines))
    assert lines[0] == "Teaser line we do not need:"
    assert lines[1] == "First meaningful sentence ends here."
    assert "Third sentence" not in cleaned  # слишком короткая строка удаляется шагом 8
