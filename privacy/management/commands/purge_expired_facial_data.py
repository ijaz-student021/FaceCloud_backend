from django.core.management.base import BaseCommand

from privacy.services import purge_expired_facial_data


class Command(BaseCommand):
    help = "Delete face embeddings and identity groups older than FACIAL_DATA_RETENTION_DAYS."

    def handle(self, *args, **options):
        summary = purge_expired_facial_data()
        if not summary["enabled"]:
            self.stdout.write("Facial-data retention is disabled (FACIAL_DATA_RETENTION_DAYS=0).")
            return
        style = self.style.SUCCESS if summary.get("unverified_users", 0) == 0 else self.style.ERROR
        self.stdout.write(
            style(
                f"Purged data for {summary['users_affected']} user(s); "
                f"deleted {summary['deleted']}; unverified users: {summary['unverified_users']}."
            )
        )
