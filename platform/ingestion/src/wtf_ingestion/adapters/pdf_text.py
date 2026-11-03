from __future__ import annotations

import io
import logging
import warnings

from pypdf import PdfReader, apply_configuration

from wtf_ingestion.adapters.base import PayloadError, UnreadableDocumentError

MAX_PDF_BYTES = 10_000_000
MAX_PAGES = 100
MAX_STREAM_BYTES = 2_000_000
MAX_TOTAL_STREAM_BYTES = 20_000_000
MAX_TEXT_CHARS = 1_000_000


class _WarningFlag(logging.Handler):
    """Do not leak PDF diagnostics or accept an extraction that dropped unsupported text."""

    seen = False

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno >= logging.WARNING:
            self.seen = True


def extract_pdf_text(raw: bytes) -> str:
    """Text-layer extraction only; no OCR, password attempts, or silent parser repair."""
    if len(raw) > MAX_PDF_BYTES or not raw.startswith(b"%PDF-"):
        raise PayloadError("SEC PDF has an invalid signature or exceeds the size limit")
    logger = logging.getLogger("pypdf")
    warning_flag = _WarningFlag()
    previous_propagate = logger.propagate
    logger.addHandler(warning_flag)
    logger.propagate = False
    try:
        with (
            warnings.catch_warnings(record=True) as notices,
            apply_configuration(
                # JPEG resources can be larger than a text stream. Their encoded bytes are
                # already bounded by the full PDF limit; decoded text streams stay at 2 MB.
                maximum_declared_stream_length=MAX_PDF_BYTES,
                array_based_stream_maximum_output_length=MAX_STREAM_BYTES,
                zlib_maximum_output_length=MAX_STREAM_BYTES,
                lzw_maximum_output_length=MAX_STREAM_BYTES,
                run_length_maximum_output_length=MAX_STREAM_BYTES,
                page_tree_maximum_entries=MAX_PAGES * 3,
                page_tree_maximum_depth=20,
                xform_maximum_invocations_per_extraction=500,
                jbig2dec_binary=None,
            ),
        ):
            reader = PdfReader(io.BytesIO(raw), strict=True)
            if reader.is_encrypted:
                raise PayloadError("encrypted SEC PDF requires review; no decryption attempted")
            if not 1 <= len(reader.pages) <= MAX_PAGES:
                raise PayloadError("SEC PDF page count is empty or exceeds the limit")
            pieces = []
            stream_bytes = 0
            text_chars = 0
            for page in reader.pages:
                contents = page.get_contents()
                if contents is None:
                    raise UnreadableDocumentError("SEC PDF page has no readable content")
                size = len(contents.get_data())
                stream_bytes += size
                if size > MAX_STREAM_BYTES or stream_bytes > MAX_TOTAL_STREAM_BYTES:
                    raise PayloadError("SEC PDF decompressed content exceeds the limit")
                text = page.extract_text()
                controls = sum(ord(char) < 32 and char not in "\n\r\t" for char in text)
                if len(text.strip()) < 20 or "\ufffd" in text or controls > len(text) * 0.01:
                    raise UnreadableDocumentError(
                        "SEC PDF page text is missing or incomplete; review required"
                    )
                text_chars += len(text)
                if text_chars > MAX_TEXT_CHARS:
                    raise PayloadError("SEC PDF extracted text exceeds the limit")
                pieces.append(text)
            if warning_flag.seen or notices:
                raise PayloadError("SEC PDF extraction warnings require review")
            return "\n\n".join(pieces)
    except PayloadError:
        raise
    except Exception as exc:
        # Public parser boundary: PDF diagnostics may include arbitrary source content.
        raise PayloadError("SEC PDF is malformed or cannot be completely read") from exc
    finally:
        logger.removeHandler(warning_flag)
        logger.propagate = previous_propagate
