# Rate limits: what they count, and the values for a demo day and for production

## The problem they were changed to avoid

A classroom, a campus or a defence-day room shares **one public address**. Limits counted per address and counted on *every* request were used up by a
handful of people: the default "anonymous" limit is 100 requests an hour for the whole address, sign-in was 20 an hour for the whole address and token
refresh 60 an hour for the whole address, so a few mistakes locked everyone out. (Behind Nginx there is a second way to get this wrong: if
`TRUSTED_PROXY_COUNT` is 0, every visitor looks like Nginx's own address. Set it to the number of proxies in front of Django, `1` for one Nginx.)

## Sign-in and token refresh now count what signals abuse

| Scope | Counts | Per | Default |
|---|---|---|---|
| `login` | **failed** sign-ins only (a correct password never uses any up) | address **and** account name together | 20/hour |
| `login_ip` | failed sign-ins | address, across every account name (one address trying many accounts) | 300/hour |
| `token_refresh` | refresh requests | address **and** refresh token | 60/hour |
| `token_refresh_ip` | refresh requests | address (a ceiling against floods of different tokens) | 1200/hour |

Guessing at **one account** is stopped by two things that do not depend on the address: the per-account backoff in `accounts/lockout.py` (after
`LOGIN_LOCK_THRESHOLD`=5 consecutive failures the account is refused for 30 s, doubling to 15 min, even with the right password), and the per-(address, name)
limit above. Many people on one address, each with their own account, never meet either. Sign-in and refresh no longer fall under the generic
anonymous limit.

What a client sees when it is stopped: HTTP 429 with a `Retry-After` header (seconds) and `{"detail": "Request was throttled. Expected available in N seconds."}`
(or the lock message "Too many failed sign-in attempts. Try again in N seconds."). Show the number of seconds, not a generic error.

## Everything else is still per address (or per user)

The other limits are unchanged. The ones a room of people signing up together can meet are the *anonymous* ones, which are per address:

| Scope | Default | Used by |
|---|---|---|
| `anon` | 100/hour | **every** unauthenticated request of an address (registering, verifying, resetting a password, public share links, contact) |
| `registration` | 10/hour | `POST /api/auth/register/` |
| `verification` | 10/hour | `POST /api/auth/verify-email/` and its resend |
| `verification_status` | 60/hour | the "check your email" polling |
| `password_reset` | 10/hour | password reset requests and confirmations |
| `invitation` | 30/hour | invitation preview, inspect and accept |
| `contact` | 5/hour | the contact form |

Per-user limits (`user` 1000/hour, `image_upload` 100/hour, `zip_upload` 20/hour, ...) are counted per signed-in account, so a shared address does not matter.

## Recommended values

Set these in the environment (`THROTTLE_RATE_<SCOPE>`, the scope in upper case; a malformed value stops the start and names the variable).

**Production** (real users, many addresses): leave everything at the defaults, and set `TRUSTED_PROXY_COUNT=1` behind Nginx. The production guard refuses a
`login` rate above 100/hour and a `login_ip` rate above 1000/hour.

**Demo or defence day** (one address for the whole room, for a few hours; put these in the demo machine's `.env` and remove them afterwards):

```
THROTTLE_RATE_ANON=5000/hour
THROTTLE_RATE_REGISTRATION=300/hour
THROTTLE_RATE_VERIFICATION=600/hour
THROTTLE_RATE_VERIFICATION_STATUS=3000/hour
THROTTLE_RATE_PASSWORD_RESET=200/hour
THROTTLE_RATE_INVITATION=300/hour
```

`THROTTLE_RATE_LOGIN` and `THROTTLE_RATE_TOKEN_REFRESH` do **not** need raising any more. Do not copy the demo block to production: the production guard only
checks the two sign-in limits, so the others are on you.
