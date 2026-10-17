from pathlib import Path

from django.conf import settings
from django.core.checks import Error, Warning, register


@register()
def identity_configuration_check(app_configs, **kwargs):
    errors = []
    match = settings.IDENTITY_MATCH_THRESHOLD
    boundary = settings.IDENTITY_NEW_GROUP_THRESHOLD
    margin = settings.IDENTITY_AMBIGUITY_MARGIN
    if not (0 <= boundary < match <= 1):
        errors.append(Error("Identity thresholds must satisfy 0 <= new-group < match <= 1.", id="identities.E001"))
    if not (0 <= margin < 1):
        errors.append(Error("Identity ambiguity margin must be in [0, 1).", id="identities.E002"))
    if settings.IDENTITY_EMBEDDING_DIMENSION <= 0:
        errors.append(Error("Identity embedding dimension must be positive.", id="identities.E003"))
    if settings.IDENTITY_MAX_CANDIDATE_GROUPS <= 0:
        errors.append(Error("Identity candidate limit must be positive.", id="identities.E004"))
    if settings.IDENTITY_AUTO_ANALYZE:
        if settings.IDENTITY_PROVIDER == "disabled":
            errors.append(Error("Automatic identity analysis requires an approved provider.", id="identities.E005"))
        if not settings.IDENTITY_MODEL_PATH or not Path(settings.IDENTITY_MODEL_PATH).is_file():
            errors.append(Error("The configured identity model is unavailable.", id="identities.E006"))
        if len(settings.IDENTITY_MODEL_SHA256) != 64:
            errors.append(Error("A SHA-256 checksum is required for the identity model.", id="identities.E007"))
        if not settings.IDENTITY_LICENSE_RECORD_PATH or not Path(settings.IDENTITY_LICENSE_RECORD_PATH).is_file():
            errors.append(Error("A local commercial-license record is required for identity inference.", id="identities.E008"))
        if settings.FACE_DETECTOR_PROVIDER != "yunet_v1":
            errors.append(Error("Automatic SFace identity analysis requires the YuNet detector.", id="identities.E009"))
        if not Path(settings.YUNET_MODEL_PATH).is_file() or len(settings.YUNET_MODEL_SHA256) != 64:
            errors.append(Error("A checksummed local YuNet model is required.", id="identities.E010"))
    if settings.IDENTITY_PROVIDER == "arcface_w600k_r50_v1":
        if not Path(settings.ARCFACE_MODEL_PATH).is_file():
            errors.append(Error("The configured ArcFace model is unavailable.", id="identities.E013"))
        if len(settings.ARCFACE_MODEL_SHA256) != 64:
            errors.append(Error("A SHA-256 checksum is required for the ArcFace model.", id="identities.E014"))
        if not Path(settings.ARCFACE_LICENSE_RECORD_PATH).is_file():
            errors.append(Error("A license record is required for ArcFace (non-commercial research weights).", id="identities.E015"))
        if not (0 <= settings.ARCFACE_NEW_GROUP_THRESHOLD < settings.ARCFACE_MATCH_THRESHOLD <= 1):
            errors.append(Error("ArcFace thresholds must satisfy 0 <= new-group < match <= 1.", id="identities.E016"))
    if not (0 < settings.YUNET_SCORE_THRESHOLD <= 1):
        errors.append(Error("YuNet score threshold must be in (0, 1].", id="identities.E011"))
    if not (0 < settings.YUNET_NMS_THRESHOLD <= 1):
        errors.append(Error("YuNet NMS threshold must be in (0, 1].", id="identities.E012"))
    elif settings.IDENTITY_PROVIDER == "disabled":
        errors.append(Warning("Identity inference is disabled until an approved model is configured.", id="identities.W001"))
    return errors
