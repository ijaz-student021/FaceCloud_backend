# FaceCloud documentation

Everything about the backend, grouped by what you want to do. Start with the section that matches your question.
The repository [README](../README.md) explains what FaceCloud is and how to run it.

## Start here

| I want to | Read |
|---|---|
| See the whole project as submitted | [`report/FaceCloud_Documentation.pdf`](report/FaceCloud_Documentation.pdf) ([about the report](report/README.md)) |
| Know what is done, what works and what is open | [`PROJECT_STATUS_2026-11-25.md`](PROJECT_STATUS_2026-11-25.md) (with a 2026-11-26 update) and [`FEATURES.md`](FEATURES.md) |
| Run the defence demo | [`demo/defense-demo.md`](demo/defense-demo.md), [`DEFENSE_DATA_AND_LOGS_GUIDE.md`](DEFENSE_DATA_AND_LOGS_GUIDE.md), [`DEFENSE_READINESS_REPORT.md`](DEFENSE_READINESS_REPORT.md) |
| Deploy or operate the live server | [`deployment/VPS_DEPLOYED_STATE.md`](deployment/VPS_DEPLOYED_STATE.md), [`deployment/VPS_RUNBOOK.md`](deployment/VPS_RUNBOOK.md) |
| Call the API from a frontend | [`FRONTEND_API_GUIDE.md`](FRONTEND_API_GUIDE.md), [`API_ERRORS.md`](API_ERRORS.md), [`api/API_REFERENCE.md`](api/API_REFERENCE.md) |

## Architecture and decisions

| Document | Contents |
|---|---|
| [`structured/prd.md`](structured/prd.md) | Product requirements |
| [`structured/architecture.md`](structured/architecture.md) | System architecture |
| [`structured/decisions.md`](structured/decisions.md) | Decision log |
| [`structured/tasks.md`](structured/tasks.md) | Engineering task plan and status |
| [`structured/agents.md`](structured/agents.md) | Engineering roles and responsibilities |
| [`ORGANIZATIONS_AND_REVIEW_PROCESS.md`](ORGANIZATIONS_AND_REVIEW_PROCESS.md) | Organizations, moderators and the photo review process (Roman Urdu) |
| [`SECURITY_AND_PERFORMANCE_REVIEW.md`](SECURITY_AND_PERFORMANCE_REVIEW.md) | Dependency audit, static analysis, performance findings |
| [`ENGINEERING_REPORT_2026-10-15.md`](ENGINEERING_REPORT_2026-10-15.md) | Engineering report of 2026-10-15 |

## Features

[`FEATURES.md`](FEATURES.md) is the authoritative catalogue. Each feature has a document in [`features/`](features/) with what was built,
how it was verified and its known limits, for example
[authentication](features/01-authentication-and-authorization.md),
[upload and storage](features/02-image-upload-and-secure-storage.md),
[enhancement](features/10-12-image-enhancement.md),
[identity grouping](features/18-face-recognition-and-identity-grouping.md),
[moderator and organization panel](features/27-31-46-moderator-organization-panel.md),
[privacy and facial data](features/41-privacy-and-facial-data.md),
[stored-image encryption](features/stored-image-encryption.md) and
[the enhanced-image database copy](features/enhanced-image-database-copy.md).
The product plan for the Google-Photos-style features is in [`product/google-photos-parity/`](product/google-photos-parity/PRD.md).

## Enhancement and models

| Document | Contents |
|---|---|
| [`enhancement/adaptive-engine.md`](enhancement/adaptive-engine.md) | The adaptive engine: analyse, route, validate, return one image or keep the original |
| [`enhancement/lightweight-pipeline.md`](enhancement/lightweight-pipeline.md) | The OpenCV and Pillow baseline pipeline |
| [`enhancement/optional-models.md`](enhancement/optional-models.md) | The learned models and how they are switched on |
| [`enhancement/cloud-auto-enhance.md`](enhancement/cloud-auto-enhance.md) | Pro: the cloud enhancement and its safety checks |
| [`enhancement/dashboard-sliders.md`](enhancement/dashboard-sliders.md) | Editor sliders |
| [`enhancement/openai-advisor.md`](enhancement/openai-advisor.md) | Optional cloud adjustment advisor |
| [`enhancement/retired-capabilities.md`](enhancement/retired-capabilities.md) | Capabilities that were removed, and why |
| [`deployment/restoration-chain.md`](deployment/restoration-chain.md) | Deblur, face restoration and upscaling to HD |
| [`model-licenses/`](model-licenses/model-set-2026-10.md) | Licence and provenance of every model ([inventory](model-licenses/commercial-inventory-2026-10.md)) |
| [`DATASET_TERMS.md`](DATASET_TERMS.md) | The third-party face dataset, its conditions and how they are kept |
| [`../model_evaluation/`](../model_evaluation/README.md) | Per-model evaluation notebooks and scorecard (no model was fine-tuned) |

