# Feature NN: Feature Name

## 1. Overview

Describe what the feature does, why it is required, and who uses it.

## 2. Requirements

- Functional requirement
- Security requirement
- Performance requirement
- User or administrator requirement

## 3. Architecture and workflow

Describe the components and the end-to-end data flow.

```text
Input -> Validation -> Processing -> Persistence -> Response
```

## 4. Database design

Document every new or changed model and its important fields and relationships.

## 5. API endpoints

| Method | Endpoint | Access | Purpose |
|---|---|---|---|
| GET | `/api/example/` | Authenticated user | Example |

Include example request and response payloads.

## 6. Main implementation files

- `app/models.py`
- `app/serializers.py`
- `app/views.py`
- `app/services.py`
- `app/urls.py`

Explain the responsibility of each file.

## 7. Validation and security

Explain input validation, permissions, ownership, attack prevention, data privacy, and failure handling.

## 8. Configuration

Document environment variables, defaults, units, and production considerations.

## 9. Error handling

Describe expected validation errors, HTTP status codes, partial failures, and cleanup behavior.

## 10. Automated tests

List the test cases and the latest verified result.

```text
Command:
Result:
Date:
```

## 11. Manual verification

Document a reproducible manual test and its observed result.

## 12. Frontend integration

Describe required forms, fields, token handling, redirects, UI states, and error messages.

## 13. Limitations and future improvements

Record intentional limitations and work assigned to later features.

## 14. Completion checklist

- [ ] Implementation complete
- [ ] Migration applied
- [ ] Permissions verified
- [ ] Automated tests passed
- [ ] Django checks passed
- [ ] API documented
- [ ] Manual verification completed
