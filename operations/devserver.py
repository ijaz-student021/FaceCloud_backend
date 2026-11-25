"""A longer connection queue for Django's development server (``manage.py runserver``).

``runserver`` listens with a queue of 10 pending connections. A page that asks for two dozen thumbnails at once (the gallery, in development the frontend asks twice) overflows it:
the extra connections are reset ("Broken pipe" / "Connection reset by peer" in the terminal) and the browser shows those tiles as photos that failed to load. Measured on this project:
12 parallel requests all answered, 16 lost about a fifth, 24 lost about a third. A queue of 128 answers all of them. It changes nothing in production (gunicorn sets its own backlog).
"""
from django.core.servers import basehttp

LISTEN_QUEUE = 128


def lengthen_listen_queue(size=LISTEN_QUEUE):
    if basehttp.WSGIServer.request_queue_size < size:
        basehttp.WSGIServer.request_queue_size = size
