"""A local stand-in for the OpenAI Responses endpoint, for tests and development only.

Production code never imports this module. It lets the *real* ``openai`` SDK run
its real request serialisation, error handling and response parsing against a
server on 127.0.0.1 (``OPENAI_BASE_URL``), so those parts are exercised without
any account, key or network. What it cannot show is how the real model behaves:
its replies are scripted.

``behaviours`` is a list consumed one per request (the last one repeats):

* ``{"advice": {...}}``  reply with this suggestion (default)
* ``{"status": 429, "retry_after": "1", "code": "rate_limit_exceeded"}``  an error response
* ``{"refusal": "..."}``  a refusal content part
* ``{"incomplete": True}``  ``status: incomplete``
* ``{"text": "..."}``  raw output text (e.g. not JSON)
* any behaviour may add ``"delay": seconds``
"""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeResponsesServer:
    def __init__(self, behaviours=None, delay=0.0, tokens=(300, 60)):
        self.behaviours = list(behaviours or [{}])
        self.delay = delay
        self.tokens = tokens
        self.requests = []          # {"path", "headers", "body"} in arrival order
        self.in_flight = 0
        self.peak_in_flight = 0
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

            def do_POST(self):
                length = int(self.headers.get("content-length") or 0)
                raw = self.rfile.read(length)
                try:
                    body = json.loads(raw or b"{}")
                except ValueError:
                    body = {"_unparseable": True}
                with outer._lock:
                    outer.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
                    behaviour = outer.behaviours.pop(0) if len(outer.behaviours) > 1 else outer.behaviours[0]
                    outer.in_flight += 1
                    outer.peak_in_flight = max(outer.peak_in_flight, outer.in_flight)
                try:
                    time.sleep(behaviour.get("delay", outer.delay))
                    status, payload, headers = outer._reply(self.path, behaviour, body)
                finally:
                    with outer._lock:
                        outer.in_flight -= 1
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                for key, value in headers.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(data)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self._server.server_address[1]}/v1"

    def stop(self):
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    # -- replies -------------------------------------------------------------
    def _reply(self, path, behaviour, body):
        if not path.rstrip("/").endswith("/responses"):
            return 404, {"error": {"message": "unknown route", "type": "invalid_request_error", "code": "not_found"}}, {}
        status = behaviour.get("status", 200)
        if status != 200:
            headers = {"retry-after": str(behaviour["retry_after"])} if "retry_after" in behaviour else {}
            error = {"message": "scripted failure", "type": "scripted", "param": None, "code": behaviour.get("code")}
            return status, {"error": error}, headers
        content = [{"type": "output_text", "text": "", "annotations": []}]
        response_status = "completed"
        if "refusal" in behaviour:
            content = [{"type": "refusal", "refusal": behaviour["refusal"]}]
        elif behaviour.get("incomplete"):
            response_status = "incomplete"
            content = [{"type": "output_text", "text": '{"action": "ap', "annotations": []}]
        elif "text" in behaviour:
            content[0]["text"] = behaviour["text"]
        else:
            advice = behaviour.get("advice") or {
                "action": "apply",
                "adjustments": {"brightness": 0, "contrast": 6, "saturation": 0, "shadows": 0,
                                "highlights": -4, "warmth": 0, "sharpness": 0},
                "rationale": "Slightly flat; a little contrast.", "caution_flags": [],
            }
            content[0]["text"] = json.dumps(advice)
        payload = {
            "id": "resp_fake", "object": "response", "created_at": int(time.time()), "status": response_status,
            "model": body.get("model", "fake"), "store": body.get("store"),
            "output": [{"id": "msg_fake", "type": "message", "status": "completed", "role": "assistant", "content": content}],
            "usage": {"input_tokens": self.tokens[0], "output_tokens": self.tokens[1],
                      "total_tokens": sum(self.tokens)},
        }
        if response_status == "incomplete":
            payload["incomplete_details"] = {"reason": "max_output_tokens"}
        return 200, payload, {}
