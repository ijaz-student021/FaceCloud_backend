"""Start-up warnings for settings that belonged to retired enhancement models.

A deployment that still sets ``ZERO_DCE_ENABLED=True`` (or any other retired
name) used to get that model. It no longer does, and ignoring the setting in
silence would let an operator believe it still applies, so each one is reported.
"""
import os

from django.core.checks import Warning, register

from . import policy


def retired_settings_in_environment(environ=None):
    environ = os.environ if environ is None else environ
    return sorted(
        name for name in environ
        if name.startswith(policy.RETIRED_SETTING_PREFIXES) or name in policy.RETIRED_SETTING_NAMES
    )


@register()
def retired_enhancement_settings_check(app_configs, **kwargs):
    names = retired_settings_in_environment()
    if not names:
        return []
    return [Warning(
        "These environment settings belong to retired enhancement models and have no effect: "
        + ", ".join(names) + ".",
        hint=f"Remove them. What was retired and why: {policy.RETIREMENT_DOCUMENT}.",
        id="enhancement.W001",
    )]
