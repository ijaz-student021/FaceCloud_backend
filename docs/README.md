# Documentation

Start with the [project README](../README.md) (what it does, layout, getting started, testing, deployment). This page says where the rest is.

## Top-level documents

| Document | What it is |
|---|---|
| [`FEATURES.md`](FEATURES.md) | Feature catalogue and status |
| [`FRONTEND_API_GUIDE.md`](FRONTEND_API_GUIDE.md), [`API_ERRORS.md`](API_ERRORS.md) | How a frontend uses the API; the error shapes it can rely on |
| [`DEFENSE_DATA_AND_LOGS_GUIDE.md`](DEFENSE_DATA_AND_LOGS_GUIDE.md) | Where the data is stored (database, files, logs) and how to show it |
| [`DEFENSE_READINESS_REPORT.md`](DEFENSE_READINESS_REPORT.md) | Defence readiness report |
| [`MANUAL_TESTING_COMMANDS.md`](MANUAL_TESTING_COMMANDS.md) | Commands for testing by hand |
| [`SECURITY_AND_PERFORMANCE_REVIEW.md`](SECURITY_AND_PERFORMANCE_REVIEW.md) | Dependency audit, static analysis, performance findings and how to repeat them |
| [`GPU_SESSION_RUNBOOK.md`](GPU_SESSION_RUNBOOK.md) | Preparing, running and judging a GPU evaluation session |
| [`DATASET_TERMS.md`](DATASET_TERMS.md) | The third-party face dataset, its conditions and how the project keeps to them |
| [`ENGINEERING_REPORT_2026-10-15.md`](ENGINEERING_REPORT_2026-10-15.md), [`12_DAY_COMPLETION_PLAN.md`](12_DAY_COMPLETION_PLAN.md), [`USER_DASHBOARD_REMAINING_FEATURES.md`](USER_DASHBOARD_REMAINING_FEATURES.md) | Reports and plans (dated; they describe their own day) |

## Folders

| Folder | What is in it |
|---|---|
| [`features/`](features) | One document per feature: design, verification and limits |
| [`enhancement/`](enhancement) | The enhancement engine: adaptive engine, optional models, retired capabilities |
| [`deployment/`](deployment) | Runbooks (VPS, local staging, S3 private storage, small EC2), example environment files, [real email delivery (SMTP)](deployment/smtp-email.md) |
| [`api/`](api) | `openapi.yaml` and `API_REFERENCE.md` (generated; regenerate them in a separate change) |
| [`alignment/`](alignment) | What the frontend and backend agreed: handoff, dashboards, tracker |
| [`structured/`](structured) | Architecture, decisions, tasks, product requirements |
| [`product/`](product) | Product requirement and implementation plan for the Google-Photos-parity work |
| [`model-licenses/`](model-licenses) | The licence, source and checksum of every model the project uses |
| [`evaluation/`](evaluation) | Dated evaluation reports (benchmarks, calibrations, model runs) |
| [`evidence/`](evidence) | Raw evidence behind some reports: logs, samples, JSON (`fix-validation/`, `audit-output/`), kept out of the project root |
| [`gpu/`](gpu) | GPU model manifest and the Kaggle bake-off |
| [`demo/`](demo) | The defence demo |
| [`images/`](images) | Images used by the documents |