## Evaluation evidence

[`evaluation/`](evaluation/) holds one dated report per experiment (blur measure, low-light, denoising, identity thresholds, small faces, group photographs and more).
The file name starts with the date. Raw results are under [`evaluation/evidence/`](evaluation/evidence).
GPU preparation: [`GPU_SESSION_RUNBOOK.md`](GPU_SESSION_RUNBOOK.md), [`gpu/kaggle-face-bakeoff.md`](gpu/kaggle-face-bakeoff.md).

## Deployment and operations

| Document | Contents |
|---|---|
| [`deployment/VPS_DEPLOYED_STATE.md`](deployment/VPS_DEPLOYED_STATE.md) | What is deployed where, how to deploy backend and frontend, measured timings |
| [`deployment/VPS_RUNBOOK.md`](deployment/VPS_RUNBOOK.md) | Step-by-step VPS set-up and maintenance |
| [`deployment/vps.env.example`](deployment/vps.env.example) | Environment variable names for the VPS (no values) |
| [`deployment/smtp-email.md`](deployment/smtp-email.md) | Sending e-mail |
| [`deployment/rate-limits.md`](deployment/rate-limits.md) | Rate limits |
| [`deployment/one-database.md`](deployment/one-database.md) | One database for everything |
| [`deployment/merge-and-migration-plan.md`](deployment/merge-and-migration-plan.md) | Merge order, migrations, backup and restore |
| [`deployment/organization-offboarding.md`](deployment/organization-offboarding.md) | Removing an organization |
| [`deployment/aws-s3-private-storage.md`](deployment/aws-s3-private-storage.md), [`ec2-small-cpu.md`](deployment/ec2-small-cpu.md), [`local-staging.md`](deployment/local-staging.md), [`hostinger-readiness-2026-10.md`](deployment/hostinger-readiness-2026-10.md) | Other targets that were assessed |
| [`MANUAL_TESTING_COMMANDS.md`](MANUAL_TESTING_COMMANDS.md) | Commands for testing by hand |

## Frontend and project planning

| Document | Contents |
|---|---|
| [`alignment/frontend-handoff.md`](alignment/frontend-handoff.md) | Backend contracts ready for the frontend |
| [`alignment/backend-alignment-tracker.md`](alignment/backend-alignment-tracker.md) | Backend alignment tracker |
| [`alignment/user-dashboard.md`](alignment/user-dashboard.md), [`moderator-console.md`](alignment/moderator-console.md), [`admin-dashboard.md`](alignment/admin-dashboard.md) | One page per dashboard |
| [`USER_DASHBOARD_REMAINING_FEATURES.md`](USER_DASHBOARD_REMAINING_FEATURES.md) | Features that were still open on the user dashboard |
| [`12_DAY_COMPLETION_PLAN.md`](12_DAY_COMPLETION_PLAN.md) | Delivery plan |

## Where things are in the repository

| Path | Contents |
|---|---|
| Django apps (`accounts/`, `uploads/`, `enhancement/`, ...) | The backend; the [README](../README.md#layout) lists each one |
| [`scripts/`](../scripts) | Deploy, demo, evaluation and model scripts |
| [`model_evaluation/`](../model_evaluation) | Evaluation notebooks and outputs |
| [`notebooks/`](../notebooks) | Model training notebooks |
| `docs/` | This documentation |
| `.env.example`, `.env.demo.example`, `.env.staging.example` | Environment variable names (never real values) |
