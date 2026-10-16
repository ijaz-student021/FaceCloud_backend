"""How a stored timeline event reads on the dashboard: a type, a status and a sentence.

The frontend draws one icon per ``type`` and one colour per ``status``; the stored ``category`` and
``event`` names are kept in the response for anyone who needs the exact record. The mapping lives in
one table so the documentation and the code cannot drift.

type:   upload | enhance | validate | download | live | group | reject | login | email | admin
status: success | warning | error | info
"""

# (type, status, message) for events with a fixed name.
EXACT = {
    # uploads
    "upload_received": ("upload", "info", "Upload received."),
    # processing
    "processing_started": ("enhance", "info", "Processing started."),
    "batch_completed": ("enhance", "success", "Batch processing completed."),
    "batch_partial": ("enhance", "warning", "Batch processing finished, but some photos could not be processed."),
    "batch_failed": ("enhance", "error", "Batch processing failed."),
    "batch_cancelled": ("enhance", "warning", "Batch processing was cancelled."),
    "quality_accepted": ("validate", "success", "A photo passed the quality check."),
    "quality_rejected": ("reject", "warning", "A photo was rejected by the quality check."),
    "quality_needs_review": ("reject", "warning", "A photo needs a manual quality review."),
    "quality_failed": ("validate", "error", "The quality check of a photo failed."),
    "enhancement_completed": ("enhance", "success", "A photo was enhanced."),
    "enhancement_failed": ("enhance", "error", "Enhancing a photo failed."),
    "enhancement_cancelled": ("enhance", "warning", "Enhancing a photo was cancelled."),
    # downloads and sharing
    "package_ready": ("download", "success", "A download package is ready."),
    "package_failed": ("download", "error", "Creating a download package failed."),
    "package_expired": ("download", "warning", "A download package expired."),
    "identity_export_ready": ("download", "success", "A person-folder export is ready."),
    "identity_export_failed": ("download", "error", "Creating a person-folder export failed."),
    "identity_export_expired": ("download", "warning", "A person-folder export expired."),
    "report_ready": ("download", "success", "A report is ready."),
    "report_failed": ("download", "error", "Creating a report failed."),
    "share_link_created": ("download", "success", "A share link was created."),
    "share_link_revoked": ("download", "info", "A share link was revoked."),
    # privacy
    "consent_granted": ("validate", "success", "Face-recognition consent was given."),
    "consent_withdrawn": ("validate", "warning", "Face-recognition consent was withdrawn."),
    "facial_data_erased": ("group", "success", "Your facial data was erased."),
    "data_exported": ("download", "success", "Your data export was created."),
    # accounts
    "registration_pending": ("email", "info", "Registration started; check your email."),
    "verification_code_sent": ("email", "info", "A verification email was sent."),
    "registered": ("email", "success", "Your account was created."),
    "email_verified": ("email", "success", "Your email address was verified."),
    "password_reset": ("email", "success", "Your password was reset."),
    "login": ("login", "success", "Signed in."),
    "login_failed": ("login", "error", "A sign-in attempt failed."),
    "login_locked": ("login", "warning", "Sign-in was temporarily locked after repeated failures."),
    "logout": ("login", "info", "Signed out."),
    "logout_all": ("login", "info", "Signed out of every device."),
    "password_changed": ("login", "success", "Your password was changed."),
    "profile_updated": ("login", "success", "Your profile was updated."),
    "role_changed": ("admin", "info", "Your role was changed by an administrator."),
    "suspended": ("admin", "warning", "Your account was suspended."),
    "reactivated": ("admin", "success", "Your account was reactivated."),
    "account_deleted": ("admin", "info", "Your account was deleted."),
    "admin_created_account": ("admin", "info", "Your account was created by an administrator."),
}

# Event families whose last word varies (group_renamed, group_merged, ...).
GROUP_MESSAGES = {
    "renamed": "A person folder was renamed.",
    "merged": "Person folders were merged.",
    "moved": "A photo was moved to another person folder.",
    "removed": "A photo was removed from a person folder.",
    "profile_set": "A person folder's cover photo was changed.",
    "deleted": "A person folder was deleted.",
    "reviewed": "A suggested person match was reviewed.",
}

# Fallback by stored category when the event name is not listed above.
CATEGORY_DEFAULT_TYPE = {
    "upload": "upload", "processing": "enhance", "moderation": "validate", "download": "download",
    "group": "group", "share": "download", "admin": "admin", "privacy": "validate", "auth": "login",
}

FAILURE_WORDS = ("failed", "error")
WARNING_WORDS = ("rejected", "cancelled", "expired", "needs_review", "withdrawn", "locked", "partial")
SUCCESS_WORDS = ("completed", "ready", "accepted", "created", "granted", "success")


def _fallback_status(event):
    if any(word in event for word in FAILURE_WORDS):
        return "error"
    if any(word in event for word in WARNING_WORDS):
        return "warning"
    if any(word in event for word in SUCCESS_WORDS):
        return "success"
    return "info"


def describe(category, event):
    """``(type, status, message)`` for one stored event; unknown names still get a sensible reading."""
    if event in EXACT:
        return EXACT[event]
    if event.startswith("group_"):
        action = event[len("group_"):]
        return "group", "info", GROUP_MESSAGES.get(action, f"Person folder change: {action.replace('_', ' ')}.")
    kind = "reject" if "reject" in event else CATEGORY_DEFAULT_TYPE.get(category, "validate")
    message = event.replace("_", " ").strip().capitalize() + "."
    return kind, _fallback_status(event), message
