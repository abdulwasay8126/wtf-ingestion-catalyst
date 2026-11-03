from __future__ import annotations

from wtf_ingestion.logging import redact_event


def test_redacts_sensitive_keys_and_bearer_values() -> None:
    event = {
        "authorization": "Bearer abc.def",
        "safe": "Bearer hidden-token",
        "nested": {"api_key": "secret", "status": "ok"},
    }
    redacted = redact_event(None, "info", event)
    assert redacted["authorization"] == "[REDACTED]"
    assert redacted["safe"] == "Bearer [REDACTED]"
    assert redacted["nested"]["api_key"] == "[REDACTED]"
    assert redacted["nested"]["status"] == "ok"
