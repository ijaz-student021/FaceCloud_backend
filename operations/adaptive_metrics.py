"""Operational metrics for the adaptive enhancement engine, computed from what the jobs already store.

No new service and no image content: it reads ``ImageEnhancementResult`` rows (status, timings, the ``adaptive`` block of
``applied_parameters``) over a time window. The same function backs the ``adaptive_report`` management command, so an operator
can answer "how often is the original preserved, how long do jobs take (P95), which operations are rejected and why" from a
shell or a cron job. Resource utilisation (worker memory, queue depth) comes from the host and the broker, not from here.
"""
from collections import Counter
from datetime import timedelta

from django.utils import timezone

from enhancement.models import ImageEnhancementResult


def percentile(values, q):
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * q / 100.0
    low, high = int(position), min(int(position) + 1, len(ordered) - 1)
    return round(ordered[low] + (ordered[high] - ordered[low]) * (position - low), 2)


def adaptive_metrics(days=7, now=None):
    since = (now or timezone.now()) - timedelta(days=days)
    rows = ImageEnhancementResult.objects.filter(created_at__gte=since).exclude(applied_parameters={}).values(
        "status", "processing_duration_ms", "applied_parameters", "created_at", "started_at")
    outcomes, rejected, operations, models = Counter(), Counter(), Counter(), Counter()
    identity, seconds, wait = Counter(), [], []
    stage_seconds = {}
    failed = timeouts = total = 0
    for row in rows:
        adaptive = (row["applied_parameters"] or {}).get("adaptive") if isinstance(row["applied_parameters"], dict) else None
        if row["status"] == "failed":
            failed += 1
        if not isinstance(adaptive, dict):
            continue
        total += 1
        outcomes[adaptive.get("outcome")] += 1
        if row["processing_duration_ms"]:
            seconds.append(row["processing_duration_ms"] / 1000.0)
        if row["started_at"]:
            wait.append((row["started_at"] - row["created_at"]).total_seconds())
        identity[(row["applied_parameters"].get("identity_verification") or {}).get("state")] += 1
        if any("time budget" in str(w) for w in row["applied_parameters"].get("warnings", [])):
            timeouts += 1
        for stage in adaptive.get("stages", []):
            operations[(stage["operation"], stage["status"])] += 1
            for reason in stage.get("rejection_reasons") or []:
                rejected[(stage["operation"], reason)] += 1
            if stage.get("seconds") is not None:
                stage_seconds.setdefault(stage["operation"], []).append(stage["seconds"])
            for model in stage.get("models_run") or []:
                models[model] += 1
    return {
        "window_days": days, "adaptive_results": total, "failed_jobs_in_window": failed, "time_budget_reached": timeouts,
        "outcomes": dict(outcomes),
        "original_preserved_rate": round(outcomes.get("original_preserved", 0) / total, 3) if total else None,
        "job_seconds": {"p50": percentile(seconds, 50), "p95": percentile(seconds, 95), "max": percentile(seconds, 100)},
        "queue_wait_seconds": {"p50": percentile(wait, 50), "p95": percentile(wait, 95)},
        "operations": {f"{op}:{status}": count for (op, status), count in sorted(operations.items())},
        "rejection_reasons": {f"{op}:{reason}": count for (op, reason), count in rejected.most_common()},
        "stage_seconds": {op: {"p50": percentile(v, 50), "p95": percentile(v, 95)} for op, v in stage_seconds.items()},
        "models_run": dict(models), "identity_states": {str(k): v for k, v in identity.items()},
    }
