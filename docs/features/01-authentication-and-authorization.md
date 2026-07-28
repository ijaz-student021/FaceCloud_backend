# Feature 01: Authentication, Account Management, and Authorization

## 1. Overview

This feature establishes the security foundation for FaceCloud. It identifies users through verified accounts and JWT tokens, controls access through trusted database roles, and provides account lifecycle operations such as password reset, suspension, logout, and deletion.

The backend supports three roles: user, moderator, and administrator. A public registration always creates a normal user. Privileged roles can only be assigned by an authorized administrator.

## 2. Implemented requirements

- Registration with username, required email, and password
- Case-insensitive username and email uniqueness validation
- Django password validators and secure password hashing
- Six-digit signup verification with pending registrations
- Verification-code expiry, attempt locking, resend cooldown, and hourly limits
- Login using username, email, or a common identifier field
- JWT access and refresh tokens
- Refresh-token rotation and blacklisting
- Token revocation after password, email, suspension, or role changes
- Profile retrieval and editing
- Password change and password reset
- Current-device and all-device logout
- Account deletion with password confirmation
- User, moderator, and administrator roles
- Administrator user listing, role management, suspension, and reactivation
- Authentication activity records
- Request throttling for sensitive public endpoints

## 3. Architecture and workflow

```text
Registration
  -> serializer validation
  -> pending password and six-digit code hashes
  -> verification-code email
  -> locked pending-row verification
  -> atomic verified user creation
  -> pending secrets cleared

Login
  -> username/email lookup
  -> password verification
  -> active/verified/suspension checks
  -> JWT access and refresh tokens
  -> trusted role and dashboard response

Protected request
  -> JWT signature and expiry validation
  -> user lookup
  -> verification and suspension checks
  -> token-version check
  -> endpoint permission check
```

## 4. Database design

### User

Extends Django's `AbstractUser` with:

- `email`: required and unique
- `role`: user, moderator, or admin
- `is_email_verified`: verification state
- `is_suspended`: account suspension state
- `suspension_reason`: administrator-provided explanation
- `token_version`: invalidates previously issued access and refresh tokens

### AuthEvent

Stores security-relevant events:

- Related user, nullable after deletion
- Event type
- Email or login identifier
- IP address where available
- Structured event details
- Creation timestamp

### PendingRegistration

Stores temporary signup state until email ownership is proven:

- Opaque UUID registration ID
- Normalized username and email
- Django password hash and verification-code hash
- Expiry, resend cooldown, attempt, and hourly-send limits
- Pending, locked, expired, cancelled, or consumed status
- Optional link to the final user for idempotent verification

No permanent `User` exists before successful code verification. Final user
creation, pending-record consumption, and audit events run in one
`transaction.atomic()` database transaction.

The account schema is introduced by migrations `accounts.0001` through
`accounts.0004`.

## 5. API endpoints

### Public authentication endpoints

| Method | Endpoint | Purpose |
|---|---|---|
| POST | `/api/auth/register/` | Start pending registration and email a six-digit code |
| POST | `/api/auth/login/` | Login using username, email, or identifier |
| POST | `/api/auth/refresh/` | Rotate tokens and return a new access token |
| POST | `/api/auth/verify-email/` | Verify a signup code and atomically create the user |
| POST | `/api/auth/verify-email/resend/` | Replace and resend a signup code |
| GET | `/api/auth/registrations/{registration_id}/status/` | Read masked pending status |
| GET | `/api/auth/verify-email/change/{uid}/{token}/` | Verify an existing user's changed email |
| POST | `/api/auth/password-reset/` | Request password-reset email |
| POST | `/api/auth/password-reset/confirm/` | Set a new password using a valid token |

### Authenticated account endpoints

| Method | Endpoint | Purpose |
|---|---|---|
| GET/PATCH | `/api/user/profile/` | View or update profile |
| POST | `/api/user/change-password/` | Change password |
| POST | `/api/user/logout/` | Blacklist one refresh token |
| POST | `/api/user/logout-all/` | Revoke all existing tokens |
| DELETE | `/api/user/delete-account/` | Delete account after password confirmation |
| GET | `/api/user/activity/` | View recent authentication events |

