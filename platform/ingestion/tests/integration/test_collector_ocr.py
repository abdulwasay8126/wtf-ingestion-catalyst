import hashlib
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from wtf_ingestion.adapters.base import PayloadError
from wtf_ingestion.adapters.ocr import OcrQualityError
from wtf_ingestion.collectors import SecCollector
from wtf_ingestion.config import Settings


def collector_proof(db_session, tmp_path, first, second, *, readable=True):
    collector = SecCollector(Settings(raw_dir=tmp_path), db_session)
    body = b"<html><body><img src='page.png'></body></html>"
    digest = hashlib.sha256(body).hexdigest()
    url = "https://www.sec.gov/Archives/edgar/data/123/000000012326000001/a.htm"
    document = {
        "body_sha256": digest,
        "ticker": "TEST",
        "exchange": "NASDAQ",
        "company_name": "Sanitized fixture",
        "url": url,
        "filing": {
            "cik": "0000000123",
            "accession": "0000000123-26-000001",
            "filed_on": "2026-10-01",
            "form": "8-K",
            "primary_document": "a.htm",
        },
    }
    image = b"Sanitized retained image fixture"
    image_hash = hashlib.sha256(image).hexdigest()
    collector.service._store_raw(image_hash, image)
    proof = {
        "document_sha256": digest,
        "document_url": document["url"],
        "pages": [
            {
                "number": 1,
                "image_sha256": image_hash,
                "recognition": {
                    "readable": readable,
                    "blank": False,
                    "layouts": [
                        {"text": first, "mean_confidence": 95},
                        {"text": second, "mean_confidence": 94},
                    ],
                },
            }
        ],
    }
    raw = json.dumps(proof).encode()
    proof_hash = hashlib.sha256(raw).hexdigest()
    collector.service._store_raw(proof_hash, raw)
    document["ocr_sha256"] = proof_hash
    return collector, body, document


def test_readable_complete_ocr_can_validate_non_event_document(db_session, tmp_path):
    collector, body, document = collector_proof(
        db_session,
        tmp_path,
        "Historical quarterly report, published in July.",
        "Historical quarterly report, published in July.",
    )
    assert collector._parse_document(body, document, datetime(2026, 10, 2, tzinfo=UTC)) == []


@pytest.mark.parametrize("fault", ["poor", "conflict", "event", "corrupt"])
def test_ocr_does_not_publish_uncertain_or_unreviewed_facts(db_session, tmp_path, fault):
    first = "Company will release third quarter 2026 earnings on October 29, 2026."
    second = first.replace("29", "30") if fault == "conflict" else first
    collector, body, document = collector_proof(
        db_session, tmp_path, first, second, readable=fault != "poor"
    )
    if fault == "corrupt":
        document["body_sha256"] = "0" * 64
    with pytest.raises(PayloadError):
        collector._parse_document(body, document, datetime(2026, 10, 2, tzinfo=UTC))
    if fault != "corrupt":
        with pytest.raises(OcrQualityError):
            collector._parse_document(body, document, datetime(2026, 10, 2, tzinfo=UTC))


def pdf_proof(db_session, tmp_path, monkeypatch):
    collector, _, document = collector_proof(db_session, tmp_path, "Past report", "Past report")
    body = b"%PDF-1.7\nSanitized PDF fixture"
    document["url"] = document["url"].replace("a.htm", "a.pdf")
    document["body_sha256"] = hashlib.sha256(body).hexdigest()
    proof = json.loads(collector._body({"body_sha256": document["ocr_sha256"]}))
    proof["document_url"] = document["url"]
    proof["document_sha256"] = document["body_sha256"]
    proof["pages"][0]["recognition"]["context"] = {"version": "fixture"}
    monkeypatch.setattr("wtf_ingestion.collectors.engine_context", lambda: {"version": "fixture"})
    return collector, body, document, proof


def store_pdf_proof(collector, document, proof):
    raw = json.dumps(proof).encode()
    digest = hashlib.sha256(raw).hexdigest()
    collector.service._store_raw(digest, raw)
    collector.pdf_ocr_proofs[(document["url"], document["body_sha256"])] = digest
    return digest


