from __future__ import annotations

import pytest

from wtf_ingestion.adapters.base import PayloadError, SourcePermissionError
from wtf_ingestion.ocr_trial import indexed_images, sample_pages

ROOT = "https://www.sec.gov/Archives/edgar/data/19617/000001961726000241/"
INDEX = b"""<table summary="Document Format Files"><tr><td>2</td><td>Page</td>
<td><a href="page.jpg">page.jpg</a></td><td>GRAPHIC</td></tr></table>"""


def test_ocr_only_accepts_explicitly_indexed_same_filing_images() -> None:
    assert indexed_images(
        INDEX, b'<img src="page.jpg">', ROOT + "index.html", ROOT + "doc.htm"
    ) == [ROOT + "page.jpg"]


@pytest.mark.parametrize(
    "image", ["../page.jpg", "https://evil.invalid/page.jpg", "other.jpg", "page.jpg?secret=x"]
)
def test_ocr_cannot_leave_indexed_document_directory(image: str) -> None:
    with pytest.raises(SourcePermissionError):
        indexed_images(
            INDEX, f'<img src="{image}">'.encode(), ROOT + "index.html", ROOT + "doc.htm"
        )


@pytest.mark.parametrize("body", [b"", b'<img src="page.jpg"><img src="page.jpg">'])
def test_ocr_missing_or_duplicate_pages_fail(body: bytes) -> None:
    with pytest.raises(PayloadError):
        indexed_images(INDEX, body, ROOT + "index.html", ROOT + "doc.htm")


def test_ocr_samples_bound_work_and_include_start_middle_end() -> None:
    assert sample_pages(2, 3) == [0, 1]
    assert sample_pages(139, 3) == [0, 69, 138]
    assert sample_pages(25, 3) == [0, 12, 24]
    assert sample_pages(139, 1) == [0]
    with pytest.raises(ValueError):
        sample_pages(151, 3)