### Administrator endpoints

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/api/user/admin/users/` | List users |
| PATCH | `/api/user/admin/users/{id}/` | Change role, suspension, or active state |

## 6. Login request and response

Login accepts exactly one identifier field:

```json
{
  "email": "user@example.com",
  "password": "StrongPassword!123"
}
```

The response provides the frontend routing decision from the trusted stored role:

```json
{
  "access": "access-token",
  "refresh": "refresh-token",
  "role": "user",
  "dashboard": "user",
  "user": {
    "id": 3,
    "username": "example",
    "email": "user@example.com",
    "role": "user",
    "is_email_verified": true
  }
}
```

The frontend must redirect using `dashboard`, but protected backend endpoints still enforce authorization independently.

## 7. Main implementation files

- `accounts/models.py`: user extensions and authentication events
- `accounts/serializers.py`: validation, login, refresh, profile, password, logout, deletion, and admin inputs
- `accounts/authentication.py`: verification, suspension, and token-version enforcement
- `accounts/permissions.py`: administrator and moderator permission classes
- `accounts/services.py`: email generation and activity recording
- `accounts/views.py`: authentication and account API behavior
- `accounts/urls.py`: public authentication routes
- `accounts/user_urls.py`: protected account and administrator routes
- `accounts/admin.py`: custom user and event administration

## 8. Security controls

- Passwords are never stored in plain text.
- Signup codes are generated with `secrets` and stored only as Django hashes.
- Permanent users are created only after verified pending rows are locked.
- Final user creation and pending consumption are atomic.
- Public registration cannot select a moderator or administrator role.
- Email and username duplicates are checked case-insensitively.
- Unverified, suspended, and inactive users cannot log in.
- Suspended users cannot refresh tokens or use existing access tokens.
- Refresh tokens rotate and old refresh tokens are blacklisted.
- Token versions revoke all earlier tokens after sensitive changes.
- Password reset responses do not reveal whether an email exists.
- Sensitive endpoints have scoped throttling.
- Administrators cannot remove their own access accidentally.
- Users see only their own authentication activity.

## 9. Configuration

Important environment settings include:

- `DJANGO_SECRET_KEY`
- `DJANGO_DEBUG`
- `DJANGO_ALLOWED_HOSTS`
- `PUBLIC_BACKEND_URL`
- `FRONTEND_URL`
- SMTP email settings
- `SIGNUP_VERIFICATION_CODE_LIFETIME_SECONDS`
- `SIGNUP_VERIFICATION_RESEND_COOLDOWN_SECONDS`
- `SIGNUP_VERIFICATION_MAX_ATTEMPTS`
- `SIGNUP_VERIFICATION_MAX_SENDS_PER_HOUR`
- `SIGNUP_VERIFICATION_RETENTION_HOURS`
- `SIGNUP_VERIFICATION_SUPPORT_EMAIL`
- CORS origins
- HTTPS, secure-cookie, and HSTS settings

Development uses the console email backend by default. Production requires real SMTP credentials and HTTPS-safe values from `.env.example`.

## 10. Automated tests

The authentication suite contains 14 test methods covering registration, password validation, email verification, username/email login, role-based dashboard values, suspension, token refresh, profile changes, password reset, logout, account deletion, and administrator permissions.

```text
Command: python manage.py test accounts --verbosity 2
Latest result: Passed
Combined authentication/upload suite: 29 tests passed
Verification date: 2026-07-14
```

## 11. Frontend integration

The frontend must provide:

- Registration, verification, login, reset, and profile forms
- Secure token storage strategy
- Access-token refresh handling
- Redirect based on the `dashboard` response
- User, moderator, and administrator route guards
- Logout and session-expiry behavior
- Clear validation and suspension messages

Frontend route guards improve user experience but do not replace backend permissions.

## 12. Limitations and later integration

- Real SMTP delivery requires deployment credentials.
- The visual dashboards belong to the frontend project.
- Moderator permissions exist, while moderator review endpoints belong to the later review-system feature.
- Image and processing events will expand the activity timeline in later features.

## 13. Completion checklist

- [x] Implementation complete
- [x] Migrations applied
- [x] Role permissions verified
- [x] Automated tests passed
- [x] Django system check passed
- [x] API contracts documented
- [x] Manual registration, login, token, and profile flow verified
