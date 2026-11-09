from django.core.checks import Warning, register

from .inline import WARNING_TEXT, background_jobs_inline


@register()
def background_jobs_run_inline(app_configs, **kwargs):
    """A warning (never an error: it is what development uses) whenever tasks would run inside the request."""
    if background_jobs_inline():
        return [Warning(WARNING_TEXT, id="operations.W001")]
    return []
