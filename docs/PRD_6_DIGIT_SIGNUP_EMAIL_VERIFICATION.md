# Product Requirements Document: Six-Digit Signup Email Verification

## 1. Document control

| Field | Value |
|---|---|
| Product | FaceCloud |
| Feature | Six-digit signup email verification |
| Area | Authentication and account lifecycle |
| Document type | Product Requirements Document (PRD) |
| Status | Ready for implementation review |
| Priority | High |
| Date | 2026-07-27 |

## 2. Product summary

FaceCloud must verify ownership of an email address before creating a permanent
user account. A person submits a username, email address, and password. The
backend validates the information, stores it temporarily as a pending
registration, and sends a six-digit verification code to the submitted email
address.

The permanent `User` record is created only after the correct, unexpired code is
submitted. The new account is created as a normal `user`, marked as email
verified, and can then log in through the existing JWT authentication endpoint.

## 3. Problem statement

The current backend:

1. creates an unverified `User` during registration;
2. emails a verification link containing a UID and token;
3. changes `is_email_verified` after the link is opened.

The required product experience is different:

1. registration data must remain temporary until verification succeeds;
2. the email must contain a six-digit code;
3. the frontend must provide a code-entry screen;
4. a permanent account must be created only after the code is verified.

This change reduces unverified account records, provides a mobile-friendly
verification experience, and makes account activation explicit.

## 4. Goals

- Send a cryptographically generated six-digit code after valid signup details
  are submitted.
- Create no permanent user account before successful code verification.
- Store passwords only as secure Django password hashes.
- Expire verification codes after a short, configurable period.
- Limit incorrect attempts, resends, and public endpoint traffic.
- Prevent duplicate usernames and email addresses during final account creation.
- Preserve the existing JWT login, role routing, and authorization model after
  verification.
- Provide stable API responses that the frontend can implement directly.
- Record security-relevant registration and verification events without logging
  the verification code or password.

## 5. Non-goals

- Two-factor authentication for every login
- SMS or WhatsApp verification
- Social login
- Passwordless authentication
- Automatic assignment of moderator or administrator roles
- Replacing the existing password-reset flow
- Returning JWT tokens directly from the verification endpoint

The verified user must log in normally after account creation.

## 6. Users and roles

### Applicant

A person who has submitted signup information but does not yet have a permanent
FaceCloud account.

### User

A verified person whose permanent account has been created. Every public signup
creates this role.

### Administrator

Can manage permanent accounts through the existing administrator APIs. An
administrator does not need access to verification codes and cannot retrieve
their plaintext values.

## 7. Required user journey

```text
Signup form
  -> validate username, email, and password
  -> create or replace a pending registration
  -> generate a six-digit code
  -> store only the code hash
  -> send the code by email
  -> show the verification screen
  -> submit registration ID and code
  -> lock and validate the pending registration
  -> create a verified normal user in one database transaction
  -> mark the pending registration consumed
  -> show account-created confirmation
  -> redirect to login
```

### Failed-code journey

```text
Submit incorrect code
  -> increase the attempt counter atomically
  -> return remaining attempts
  -> lock the pending registration after the configured maximum
  -> require a resend to issue a new code
```

### Resend journey

```text
Request resend
  -> return a non-enumerating response
  -> enforce cooldown and hourly limits
  -> invalidate the previous code
  -> generate and email a new code
  -> reset the verification attempt counter
```

## 8. Functional requirements

### FR-01: Registration input

The registration endpoint must accept:

- `username`
- `email`
- `password`
- optional `password_confirm` if the frontend chooses to send it

The backend remains the source of truth for validation.

### FR-02: Input validation

Before sending a code, the backend must:

- normalize the username using Django's username normalization;
- trim the username and reject blank values;
- normalize the email to lowercase;
- validate email syntax;
- apply all configured Django password validators;
- reject a username already used by a permanent user;
- reject an email already used by a permanent user;
- prevent conflicting active pending registrations.

Public registration cannot submit or select a role. The future account role is
always `user`.

### FR-03: Pending registration

A successful registration request must create a temporary pending-registration
record instead of a `User`.

The record must contain:

- opaque public registration ID;
- normalized username;
- normalized email;
- Django password hash;
- verification-code hash;
- code expiry timestamp;
- resend-available timestamp;
- failed-attempt count;
- resend count and resend-window timestamp;
- status;
- creation and update timestamps;
- consumed timestamp when applicable.

The plaintext password and plaintext verification code must never be stored.

### FR-04: Code generation

