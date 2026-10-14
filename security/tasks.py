from celery import shared_task

from .services import evaluate_rules, purge_old_events


@shared_task(name="security.evaluate_alert_rules")
def evaluate_alert_rules():
    return {"opened": evaluate_rules(), "purged_events": purge_old_events()}