def test_unchanged_pdf_reuses_complete_model_bound_proof(db_session, tmp_path, monkeypatch):
    collector, body, document, proof = pdf_proof(db_session, tmp_path, monkeypatch)
    digest = store_pdf_proof(collector, document, proof)
    render = Mock(side_effect=AssertionError("Unchanged PDF must not be rendered"))
    monkeypatch.setattr("wtf_ingestion.collectors.render_pdf", render)
    assert collector._recognize_document(Mock(), uuid4(), body, document, b"", "") == digest
    assert collector.ocr_evidence[document["url"]] == digest
    render.assert_not_called()


@pytest.mark.parametrize("fault", ["model", "parent", "incomplete", "force"])
def test_pdf_proof_changes_require_recognition(db_session, tmp_path, monkeypatch, fault):
    collector, body, document, proof = pdf_proof(db_session, tmp_path, monkeypatch)
    if fault == "model":
        proof["pages"][0]["recognition"]["context"] = {"version": "older"}
    elif fault == "parent":
        proof["document_sha256"] = "0" * 64
    elif fault == "incomplete":
        proof["pages"][0]["number"] = 2
    else:
        collector.settings.sec_force_recheck = True
    store_pdf_proof(collector, document, proof)
    render = Mock(side_effect=OcrQualityError("Recognition required for changed proof"))
    monkeypatch.setattr("wtf_ingestion.collectors.render_pdf", render)
    with pytest.raises(OcrQualityError, match="Recognition required"):
        collector._recognize_document(Mock(), uuid4(), body, document, b"", "")
    render.assert_called_once()


def test_corrupt_pdf_proof_fails_before_reuse(db_session, tmp_path, monkeypatch):
    collector, body, document, proof = pdf_proof(db_session, tmp_path, monkeypatch)
    digest = store_pdf_proof(collector, document, proof)
    collector.verified_bodies.clear()
    collector.verified_body_bytes = 0
    (tmp_path / f"{digest}.bin").write_bytes(b"corrupted fixture")
    with pytest.raises(PayloadError):
        collector._recognize_document(Mock(), uuid4(), body, document, b"", "")


def test_corrupt_retained_pdf_page_cannot_hide_behind_parse_cache(
    db_session, tmp_path, monkeypatch
):
    collector, body, document, proof = pdf_proof(db_session, tmp_path, monkeypatch)
    store_pdf_proof(collector, document, proof)
    image_hash = proof["pages"][0]["image_sha256"]
    (tmp_path / f"{image_hash}.bin").write_bytes(b"corrupted derived page")
    collector.verified_bodies.clear()
    collector.verified_body_bytes = 0
    with pytest.raises(PayloadError):
        collector._recognize_document(Mock(), uuid4(), body, document, b"", "")


def test_combined_validation_reports_progress_after_company_scan(db_session, tmp_path, monkeypatch):
    collector, body, document = collector_proof(db_session, tmp_path, "Past report", "Past report")
    digest = hashlib.sha256(body).hexdigest()
    collector.service._store_raw(digest, body)
    collector.run_id = uuid4()
    collector.progress_state = (1, 1, "TEST")
    collector.progress = Mock()
    monkeypatch.setattr(collector, "_parse_document", lambda *args: [])
    monkeypatch.setattr(
        "wtf_ingestion.collectors.project_next_earnings",
        lambda *args, **kwargs: SimpleNamespace(candidates=[]),
    )
    payload = {
        "complete": True,
        "companies_scanned": 1,
        "estimates_enabled": True,
        "projection_horizon_days": 90,
        "documents": [document],
        "projection_inputs": [{**document, "cik": document["filing"]["cik"]}],
    }
    assert collector._parse(json.dumps(payload).encode(), datetime(2026, 10, 3, tzinfo=UTC)) == []
    assert collector.progress.call_count == 2
    collector.progress.assert_any_call(collector.run_id, 1, 1, "Validate files")
    collector.progress.assert_any_call(collector.run_id, 1, 1, "Validate estimates")
