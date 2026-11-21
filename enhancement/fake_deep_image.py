"""A local stand-in for Deep Image's REST API, for tests and development only.

Production code never imports this module. It lets the *real* client (``enhancement.cloud.DeepImageClient``: real multipart encoding, real headers, real redirects and
error handling) run against a server on 127.0.0.1 (``DEEP_IMAGE_BASE_URL``), so all of that is exercised without an account, a key, a credit or a network. What it cannot show
is how the real service behaves: its answers are scripted, and the shapes it answers with are the ones the provider's documentation shows (``POST /process`` -> ``{"job": ...}``,
``GET /result/<job>`` -> ``{"status": ..., "result_url": ...}``).

``behaviours`` is a list consumed one per upload (the last one repeats):

* ``{}``                                  accept the upload, report ``in_progress`` ``polls`` times, then ``complete`` with the image (default)
* ``{"image": bytes}``                    the bytes served as the result (default: the upload enlarged 2x, a plausible "enhancement")
* ``{"polls": 2}``                        status requests answered ``in_progress`` before ``complete``
* ``{"status_names": ["received", "predicting"]}``  the in-progress statuses reported, in turn (the last repeats): the real service says ``predicting``
* ``{"status_redirect": "<url>"}``        the status request is answered with a redirect there (a request that carries the key must never follow it)
* ``{"upload_status": 402}``              refuse the upload with this HTTP status (a ``{"message": ..., "code": "BAD_REQUEST"}`` body, the shape the real service uses)
* ``{"upload_message": "..."}``           with ``upload_status``: the message in that body
* ``{"status_after": "failed"}``          the status the job ends in instead of ``complete``
* ``{"status_errors": 2}``                the first status requests fail with HTTP 503
* ``{"result_status": 404}``              the result address answers with this HTTP status
* ``{"result_redirect": "<url>"}``        the result address redirects there
* ``{"via_redirect": True}``              the result address is one hop away (it redirects to the real one)
* ``{"upload_redirect": "<url>"}``        the upload is answered with a redirect there (a request that carries the key must never follow it)
* ``{"echo_key": True}``                  with ``upload_status``: the refusal's body repeats the key it received (the log must not)
* ``{"result_url": "<url>"}``             report this address instead of the server's own
* ``{"never_done": True}``                always ``in_progress``
* ``{"delay": seconds}``                  wait before answering the upload
* ``{"bad_json": True}``                  answer the upload with text that is not JSON
* ``{"no_job": True}``                    answer the upload with JSON that holds no job id
* ``{"no_result_url": True}``             report ``complete`` without a result address
"""
import json
import re
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2
import numpy as np


def enlarged_copy(payload, factor=2):
    """The upload enlarged: a stand-in for an enhancement that keeps the picture and changes its size."""
    image = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_COLOR)
    big = cv2.resize(image, None, fx=factor, fy=factor, interpolation=cv2.INTER_CUBIC)
    return cv2.imencode(".jpg", big, [cv2.IMWRITE_JPEG_QUALITY, 92])[1].tobytes()


