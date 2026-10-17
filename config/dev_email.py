"""Development email backend: print each message as readable text, with its links on lines you can copy.

Django's console backend writes the raw MIME message. With a UTF-8 body that is quoted-printable: ``=`` becomes ``=3D``, and long lines
are cut with a trailing ``=``. A verification link copied from that output has a mangled ``registration_id`` and ``token``, so
verification fails. This backend prints the decoded text instead, and one clearly marked line per link::

    [dev email] verify link: http://localhost:5173/verify-email?registration_id=...&token=...

Only for development. It is selected automatically when ``DEBUG`` is true and ``EMAIL_BACKEND`` is Django's console backend
(see ``DEV_PRINT_EMAIL_LINKS`` in settings), prints nothing extra when ``DEBUG`` is false, and the production guard refuses it as an
``EMAIL_BACKEND`` (a recipient of real mail never goes through here).
"""
import html
import re

from django.conf import settings
from django.core.mail.backends.console import EmailBackend as ConsoleEmailBackend

LINK_PATTERN = re.compile(r"https?://[^\s<>\"']+")
TRAILING = ".,;:!?)]}"
BAR = "=" * 78

# (fragment of the URL path or query, label). The first match wins.
LABELS = (
    ("verify-email", "verify link"),
    ("reset-password", "password reset link"),
    ("invitation", "invitation link"),
    ("accept", "invitation link"),
)


def label_for(url):
    lowered = url.lower()
    for fragment, label in LABELS:
        if fragment in lowered:
            return label
    return "link"


def links_in(text):
    """The URLs in ``text``, in order, without duplicates and without sentence punctuation stuck to them."""
    found = []
    for match in LINK_PATTERN.findall(html.unescape(text or "")):
        url = match.rstrip(TRAILING)
        if url not in found:
            found.append(url)
    return found


def readable(message):
    """``(text, links)`` for an ``EmailMessage``: the plain body, and every link in it and in its HTML alternatives."""
    texts = [message.body or ""]
    texts += [content for content, mimetype in getattr(message, "alternatives", []) if mimetype == "text/html"]
    links = []
    for text in texts:
        for url in links_in(text):
            if url not in links:
                links.append(url)
    return message.body or "", links


class DevConsoleEmailBackend(ConsoleEmailBackend):
    def write_message(self, message):
        if not settings.DEBUG:
            # Not meant for anything but development: behave exactly like the console backend, add nothing.
            return super().write_message(message)
        body, links = readable(message)
        lines = [BAR, f"[dev email] To: {', '.join(message.to)}", f"[dev email] Subject: {message.subject}"]
        lines += [f"[dev email] {label_for(url)}: {url}" for url in links]
        lines += ["", body.rstrip(), BAR, ""]
        self.stream.write("\n".join(lines))
        self.stream.flush()
