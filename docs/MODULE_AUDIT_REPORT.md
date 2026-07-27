# FaceCloud Backend Module Audit Report

**Date:** 2026-07-27

This report audits the requested modules against the current workspace code, tests, and runtime configuration. Status labels used below:

- Done and tested
- Implemented but untested
- Partially implemented
- Not started
- Could not verify

## 1. AUTH

**Status:** Done and tested

**Evidence:**

- [accounts/models.py](../accounts/models.py): `User`, `AuthEvent`
- [accounts/permissions.py](../accounts/permissions.py): `IsAdminRole`, `IsModeratorOrAdmin`
- [accounts/serializers.py](../accounts/serializers.py): `RegisterSerializer`, `FaceCloudTokenObtainPairSerializer.validate`, `FaceCloudTokenRefreshSerializer.validate`, `DeleteAccountSerializer.validate_password`, `AdminUserSerializer`
- [accounts/views.py](../accounts/views.py): `RegisterView`, `FaceCloudTokenObtainPairView`, `FaceCloudTokenRefreshView`, `VerifyEmailView`, `ResendVerificationView`, `ProfileView`, `LogoutView`, `LogoutAllView`, `DeleteAccountView`, `AuthEventListView`, `AdminUserListView`, `AdminUserUpdateView`
- [accounts/urls.py](../accounts/urls.py), [accounts/user_urls.py](../accounts/user_urls.py)
- [config/settings.py](../config/settings.py): `REST_FRAMEWORK`, `SIMPLE_JWT`
- [accounts/tests.py](../accounts/tests.py): `AuthenticationTests.test_email_verification_enables_login`, `test_login_accepts_email_and_returns_user_dashboard`, `test_login_returns_dashboard_from_trusted_role`, `test_only_admin_can_manage_roles_and_suspension`, `test_delete_account_requires_password`

**Role enforcement check:**

Yes. Access restriction is not just a stored role field. `IsAdminRole.has_permission` explicitly checks `request.user.role == "admin"` or `is_superuser`, and `AdminUserUpdateView` blocks self-demotion/suspension. `FaceCloudTokenObtainPairSerializer.get_dashboard` also branches on the live role value.

**Gaps:**

- No material backend gap in this module was found. The main remaining work is product-level, not auth plumbing.

## 2. UPLOADS & CORE PIPELINE

**Status:** Done and tested

**Evidence:**

- [uploads/models.py](../uploads/models.py): `UploadBatch`, `ImageUpload`, `ImagePreprocessingResult`, `BatchImageProgress`
- [uploads/validators.py](../uploads/validators.py): `validate_image_file`, `validate_zip_archive`, `safe_original_name`
- [uploads/services.py](../uploads/services.py): `create_image_batch`, `create_zip_batch`, `preprocess_image`, `enqueue_batch_preprocessing`, `delete_batch`
- [uploads/progress.py](../uploads/progress.py): `sync_image_progress`, `recompute_batch_progress`, `batch_progress_payload`
- [uploads/views.py](../uploads/views.py): `SingleImageUploadView`, `MultipleImageUploadView`, `ZipImageUploadView`, `UploadBatchListView`, `UploadBatchDetailView`, `UploadBatchProgressView`, `UploadBatchRetryView`, `UploadBatchCancelView`, `ImageUploadListView`, `MyImageListView`, `ImageDownloadView`, `ImagePreprocessView`, `ImagePreprocessingDetailView`, `ImagePreprocessingDownloadView`
- [uploads/urls.py](../uploads/urls.py)
- [uploads/tests.py](../uploads/tests.py): `ImageUploadTests.test_valid_single_image_upload_stores_secure_metadata`, `test_batch_progress_is_persistent_and_owner_scoped`, `test_multiple_upload_keeps_valid_files_and_rejects_invalid_files`, `test_zip_upload_accepts_images_and_records_unsupported_files`, `test_preprocessing_status_download_and_cross_user_access`, `test_batch_reports_background_processing_failure`

**Gaps:**

- Storage is implemented on the Django file system backend, not a private cloud object store. External storage migration remains a future capability.
- No material backend gap was found in the batch/progress pipeline itself.

## 3. ENHANCEMENT PIPELINE

**Status:** Partially implemented

**Evidence:**

