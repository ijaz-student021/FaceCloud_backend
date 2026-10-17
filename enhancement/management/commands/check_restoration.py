"""Report which restoration stages (deblur, face restoration, upscale to HD) can run on this server, and why not when they cannot."""
import hashlib
import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from enhancement import restoration


def device_name():
    try:
        import torch
    except ImportError:
        return "none (PyTorch not installed)"
    if torch.cuda.is_available():
        return f"cuda ({torch.cuda.get_device_name(0)})"
    if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        return "mps (Apple GPU)"
    return "cpu"


def sha256_of(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class Command(BaseCommand):
    help = "Show which restoration stages can run on this server (switched on, checkpoint present, PyTorch importable)."

    def add_arguments(self, parser):
        parser.add_argument("--verify", action="store_true", help="Also compute each checkpoint's SHA-256 and compare it with the configured one.")
        parser.add_argument("--json", action="store_true", help="Print the report as JSON.")
        parser.add_argument("--require", nargs="*", choices=sorted(restoration.STAGE_ENGINES), default=[],
                            help="Exit with an error unless these stages are available (for a deployment check).")

    def handle(self, *args, **options):
        status = restoration.stage_status()
        if options["verify"]:
            for stage in status.values():
                for engine in stage["engines"]:
                    expected = str(getattr(settings, engine["checksum_setting"], "") if engine["checksum_setting"] else "").lower()
                    if engine["checkpoint_present"] and expected:
                        actual = sha256_of(engine["checkpoint"])
                        engine["checksum_ok"] = actual == expected
                        if actual != expected:
                            engine["available"] = False
                            engine["problems"].append("checksum does not match (the file is corrupt or the wrong one)")
                            stage["available"] = any(item["available"] for item in stage["engines"])
        report = {"policy": settings.ENHANCEMENT_RENDER_POLICY, "escalation": settings.ENHANCEMENT_RESTORATION_ESCALATION,
                  "device": device_name(), "stages": status}
        if options["json"]:
            self.stdout.write(json.dumps(report, indent=2, default=str))
        else:
            self.stdout.write(f"Rendering policy: {report['policy']}; restoration escalation: {'on' if report['escalation'] else 'off'}; device: {report['device']}")
            for name, stage in status.items():
                self.stdout.write(self.style.SUCCESS(f"[ok]  {name}: {stage['engine']}") if stage["available"]
                                  else self.style.WARNING(f"[--]  {name}: not available"))
                for engine in stage["engines"]:
                    mark = "available" if engine["available"] else "; ".join(engine["problems"])
                    checksum = "" if "checksum_ok" not in engine else (" checksum ok" if engine["checksum_ok"] else " CHECKSUM MISMATCH")
                    self.stdout.write(f"        {engine['engine']} ({engine['licence']}): {mark}{checksum}")
        missing = [stage for stage in options["require"] if not status[stage]["available"]]
        if missing:
            raise CommandError("Required restoration stages are not available: " + ", ".join(missing))
