# Real email delivery (SMTP)

By default the application does not send mail: in development the message is printed in the server's console. Real delivery needs an SMTP account and a handful of settings. This page says exactly which,
from what the code reads (`config/settings.py`, `config/production_guard.py`, `accounts/`, `organizations/`, `notifications/`, `contact/`).

## What sends mail

| Mail | Sent by |
|---|---|
| Sign-up verification link (valid once, `SIGNUP_VERIFICATION_LINK_LIFETIME_SECONDS`) and its resend | the API request itself (`accounts/services.py`) |
| Password reset, verification of an e-mail change | the API request |
| Account invitation (administrator) and organization invitation | the API request |
| Notification e-mails (the copy of an in-app notification) | the Celery worker (`notifications/tasks.py`): the worker needs the same settings |
| Contact form message to the addresses in `CONTACT_RECIPIENT_EMAILS` | the API request (`contact/services.py`) |

A failed sign-up e-mail is answered with HTTP 503 (`Verification email delivery is temporarily unavailable.`) and the log line `Unable to send signup verification email.` carries the cause.

## What you need

| Need | Why | Setting |
|---|---|---|
| An SMTP account at a provider: host, port **587** (STARTTLS), user name, password (an app password or API key where the provider has them) | The application logs in and hands the message over. Only STARTTLS is configurable (`EMAIL_USE_TLS`); port 465 (implicit TLS) is not supported by the settings. | `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, `EMAIL_USE_TLS=True` |
| The SMTP backend switched on | The default is the console backend. | `EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend` |
| A sender address the account may send as | Providers refuse a `From` address that is not theirs or not verified. The default `noreply@facecloud.local` will be refused. | `DEFAULT_FROM_EMAIL` |
| Links that open for the person who gets the mail | Every emailed link is built from these. `http://localhost:5173` opens only on the machine that runs the frontend. | `FRONTEND_URL` (verification, reset, invitations), `PUBLIC_BACKEND_URL` (e-mail change) |
| Outbound port 587 open from the server | Some hosts and networks block it. | (network) |
| A domain with SPF, DKIM and DMARC records, for mail that must reach arbitrary inboxes and not the spam folder | The provider shows the records to add at the domain's DNS. Not needed for a demonstration to your own address. | (DNS) |
| A restart of the web process **and the worker** | Settings are read at start. | |

## The lines for `.env`

Placeholders only. Never commit real values, and never paste the password into a chat or a ticket.

```
EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend
EMAIL_HOST=smtp.example.com
EMAIL_PORT=587
EMAIL_USE_TLS=True
EMAIL_HOST_USER=<account name>
EMAIL_HOST_PASSWORD=<password or app password>
DEFAULT_FROM_EMAIL=<an address the account may send as>
EMAIL_TIMEOUT=10
FRONTEND_URL=<where the frontend can be opened by the recipient>
```

`EMAIL_TIMEOUT` (default 10 seconds) is how long the application waits for the SMTP server. Without it a blocked server would hold the sign-up request until the operating system gave up.

## Quickest path for a demonstration: a Gmail account

`EMAIL_HOST=smtp.gmail.com`, `EMAIL_PORT=587`, `EMAIL_USE_TLS=True`, `EMAIL_HOST_USER` the full address, `DEFAULT_FROM_EMAIL` the same address, and as `EMAIL_HOST_PASSWORD` an **app password**, not the
account password: Google issues them only for accounts with 2-Step Verification, and they can be revoked at any time. Google limits how many messages one account may send per day, and mail from
a personal address is for a demonstration only. Check Google's current help page for the exact steps and the limit; they are not verified here.

For a real deployment use a transactional provider with your own domain, and add the SPF, DKIM and DMARC records it gives you.

## Test it before relying on it

```bash
python manage.py sendtestemail you@example.com     # goes through the configured backend; a wrong setting prints the SMTP error
```

Then sign up from the frontend and watch the web log. Common causes:

| What you see | Usually |
|---|---|
| `SMTPAuthenticationError` (535) | wrong user name or password; an app password is needed |
| `SMTPSenderRefused` or `SMTPRecipientsRefused` | `DEFAULT_FROM_EMAIL` is not an address the account may send as |
| `TimeoutError`, `ConnectionRefusedError` or `Network is unreachable` | wrong host or port, or the port is blocked |
| the mail is sent but lands in spam, or never arrives | no SPF, DKIM and DMARC for the sending domain; the provider's dashboard shows the delivery |
| HTTP 503 on sign-up | any of the above: read the `Unable to send signup verification email.` log entry |

## What the production guard demands

When `DJANGO_DEBUG` is off the application refuses to start (`ImproperlyConfigured`, listing every problem it found) unless: `EMAIL_BACKEND` is not a development backend (console, in-memory, dummy, file);
`EMAIL_HOST` is set and not a placeholder; `EMAIL_HOST_USER` and `EMAIL_HOST_PASSWORD` are set (unless the host is local); `FRONTEND_URL` and `PUBLIC_BACKEND_URL` are real `https://` addresses
(not `localhost`). See `config/production_guard.py` and `docs/deployment/VPS_RUNBOOK.md`.

## Not verified here

This project's tests use Django's in-memory mail backend, and the guard is tested with a made-up host: no message has been delivered by a real SMTP server from this code base. The first real delivery
(`sendtestemail`, then a sign-up) is yours to check with your account.