- [enhancement/models.py](../enhancement/models.py): `EnhancementBatchJob`, `ImageEnhancementResult`, `EnhancementLevel`, `EnhancementStatus`
- [enhancement/services.py](../enhancement/services.py): `analyze_image_quality`, `compare_image_quality`, `_read_full_resolution_original`, `_read_enhancement_source`, `exposure_enhancement_stage`, `output_guard_a_stage`, `measure_post_exposure_noise_stage`, `adaptive_denoise_stage`, `low_resolution_restoration_stage`, `restore_degraded_faces`, `face_restoration_stage`, `output_guard_b_stage`, `enhance_bgr`, `create_enhancement_result`, `process_enhancement_result`, `apply_gallery_adjustments`
- [enhancement/views.py](../enhancement/views.py): `ImageEnhanceView`, `BatchEnhanceView`, `EnhancementResultListView`, `EnhancementResultDetailView`, `EnhancementJobListView`, `EnhancementJobDetailView`, `EnhancementDownloadView`
- [enhancement/tasks.py](../enhancement/tasks.py): `process_enhancement_image`, `process_enhancement_batch`
- [enhancement/tests.py](../enhancement/tests.py): `OpenCVPolicyTests.test_pipeline_exposes_stable_stage_order_and_timings`, `test_adaptive_denoise_uses_post_exposure_noise_mapping`, `test_edsr_runs_after_denoise_for_still_low_resolution_image`, `test_low_resolution_image_is_restored_at_two_times_scale`, `test_processing_creates_separate_enhanced_output`, `test_owner_receives_normalized_comparison_contract`, `test_batch_api_processes_ready_images_and_reports_progress`
- [config/settings.py](../config/settings.py): `ADAPTIVE_DENOISE_ENABLED`, `GFPGAN_ENABLED`, `GFPGAN_QUEUE_ENABLED`, `PER_FACE_METRICS_ENABLED`, `IDENTITY_EMBEDDING_ENABLED`, `ENHANCEMENT_PRESERVE_ORIGINAL_RESOLUTION`

**Pipeline answers requested:**

- Full-resolution processing: implemented via `_read_full_resolution_original` and `_read_enhancement_source`.
- Quality analysis: implemented via `analyze_image_quality` and `compare_image_quality`.
- Adaptive Zero-DCE: implemented via `exposure_enhancement_stage` and `enhance_bgr`; it is only used for low-light cases when enabled.
- EDSR position: runs after adaptive denoise in `enhance_bgr`, specifically in `low_resolution_restoration_stage` after `adaptive_denoise_stage`.
- Denoising strategy: adaptive when the feature flag is enabled, using post-exposure noise from `measure_post_exposure_noise_stage`; otherwise it falls back to legacy fixed policy values.
- Per-face metrics: not implemented in the pipeline. Only the feature flag exists.
- GFPGAN status: guarded and feature-flagged. It is present in `restore_degraded_faces` and `face_restoration_stage`, but remains disabled by default and is not the primary route.
- Two-point quality guard: implemented as `output_guard_a_stage` and `output_guard_b_stage`.
- Identity-safe embedding source contract: not implemented as a real embedding pipeline. The code contains an explicit placeholder string in `restore_degraded_faces` saying `embedding verification pending`.

**Gaps:**

- No actual per-face metric calculation pipeline was found.
- No verified identity-embedding extraction or contract implementation was found.
- GFPGAN is present, but it remains opt-in and guarded, not a fully universal pipeline stage.

## 4. IDENTITY GROUPING

**Status:** Not started

**Evidence:**

- Workspace searches found no `PersonGroup` model file or identity-grouping module.
- [config/settings.py](../config/settings.py): `IDENTITY_EMBEDDING_ENABLED` exists, but it is disabled by default.
- [config/urls.py](../config/urls.py) does not mount any identity-grouping route.

**Gaps:**

- No `PersonGroup` model.
- No group management endpoints for rename, merge, move, delete, or set-profile.
- No embedding extraction or clustering logic.

## 5. MODERATION

**Status:** Not started

**Evidence:**

- Workspace searches found no `ModeratorReview` model file and no moderation queue module.
- [config/urls.py](../config/urls.py) does not mount moderator review routes.

**Gaps:**

- No `ModeratorReview` model.
- No review-trigger logic.
- No moderator queue endpoint.
- No approve/reject decision endpoint.

## 6. ADMIN TOOLS

