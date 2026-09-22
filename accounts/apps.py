from django.apps import AppConfig


class AccountsConfig(AppConfig):
    name = 'accounts'

    def ready(self):
        from . import schema  # noqa: F401  (registers the drf-spectacular auth extension)
