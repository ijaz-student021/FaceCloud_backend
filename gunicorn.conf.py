"""Gunicorn settings: `gunicorn config.wsgi -c gunicorn.conf.py`.

Gunicorn writes its own access log outside Django's logging, so share-link tokens
(`/api/shares/public/<token>/`) would appear there. This logger redacts them.
"""
from gunicorn.glogging import Logger

from config.log_redaction import redact


class RedactingLogger(Logger):
    def atoms(self, resp, req, environ, request_time):
        atoms = super().atoms(resp, req, environ, request_time)
        return {
            key: redact(value) if isinstance(value, str) else value
            for key, value in atoms.items()
        }


logger_class = RedactingLogger
accesslog = "-"