**Status:** Partially implemented

**Evidence:**

- [accounts/views.py](../accounts/views.py): `AdminUserListView`, `AdminUserUpdateView`, `AuthEventListView`
- [accounts/user_urls.py](../accounts/user_urls.py): `admin/users/`, `admin/users/<int:pk>/`, `activity/`
- [accounts/tests.py](../accounts/tests.py): `AuthenticationTests.test_only_admin_can_manage_roles_and_suspension`
- [config/urls.py](../config/urls.py) mounts accounts and user routes but no analytics or health routes

**Gaps:**

- No analytics endpoint was found; no real-query analytics surface exists.
- No system health endpoint was found.
- Audit logging is implemented as `AuthEvent` plus `AuthEventListView`, but there is no broader admin analytics/dashboard layer.
- User management endpoints exist, so this module is only partially missing, not empty.

## 7. SHARING & REPORTS

**Status:** Partially implemented

**Evidence:**

- [reports/models.py](../reports/models.py): `ProcessingReport`
- [reports/views.py](../reports/views.py): `ImageReportCreateView`, `BatchReportCreateView`, `ProcessingReportListView`, `ProcessingReportDetailView`, `ProcessingReportDownloadView`, `ProcessingReportRegenerateView`
- [reports/tasks.py](../reports/tasks.py): report-generation worker task
- [reports/tests.py](../reports/tests.py): `ProcessingReportTests.test_json_report_generation_stores_immutable_snapshot`, `test_csv_report_has_bom_and_blocks_spreadsheet_formula_injection`, `test_pdf_report_is_valid_pdf_document`, `test_batch_report_contains_all_images_and_status_summary`, `test_create_endpoint_queues_report_and_reuses_active_request`, `test_ready_report_download_and_unready_download_conflict`
- [downloads/models.py](../downloads/models.py): `DownloadPackage`, `DownloadEvent` for package delivery and history

**Formats actually implemented:**

- JSON
- CSV
- PDF

**Gaps:**

- No share-link model or share-link endpoint was found.
- No model-comparison endpoint was found.
- Reporting itself is implemented; the missing pieces are the sharing layer and comparison workflow.

## 8. SECURITY & INFRASTRUCTURE

**Status:** Done and tested

**Evidence:**

- [config/settings.py](../config/settings.py): `REST_FRAMEWORK`, `CORS_ALLOW_ALL_ORIGINS`, `CORS_ALLOWED_ORIGINS`, `CELERY_BROKER_URL`, `CELERY_RESULT_BACKEND`, `CELERY_TASK_ALWAYS_EAGER`, `CELERY_TASK_ROUTES`, `DATABASES`
- [uploads/validators.py](../uploads/validators.py): `validate_image_file`, `validate_zip_archive`
- [uploads/views.py](../uploads/views.py): `throttle_scope` usage on upload and preprocessing endpoints
- [enhancement/views.py](../enhancement/views.py): `process_enhancement_image.apply_async`, `process_enhancement_batch.apply_async`
- [uploads/services.py](../uploads/services.py): `enqueue_batch_preprocessing` uses `preprocess_batch_task.apply_async`
- [reports/views.py](../reports/views.py): `throttle_scope = "report_generation"`
- [downloads/views.py](../downloads/views.py): `throttle_scope = "download_package"`
- [notifications/views.py](../notifications/views.py): `throttle_scope = "notification_action"`
- Runtime verification from the configured venv showed Django using `django.db.backends.postgresql` with database name `facecloud`

**Gaps:**

- CORS defaults to permissive allow-all behavior unless overridden by environment variables.
- Celery is genuinely wired for async execution, but background jobs still require Redis and a running worker to execute outside eager mode.
- No separate health-check endpoint was found, though the core security controls are present.

## Overall Assessment

The backend is strongest in authentication, uploads, processing, and secure ownership enforcement. Enhancement, reporting, and downloads are substantially implemented, while identity grouping, moderation, analytics, and sharing remain the main unfinished product areas. Infrastructure is mostly production-shaped, with PostgreSQL active in the current runtime configuration and Celery task dispatch already in use.

## Short Conclusion

FaceCloud Backend is past the foundation stage. The codebase shows a real, tested backend implementation with several completed subsystems and a clear remaining roadmap. The main unfinished work is product expansion, not core backend viability.