class FakeDeepImageServer:
    def __init__(self, behaviours=None, key=None):
        self.behaviours = list(behaviours or [{}])
        self.key = key                      # when set, a request without this x-api-key is answered 401
        self.requests = []                  # {"method", "path", "headers", "body"} in arrival order
        self.jobs = {}
        self.base_url = ""
        self.origin = ""
        self._lock = threading.Lock()
        self._server = None
        self._thread = None

    # -- control -------------------------------------------------------------
    def start(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # silence
                pass

            def _send(self, status, body=b"", content_type="application/json", extra=None):
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                for name, value in (extra or {}).items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(body)

            def _json(self, status, payload):
                self._send(status, json.dumps(payload).encode())

            def _record(self, body=b""):
                with outer._lock:
                    outer.requests.append({"method": self.command, "path": self.path, "headers": {name.lower(): value for name, value in self.headers.items()}, "body": body})

            def _authorised(self):
                if outer.key is not None and self.headers.get("x-api-key") != outer.key:
                    self._json(401, {"error": "invalid api key"})
                    return False
                return True

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("content-length") or 0))
                self._record(body)
                if not self._authorised():
                    return
                with outer._lock:
                    behaviour = outer.behaviours.pop(0) if len(outer.behaviours) > 1 else outer.behaviours[0]
                time.sleep(behaviour.get("delay", 0))
                if behaviour.get("upload_redirect"):
                    return self._send(307, b"", "text/plain", {"Location": behaviour["upload_redirect"]})
                if behaviour.get("upload_status"):
                    echoed = {"key": self.headers.get("x-api-key")} if behaviour.get("echo_key") else {}
                    return self._json(behaviour["upload_status"], {"message": behaviour.get("upload_message", "refused by the scripted behaviour"), "code": "BAD_REQUEST", **echoed})
                if behaviour.get("bad_json"):
                    return self._send(200, b"this is not json", "text/plain")
                if behaviour.get("no_job"):
                    return self._json(200, {"ok": True})
                job = uuid.uuid4().hex
                with outer._lock:
                    outer.jobs[job] = {"behaviour": behaviour, "polls": 0, "status_errors": 0, "upload": outer.upload_bytes(body)}
                self._json(200, {"job": job})

            def do_GET(self):
                self._record()
                match = re.fullmatch(r"/rest_api/result/([0-9a-f]+)", self.path)
                if match:
                    if not self._authorised():
                        return
                    return self._status(match.group(1))
                match = re.fullmatch(r"/hop/([0-9a-f]+)", self.path)
                if match:
                    return self._send(302, b"", "text/plain", {"Location": f"{outer.origin}/files/{match.group(1)}.jpg"})
                match = re.fullmatch(r"/files/([0-9a-f]+)\.jpg", self.path)
                if match and match.group(1) in outer.jobs:
                    job = outer.jobs[match.group(1)]
                    behaviour = job["behaviour"]
                    if behaviour.get("result_status"):
                        return self._send(behaviour["result_status"], b"gone", "text/plain")
                    if behaviour.get("result_redirect"):
                        return self._send(302, b"", "text/plain", {"Location": behaviour["result_redirect"]})
                    return self._send(200, behaviour.get("image") or enlarged_copy(job["upload"]), "image/jpeg")
                self._json(404, {"error": "not found"})

            def _status(self, job_id):
                with outer._lock:
                    job = outer.jobs.get(job_id)
                    if job is None:
                        return self._json(404, {"error": "unknown job"})
                    behaviour = job["behaviour"]
                    if behaviour.get("status_redirect"):
                        return self._send(302, b"", "text/plain", {"Location": behaviour["status_redirect"]})
                    if job["status_errors"] < behaviour.get("status_errors", 0):
                        job["status_errors"] += 1
                        return self._json(503, {"error": "try again"})
                    job["polls"] += 1
                    done = not behaviour.get("never_done") and job["polls"] > behaviour.get("polls", 0)
                if not done:
                    names = behaviour.get("status_names") or ["in_progress"]
                    return self._json(200, {"status": names[min(job["polls"], len(names)) - 1]})
                if behaviour.get("status_after"):
                    return self._json(200, {"status": behaviour["status_after"]})
                if behaviour.get("no_result_url"):
                    return self._json(200, {"status": "complete"})
                where = f"{outer.origin}/hop/{job_id}" if behaviour.get("via_redirect") else f"{outer.origin}/files/{job_id}.jpg"
                self._json(200, {"status": "complete", "result_url": behaviour.get("result_url") or where})

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.origin = f"http://127.0.0.1:{self._server.server_address[1]}"
        self.base_url = self.origin + "/rest_api"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._thread.join(timeout=5)

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    # -- what a test reads -------------------------------------------------------
    @staticmethod
    def upload_bytes(body):
        """The JPEG inside a multipart upload (the bytes between the file part's header and the closing boundary)."""
        start = body.find(b"\r\n\r\n", body.find(b'name="image"')) + 4
        return body[start:body.rfind(b"\r\n--")]

    @staticmethod
    def parameters(body):
        """The ``parameters`` form field of a multipart upload, parsed."""
        match = re.search(rb'name="parameters"\r\n\r\n(.*?)\r\n--', body, re.S)
        return json.loads(match.group(1)) if match else None

    def uploads(self):
        return [item for item in self.requests if item["method"] == "POST"]
