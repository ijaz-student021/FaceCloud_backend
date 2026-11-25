"""The development server's connection queue is long enough for a gallery page (see operations/devserver.py)."""
from django.core.servers import basehttp
from django.test import SimpleTestCase

from operations.devserver import LISTEN_QUEUE, lengthen_listen_queue


class ListenQueueTests(SimpleTestCase):
    def test_the_queue_is_lengthened_when_the_app_is_ready(self):
        self.assertGreaterEqual(basehttp.WSGIServer.request_queue_size, LISTEN_QUEUE)

    def test_a_longer_queue_is_never_shortened(self):
        original = basehttp.WSGIServer.request_queue_size
        try:
            basehttp.WSGIServer.request_queue_size = 500
            lengthen_listen_queue()
            self.assertEqual(basehttp.WSGIServer.request_queue_size, 500)
        finally:
            basehttp.WSGIServer.request_queue_size = original
