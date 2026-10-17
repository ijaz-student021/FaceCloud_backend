"""Which Celery queue each inference task goes to.

By default every task uses the default queue, as before. Setting ``ENHANCEMENT_QUEUE_NAME`` sends the tasks that load models
(single and batch enhancement, gallery edits, model comparison) to a queue of their own, so a worker pool sized for model memory
(few processes, no other work, optionally a GPU) can be run apart from the workers that send email or clean up files, and from
the Django API processes. ``GFPGAN_QUEUE_ENABLED`` keeps priority for the two enhancement tasks: GFPGAN needs the isolated
environment of its own worker.
"""
INFERENCE_TASKS = (
    "enhancement.process_image", "enhancement.process_batch", "enhancement.process_gallery_edit", "enhancement.run_model_comparison",
)


def task_routes(enhancement_queue="", gfpgan_enabled=False, gfpgan_queue="gfpgan"):
    routes = {}
    if enhancement_queue:
        routes.update({name: {"queue": enhancement_queue} for name in INFERENCE_TASKS})
    if gfpgan_enabled:
        routes.update({"enhancement.process_image": {"queue": gfpgan_queue}, "enhancement.process_batch": {"queue": gfpgan_queue}})
    return routes
