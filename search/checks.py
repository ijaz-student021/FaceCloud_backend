import re
from pathlib import Path

from django.conf import settings
from django.core.checks import Error, register


@register()
def semantic_search_checks(app_configs, **kwargs):
    """With search by meaning switched on, the model, its checksum and its licence record must be in place; off, nothing is asked."""
    errors = []
    if not settings.SEMANTIC_SEARCH_ENABLED:
        return errors
    if not settings.SEMANTIC_SEARCH_MODEL_DIR or not (Path(settings.SEMANTIC_SEARCH_MODEL_DIR) / "open_clip_model.safetensors").is_file():
        errors.append(Error("SEMANTIC_SEARCH_ENABLED is on but the model files are not in SEMANTIC_SEARCH_MODEL_DIR.", id="search.E001"))
    if len(settings.SEMANTIC_SEARCH_MODEL_SHA256) != 64:
        errors.append(Error("A SHA-256 checksum is required for the search-by-meaning model (SEMANTIC_SEARCH_MODEL_SHA256).", id="search.E002"))
    record = Path(settings.SEMANTIC_SEARCH_LICENSE_RECORD_PATH) if settings.SEMANTIC_SEARCH_LICENSE_RECORD_PATH else None
    if record is None or not record.is_file():
        errors.append(Error("A licence record is required for the search-by-meaning model (docs/model-licenses/semantic-search-model.md).", id="search.E003"))
        return errors
    text = record.read_text(encoding="utf-8", errors="replace")
    if len(settings.SEMANTIC_SEARCH_MODEL_SHA256) == 64 and settings.SEMANTIC_SEARCH_MODEL_SHA256.lower() not in text.lower():
        errors.append(Error("The licence record does not list the SHA-256 of the configured model weights.", id="search.E004"))
    if not re.search(r"^Owner approval:\s*approved\b", text, flags=re.IGNORECASE | re.MULTILINE):
        errors.append(Error("The licence record has not been approved by the owner (set its 'Owner approval:' line to 'approved' after reading it).", id="search.E005"))
    return errors
