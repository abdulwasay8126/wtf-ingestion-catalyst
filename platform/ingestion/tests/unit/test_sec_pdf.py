from __future__ import annotations

import hashlib
import io
import json
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest
from pypdf import PdfWriter
from pypdf.generic import (
    DictionaryObject,
    EncodedStreamObject,
    NameObject,
    NumberObject,
    StreamObject,
)
from sqlalchemy import select

from wtf_ingestion.adapters.base import FetchPolicy, PayloadError, SafeHttpClient
from wtf_ingestion.adapters.pdf_text import extract_pdf_text
from wtf_ingestion.adapters.sec_native import Filing, parse_document_index, parse_exhibit
from wtf_ingestion.collectors import SecCollector
from wtf_ingestion.config import Settings
from wtf_ingestion.persistence.models import EventCurrent, RawObject

ANNOUNCEMENT = "Example Inc. will release financial results on October 29, 2026 at 4:30 PM ET."
FILING = Filing("0000320193", "0000320193-26-000099", date(2026, 9, 28), "8-K", "announcement.htm")


def pdf_bytes(
    pages: list[str],
    *,
    encrypted: bool = False,
    compressed: bool = False,
    large_image: bool = False,
) -> bytes:
    """Synthetic text-layer PDF fixture; no real-company facts or authoring dependency."""
    writer = PdfWriter()
    for text in pages:
        page = writer.add_blank_page(width=612, height=792)
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
        )
        stream = StreamObject()
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream.set_data(f"BT /F1 12 Tf 40 750 Td ({escaped}) Tj ET".encode("ascii"))
        page[NameObject("/Contents")] = stream.flate_encode() if compressed else stream
        if large_image:
            # Text extraction never interprets JPEG pixels. Exercise a large non-text
            # resource, distinct from oversized decompressed text/content streams.
            image = EncodedStreamObject()
            image[NameObject("/Type")] = NameObject("/XObject")
            image[NameObject("/Subtype")] = NameObject("/Image")
            image[NameObject("/Filter")] = NameObject("/DCTDecode")
            image[NameObject("/Width")] = NumberObject(1)
            image[NameObject("/Height")] = NumberObject(1)
            image._data = b"\xff\xd8" + b"0" * 2_100_000 + b"\xff\xd9"
            page["/Resources"][NameObject("/XObject")] = DictionaryObject(
                {NameObject("/Im1"): image}
            )
            stream.set_data(stream.get_data() + b" /Im1 Do")
            page[NameObject("/Contents")] = stream
    if encrypted:
        writer.encrypt("test-password")
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def test_pdf_explicit_announcement_and_provenance() -> None:
    raw = pdf_bytes([ANNOUNCEMENT], compressed=True)
    assert ANNOUNCEMENT in extract_pdf_text(raw)
    events = parse_exhibit(
        raw,
        filing=FILING,
        url=FILING.root_url + "release.pdf",
        ticker="TEST",
        exchange="NASDAQ",
        company_name="Example Inc.",
        observed=datetime(2026, 10, 1, tzinfo=UTC),
    )
    assert len(events) == 1
    assert events[0].event_date == date(2026, 10, 29)
    assert events[0].scheduled_start is not None
    assert events[0].scheduled_start.hour == 20  # 4:30 PM Eastern, normalized to UTC.
    assert events[0].metadata["document_format"] == "pdf_text_layer"
    assert str(events[0].source_url).endswith(".pdf")
    assert events[0].parser_version == "sec-native-v15"


def test_index_includes_pdf_without_silently_skipping_html(fixture_root: Path) -> None:
    raw = (fixture_root / "sec/document_index.html").read_bytes()
    extra = (
        b'<tr><td>3</td><td>Press release</td><td><a href="'
        + (FILING.root_url + "release.pdf").encode()
        + b'">release.pdf</a></td><td>EX-99.1</td></tr>'
    )
    urls = parse_document_index(raw.replace(b"</table>", extra + b"</table>"), FILING)
    assert len(urls) == 3
    assert urls[-1] == FILING.root_url + "release.pdf"


