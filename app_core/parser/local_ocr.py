"""Optional local OCR fallback for scanned PDF pages.

The release keeps OCR optional: RapidOCR (ONNX) or pytesseract are used when
installed, otherwise the parser simply falls back to the PDF text layer.  No
cloud request is made here, which keeps the library's 0-Token cleaning mode
strictly local.
"""

from __future__ import annotations

import io
import logging
from functools import lru_cache

log = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def local_ocr_available() -> bool:
    try:
        import rapidocr_onnxruntime  # noqa: F401
        return True
    except Exception:
        pass
    try:
        import pytesseract  # noqa: F401
        return True
    except Exception:
        pass
    return False


@lru_cache(maxsize=1)
def _rapid_engine():
    try:
        from rapidocr_onnxruntime import RapidOCR
        return RapidOCR()
    except Exception as exc:
        log.debug("RapidOCR unavailable: %s", exc)
        return None


def local_ocr_text(image_bytes: bytes) -> str:
    """Return text from an image using a locally installed OCR engine."""
    if not image_bytes:
        return ""
    engine = _rapid_engine()
    if engine is not None:
        try:
            result, _elapsed = engine(image_bytes)
            if result:
                return "\n".join(str(item[1]) for item in result if len(item) > 1)
        except Exception:
            log.debug("RapidOCR inference failed", exc_info=True)
    try:
        import pytesseract
        from PIL import Image

        image = Image.open(io.BytesIO(image_bytes))
        return pytesseract.image_to_string(image, lang="eng")
    except Exception:
        log.debug("Local OCR unavailable or failed", exc_info=True)
        return ""


__all__ = ["local_ocr_available", "local_ocr_text"]