- The code must contain exactly six numeric digits.
- Leading zeroes are valid.
- Code generation must use Python's `secrets` module or an equivalent
  cryptographically secure generator.
- The code must not be generated using `random`.
- The code must be independently generated for every initial send and resend.

### FR-05: Code lifetime

- Default lifetime: 10 minutes.
- The lifetime must be configurable through environment-backed Django settings.
- An expired code must never create an account.
- Resending a code invalidates the previous code immediately.

### FR-06: Email delivery

The email must include:

- FaceCloud branding;
- the six-digit code;
- the expiry period;
- a statement that the code must not be shared;
- instructions to ignore the email if the recipient did not request it;
- support contact information configured by the deployment.

The subject should be: `Your FaceCloud verification code`.

The code must not be placed in a clickable URL.

### FR-07: Verification

The verification endpoint must accept:

- `registration_id`
- `code`

The backend must:

- validate that the ID is well formed;
- lock the pending row with `select_for_update`;
- reject consumed, cancelled, locked, or expired registrations;
- compare the submitted code to the stored hash using a constant-time,
  password-style hash check;
- increment failed attempts atomically;
- recheck username and email uniqueness against permanent users;
- create the user with the already secured Django password hash;
- set `role="user"`;
- set `is_email_verified=True`;
- mark the pending registration as consumed;
- record registration and email-verification security events;
- commit all successful changes in one transaction.

The endpoint must be idempotent from the user's perspective. Repeating a
successful request must not create a second user.

### FR-08: Failed-attempt limit

- Default maximum: 5 incorrect attempts per issued code.
- The maximum must be configurable.
- Each incorrect attempt reduces the remaining-attempt count.
- After the final failed attempt, the issued code is locked.
- A new code may be obtained only through the resend flow and its limits.

### FR-09: Resend

- Default cooldown: 60 seconds between sends.
- Default limit: 5 sends per email address per hour.
- The values must be configurable.
- A resend generates a new code and hash.
- A resend invalidates the old code.
- A resend resets the failed-attempt counter for the newly issued code.
- The API must not expose whether an arbitrary email address has a pending
  registration.

### FR-10: Pending-record cleanup

- Unconsumed pending registrations must expire automatically.
- Default retention after expiry: 24 hours.
- Consumed records may be retained for a short security-audit period without
  retaining usable credentials or codes.
- A scheduled Celery task or management command must delete records past the
  configured retention period.

### FR-11: Existing login behavior

After verification:

- the user logs in with the existing `/api/auth/login/` endpoint;
- JWT access and refresh tokens are issued normally;
- the response continues to provide `role`, `dashboard`, and `user`;
- moderator and administrator roles remain trusted database values;
- public registration cannot gain privileged access.

### FR-12: Email-address changes

Changing the email of an existing account is not the same as initial signup.
The current re-verification behavior must remain operational until a dedicated
email-change code workflow is designed and implemented. Initial-signup code
verification must not silently break profile email changes.

## 9. Proposed API contract

### 9.1 Start registration

`POST /api/auth/register/`

Request:

```json
{
  "username": "ali_hassan",
  "email": "ali@example.com",
  "password": "StrongPassword!123"
}
```

Success: `202 Accepted`

```json
{
  "registration_id": "89df2cc8-6df9-4dcb-b9ec-89f869a7d291",
  "email": "a**@example.com",
  "detail": "A six-digit verification code has been sent.",
  "expires_in_seconds": 600,
  "resend_available_in_seconds": 60
}
```

The response must never contain the password, password hash, code, or code hash.

### 9.2 Verify registration code

`POST /api/auth/verify-email/`

Request:

```json
{
  "registration_id": "89df2cc8-6df9-4dcb-b9ec-89f869a7d291",
  "code": "042731"
}
```

Success: `201 Created`

```json
{
  "detail": "Email verified and account created successfully.",
  "user": {
    "id": 15,
    "username": "ali_hassan",
    "email": "ali@example.com",
    "role": "user",
    "is_email_verified": true
  },
  "login_required": true
}
```

### 9.3 Resend registration code

`POST /api/auth/verify-email/resend/`

Preferred request:

```json
{
  "registration_id": "89df2cc8-6df9-4dcb-b9ec-89f869a7d291"
}
```

Success: `200 OK`

```json
{
  "detail": "If the registration is eligible, a new verification code has been sent.",
  "resend_available_in_seconds": 60
}
```

### 9.4 Registration status

`GET /api/auth/registrations/{registration_id}/status/`

Success: `200 OK`

```json
{
  "status": "pending",
  "email": "a**@example.com",
  "expires_in_seconds": 418,
  "resend_available_in_seconds": 0,
  "attempts_remaining": 4
}
```

