import hashlib
import io
import json

import pytest
from PIL import Image, ImageDraw

from wtf_ingestion.adapters import ocr


def image_bytes(blank=False):
    image = Image.new("RGB", (400, 100), "white")
    if not blank:
        ImageDraw.Draw(image).text((10, 10), "Example earnings report 2026", fill="black")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def tsv(text, score=95):
    header = "level\tblock_num\tpar_num\tline_num\tconf\ttext\n"
    return (header + "".join(f"5\t1\t1\t1\t{score}\t{word}\n" for word in text.split())).encode()


def test_dual_layout_cached_and_integrity_checked(tmp_path, monkeypatch):
    monkeypatch.setattr(ocr, "engine_context", lambda: {"version": "fixture"})
    calls = []
    monkeypatch.setattr(
        ocr, "_run", lambda args: calls.append(args) or tsv("Historical report with four words")
    )
    body = image_bytes()
    result = ocr.recognize_image(body, tmp_path)
    assert result["readable"] and len(result["layouts"]) == 2
    assert ocr.recognize_image(body, tmp_path) == result and len(calls) == 2
    cache = next(tmp_path.glob("*.json"))
    result["layouts"][0]["text"] = "changed"
    cache.write_text(json.dumps(result))
    with pytest.raises(ocr.OcrQualityError, match="checksum"):
        ocr.recognize_image(body, tmp_path)


def test_empty_recognition_is_not_blank_page(tmp_path, monkeypatch):
    monkeypatch.setattr(ocr, "engine_context", lambda: {"version": "fixture"})
    monkeypatch.setattr(ocr, "_run", lambda args, **kwargs: tsv(""))
    assert not ocr.recognize_image(image_bytes(), tmp_path)["readable"]
    result = ocr.recognize_image(image_bytes(blank=True), tmp_path)
    assert result["blank"] and result["readable"]


def test_each_recognition_pass_keeps_the_worker_alive(tmp_path, monkeypatch):
    monkeypatch.setattr(ocr, "engine_context", lambda: {"version": "fixture"})
    monkeypatch.setattr(ocr, "_run", lambda args: tsv("Readable retained historical report"))
    ticks = []
    result = ocr.recognize_image(image_bytes(), tmp_path, lambda: ticks.append(1))
    assert result["readable"] and len(ticks) == 2


def test_poor_recognition_tries_language_fallback_but_does_not_pass(tmp_path, monkeypatch):
    monkeypatch.setattr(ocr, "engine_context", lambda: {"version": "fixture"})
    monkeypatch.setattr(
        ocr, "_run", lambda args, **kwargs: tsv("Unreliable words in low quality", 40)
    )
    result = ocr.recognize_image(image_bytes(), tmp_path)
    assert len(result["layouts"]) >= 3 and not result["readable"]


def test_word_output_and_image_bounds(tmp_path, monkeypatch):
    monkeypatch.setattr(ocr, "engine_context", lambda: {"version": "fixture"})
    with pytest.raises(ocr.OcrQualityError):
        ocr.parse_tsv(tsv("invalid", 101))
    with pytest.raises(ocr.OcrQualityError):
        ocr.recognize_image(b"not an image", tmp_path)
    text = ocr.parse_tsv(tsv("valid words"))["text"]
    assert (
        hashlib.sha256(text.encode()).hexdigest()
        == ocr.parse_tsv(tsv("valid words"))["text_sha256"]
    )


def test_literal_quotes_do_not_merge_word_rows():
    result = ocr.parse_tsv(tsv('Quoted " word with literal punctuation'))
    assert result["words"] == 6 and '"' in result["text"]


def test_old_quote_artifacts_are_not_reused():
    text = "quoted\t5\t1\trow"
    layout = {"text": text, "text_sha256": hashlib.sha256(text.encode()).hexdigest()}
    assert ocr.checked_layouts([layout]) == []
    layout["text_sha256"] = "changed"
    with pytest.raises(ocr.OcrQualityError, match="checksum"):
        ocr.checked_layouts([layout])


def test_decorative_page_is_blank_but_text_is_not():
    image = Image.new("RGB", (400, 100), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((20, 0, 379, 99), fill="red")
    assert ocr.blank_image(image)
    draw.text((50, 30), "Release October 29", fill="white")
    assert not ocr.blank_image(image)