@pytest.mark.parametrize("fault", ["empty", "malformed", "truncated", "encrypted", "partial"])
def test_pdf_input_failures_are_explicit(fault: str) -> None:
    raw = pdf_bytes([ANNOUNCEMENT])
    if fault == "empty":
        raw = pdf_bytes([""])
    elif fault == "malformed":
        raw = b"%PDF-1.7\nthis is not a PDF"
    elif fault == "truncated":
        raw = raw[: len(raw) // 2]
    elif fault == "encrypted":
        raw = pdf_bytes([ANNOUNCEMENT], encrypted=True)
    else:
        raw = pdf_bytes([ANNOUNCEMENT, ""])
    with pytest.raises(PayloadError):
        extract_pdf_text(raw)


def test_pdf_decompression_is_bounded() -> None:
    raw = pdf_bytes(["A" * 2_000_001], compressed=True)
    assert len(raw) < 10_000
    with pytest.raises(PayloadError):
        extract_pdf_text(raw)


def test_garbled_pdf_text_cannot_be_treated_as_valid_empty() -> None:
    from wtf_ingestion.adapters.base import UnreadableDocumentError

    raw = pdf_bytes(["Supplemental report " + "\x01Shifted text layer " * 50])
    with pytest.raises(UnreadableDocumentError):
        extract_pdf_text(raw)


def test_pdf_large_nontext_resource_does_not_hide_text() -> None:
    assert ANNOUNCEMENT in extract_pdf_text(pdf_bytes([ANNOUNCEMENT], large_image=True))


def test_pdf_page_count_is_bounded() -> None:
    with pytest.raises(PayloadError):
        extract_pdf_text(pdf_bytes([ANNOUNCEMENT] * 101))


def test_pdf_result_filing_is_not_a_future_announcement() -> None:
    events = parse_exhibit(
        pdf_bytes(["Example Inc. reported its financial results on September 29, 2026."]),
        filing=FILING,
        url=FILING.root_url + "release.pdf",
        ticker="TEST",
        exchange="NASDAQ",
        company_name="Example Inc.",
        observed=datetime(2026, 10, 1, tzinfo=UTC),
    )
    assert events == []


def test_pdf_missing_content_page_is_not_ignored() -> None:
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    page[NameObject("/Rotate")] = NumberObject(0)
    output = io.BytesIO()
    writer.write(output)
    with pytest.raises(PayloadError):
        extract_pdf_text(output.getvalue())


@pytest.mark.parametrize("fault", ["missing", "tampered", "unsafe"])
def test_raw_references_fail_closed(db_session, tmp_path: Path, fault: str) -> None:  # type: ignore[no-untyped-def]
    collector = SecCollector(Settings(raw_dir=tmp_path), db_session)
    body = b"The actual retained raw evidence."
    digest = hashlib.sha256(body).hexdigest()
    if fault == "tampered":
        (tmp_path / f"{digest}.bin").write_bytes(b"Wrong evidence")
    elif fault == "unsafe":
        digest = "../escape"
    with pytest.raises(PayloadError):
        collector._body({"body_sha256": digest})


def test_native_pdf_collector_uses_raw_refs(db_session, fixture_root: Path, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    raw_pdf = pdf_bytes([ANNOUNCEMENT])

    def respond(request: httpx.Request) -> httpx.Response:
        if "submissions/" in str(request.url):
            cik = request.url.path.rsplit("/", 1)[-1].removeprefix("CIK").removesuffix(".json")
            payload = json.loads((fixture_root / "sec/submissions_native.json").read_bytes())
            payload["cik"] = cik
            if cik != "0000320193":
                for key in payload["filings"]["recent"]:
                    payload["filings"]["recent"][key] = []
            return httpx.Response(200, json=payload)
        if request.url.path.endswith("-index.html"):
            raw = (fixture_root / "sec/document_index.html").read_bytes()
            return httpx.Response(
                200,
                headers={"content-type": "text/html"},
                content=raw.replace(b"ex99-1.htm", b"ex99-1.pdf"),
            )
        if request.url.path.endswith(".pdf"):
            return httpx.Response(200, headers={"content-type": "application/pdf"}, content=raw_pdf)
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            content=b"<p>Refer to the supplied earnings release exhibit.</p>",
        )

    client = SafeHttpClient(
        FetchPolicy(
            frozenset({"data.sec.gov", "www.sec.gov"}), "Offline test", min_interval_seconds=0
        ),
        transport=httpx.MockTransport(respond),
    )
    settings = Settings(raw_dir=tmp_path / "raw", output_dir=tmp_path / "out")
    try:
        result = SecCollector(settings, db_session, client).run(evidence_mode="fixture")
        assert client.cache == {}  # Raw files, not a full universe's bytes held by the HTTP pool.
    finally:
        client.close()
    assert result.state == "HEALTHY" and result.inserted == 1
    event = db_session.scalar(select(EventCurrent))
    assert event is not None and str(event.source_url).endswith(".pdf")
    bundle = db_session.scalar(
        select(RawObject).where(RawObject.source_url == "https://data.sec.gov/submissions/")
    )
    assert bundle is not None
    payload = json.loads(Path(bundle.storage_path).read_bytes())
    assert payload["bundle_format"] == "raw_sha256_refs_v1"
    assert all("body_sha256" in item and "body_base64" not in item for item in payload["documents"])