This endpoint must expose no password, code, or internal database information.

## 10. Error contract

| Situation | HTTP status | Public response |
|---|---:|---|
| Invalid signup field | 400 | Field-specific validation message |
| Existing permanent username/email | 400 | Field-specific conflict message |
| Invalid registration ID | 400 | `Invalid or expired verification request.` |
| Incorrect code | 400 | `Incorrect verification code.` plus attempts remaining |
| Expired code | 400 | `The verification code has expired. Request a new code.` |
| Locked code | 423 | `Too many incorrect attempts. Request a new code.` |
| Resend cooldown active | 429 | Retry time without revealing private data |
| Hourly resend limit reached | 429 | Generic rate-limit response |
| Email delivery unavailable | 503 | Retry-safe service-unavailable response |
| Username/email claimed before verification | 409 | Prompt user to restart registration |
| Already consumed registration | 200 or 409 | Idempotent account-already-created response |

Errors must not include SMTP credentials, Redis details, stack traces, code
hashes, or internal exception messages.

## 11. Proposed database design

### PendingRegistration

| Field | Suggested type | Notes |
|---|---|---|
| `id` | BigAutoField | Internal primary key |
| `public_id` | UUIDField | Public opaque identifier, unique and indexed |
| `username` | CharField | Normalized proposed username |
| `email` | EmailField | Normalized proposed email |
| `password_hash` | CharField(128) | Result of Django `make_password` |
| `code_hash` | CharField(128) | Result of Django `make_password` |
| `status` | CharField | pending, consumed, expired, locked, cancelled |
| `failed_attempts` | PositiveSmallIntegerField | Atomic counter |
| `resend_count` | PositiveSmallIntegerField | Current resend-window count |
| `resend_window_started_at` | DateTimeField | Hourly-window anchor |
| `code_expires_at` | DateTimeField | Code expiration |
| `resend_available_at` | DateTimeField | Cooldown boundary |
| `consumed_at` | DateTimeField, nullable | Successful creation timestamp |
| `created_at` | DateTimeField | Creation timestamp |
| `updated_at` | DateTimeField | Last modification timestamp |

Recommended constraints and indexes:

- unique `public_id`;
- case-insensitive active-pending constraint for email;
- case-insensitive active-pending constraint for username;
- index on `status` and `code_expires_at`;
- index on normalized email for resend lookup;
- database transaction and final permanent-user uniqueness check.

The migration must be reviewed against PostgreSQL, which is the target database.

## 12. Security and privacy requirements

- Never store or log plaintext passwords.
- Never store or log plaintext verification codes.
- Never return codes through an API response in production.
- Never include codes in URLs, analytics, exception monitoring, or audit
  details.
- Use Django password hashing for both pending passwords and codes.
- Use constant-time hash verification.
- Use UUID registration identifiers to avoid sequential-ID enumeration.
- Apply DRF scoped throttles to registration, verification, status, and resend
  endpoints.
- Apply additional per-email and per-IP limits in the service layer.
- Invalidate the previous code on resend.
- Use database row locks to prevent duplicate account creation during concurrent
  verification requests.
- Keep SMTP credentials only in environment variables.
- Require HTTPS in production.
- Mask email addresses in public pending-registration responses.
- Do not allow public registration to choose a privileged role.
- Record safe audit details such as outcome and reason category, not secrets.

## 13. Email content requirement

```text
Subject: Your FaceCloud verification code

Welcome to FaceCloud.

Your verification code is:

042731

This code expires in 10 minutes. Do not share it with anyone.

If you did not request a FaceCloud account, you can ignore this email.
```

Production should provide both plain-text and HTML versions with an accessible,
responsive layout.

## 14. Frontend requirements

### Signup screen

- Username, email, password, and password-confirmation fields
- Inline backend validation messages
- Loading state that prevents duplicate submissions
- Transition to verification only after `202 Accepted`

### Verification screen

- Six individual visual positions or one accessible numeric input
- Numeric keyboard on supported mobile devices
- Paste support for the complete six-digit code
- Visible masked email address
- Expiry countdown
- Resend countdown
- Attempts-remaining feedback
- Submit loading and error states
- Change-details action that returns to signup

### Completion behavior

- Show a successful account-created message
- Clear pending registration data from browser storage
- Redirect to login
- Do not treat registration as an authenticated session

The frontend may store the opaque `registration_id` temporarily but must never
store the password or code after submission.

## 15. Observability and audit requirements

Record metrics for:

