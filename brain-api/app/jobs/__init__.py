"""Background jobs (BACKEND_BEST_PRACTICES.md §13).

ARQ task functions live in ``tasks/``; ``worker.py`` holds ``WorkerSettings`` and
runs in a separate process (``arq app.jobs.worker.WorkerSettings``). The API
enqueues through ``queue.enqueue``.
"""
