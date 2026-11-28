from django.apps import AppConfig


class OrganizationsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "organizations"

    def ready(self):
        from .access import install_request_hook

        install_request_hook()          # a member who is not active in an organization cannot reach its content (organizations/access.py)