- registration requests;
- verification emails queued, sent, retried, and failed;
- verification successes;
- incorrect-code attempts;
- expirations;
- locked registrations;
- resend requests and throttling;
- pending-to-created conversion rate;
- average time from signup request to verification.

Safe authentication events should distinguish:

- `registration_pending`
- `verification_code_sent`
- `verification_code_failed`
- `registration_expired`
- `registered`
- `email_verified`

Sensitive values must be excluded from event details.

## 16. Testing requirements

### Serializer and validation tests

- Valid username, email, and strong password
- Email normalization
- Case-insensitive permanent-user conflicts
- Weak password rejection
- Role injection ignored or rejected
- Code must be exactly six numeric digits

### Service tests

- Secure code generation, including leading zeroes
- Password and code stored only as hashes
- Correct expiry and cooldown timestamps
- Resend invalidates the previous code
- Email content contains the code and expiry
- Email failure does not create a permanent user

### API tests

- Registration returns `202` and creates no `User`
- Correct code creates one verified normal user
- Incorrect code does not create a user
- Expired code does not create a user
- Maximum attempts lock the code
- Resend cooldown and hourly limit
- Concurrent verification creates only one user
- Repeated successful verification is idempotent
- Username/email race returns a safe conflict
- Login succeeds only after account creation
- Existing JWT, role-routing, reset, logout, and admin tests remain green

### Privacy tests

- Another registration ID cannot reveal private signup data
- Responses never contain password/code hashes
- Logs and authentication events never contain passwords or codes
- Resend response does not enumerate arbitrary email addresses

### Required regression commands

```powershell
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py test accounts --verbosity 2
python manage.py test --keepdb --verbosity 1
```

## 17. Acceptance criteria

- [ ] A valid signup request sends exactly one six-digit code.
- [ ] No permanent `User` exists before successful code verification.
- [ ] Password and code are never stored in plaintext.
- [ ] The correct unexpired code creates one verified `user`.
- [ ] Incorrect, expired, locked, and replaced codes cannot create accounts.
- [ ] Five failed attempts lock the current code by default.
- [ ] Resend observes a 60-second cooldown and hourly limit by default.
- [ ] Concurrent verification cannot create duplicate users.
- [ ] Public registration cannot create a moderator or administrator.
- [ ] The user can log in through the existing JWT endpoint after verification.
- [ ] Email-change re-verification behavior remains supported.
- [ ] All authentication and complete backend tests pass.
- [ ] API and feature documentation reflect the implemented contract.

## 18. Implementation sequence

### Part 1: Configuration and data model

- Add environment-backed verification settings.
- Add `PendingRegistration` and its migration.
- Add safe cleanup rules and indexes.

### Part 2: Domain services

- Add secure code generation and hashing.
- Add pending-registration creation/replacement.
- Add email composition and delivery.
- Add transactional verification and user creation.
- Add resend and cleanup services.

### Part 3: API layer

- Replace immediate-user registration behavior.
- Add verification request serializer and POST endpoint.
- Replace the current resend behavior for initial signup.
- Add the safe registration-status endpoint.
- Keep legacy email-change verification isolated until migrated.

### Part 4: Abuse prevention and observability

- Add verification scopes and per-identity limits.
- Add safe audit events and metrics.
- Add cleanup task or management command.

### Part 5: Automated tests

- Add unit, API, concurrency, privacy, and email tests.
- Update existing link-verification tests.
- Run the full regression suite.

### Part 6: Frontend integration

- Build the six-digit verification screen.
- Implement countdown, resend, paste, and error states.
- Connect completion to the existing login screen.

### Part 7: Documentation and release

- Update Feature 01 documentation and API examples.
- Update `.env.example`.
- Document SMTP and Celery deployment requirements.
- Deploy behind a controlled release and monitor verification failures.

## 19. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Email arrives late | Ten-minute expiry, resend, delivery metrics |
| Brute-force guessing | Five-attempt limit, hashing, throttles, cooldown |
| Duplicate creation race | Row lock, atomic transaction, final uniqueness check |
| Pending username/email abuse | Expiry, rate limits, cleanup, active-pending constraints |
| SMTP outage | Retry-safe delivery, clear 503 response, monitoring |
| Breaking profile email changes | Keep that workflow isolated and cover it with regression tests |
| Frontend uses old verification link | Coordinate API version and frontend release |

## 20. Definition of done

The feature is complete only when the backend implementation, migrations,
security controls, frontend integration, email delivery configuration,
automated tests, documentation, and deployment monitoring all satisfy this
PRD. Creating the model or endpoint alone does not mark the feature complete.
