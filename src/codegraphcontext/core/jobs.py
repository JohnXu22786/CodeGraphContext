# src/codegraphcontext/core/jobs.py
"""
This module defines the data structures and manager for handling long-running,
background jobs, such as code indexing.
"""
import asyncio
import copy
import math
import time
import uuid
import threading
import traceback as traceback_module
from datetime import datetime, timedelta
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Dict, List, Optional
from pathlib import Path


MAX_TRACEBACK_CHARS = 32_768
_JOB_PROGRESS_FIELDS = frozenset(
    {
        "total_files",
        "processed_files",
        "current_file",
        "status_message",
        "estimated_duration",
        "actual_duration",
    }
)
_JOB_COMPLETION_FORBIDDEN_FIELDS = frozenset(
    {
        "job_id",
        "status",
        "start_time",
        "last_update_time",
        "path",
        "is_dependency",
        "timeout_seconds",
        "cancel_requested",
        "cancel_reason",
    }
)


class JobCancelledError(asyncio.CancelledError):
    """Raised at a safe boundary after a job cancellation or timeout request."""


async def _drain_task(task: asyncio.Task) -> None:
    """Wait for a shielded task to finish despite repeated caller cancellation."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
        except BaseException:
            break

    try:
        task.result()
    except BaseException:
        pass


async def wait_task_until_stopped(task: asyncio.Task) -> None:
    """Wait for an existing task to finish despite repeated caller cancellation."""
    await _drain_task(task)


async def run_blocking_until_stopped(
    func, *args, on_cancel: Optional[Callable[[], Any]] = None, **kwargs
):
    """Run blocking work in a thread and drain it before propagating cancellation.

    Cancelling an ``asyncio.to_thread`` await does not stop its worker thread.
    Database calls therefore have to finish before their caller can safely
    clean up an index or report that cancellation is complete. ``on_cancel``
    runs before that drain so a queued worker sees the stop request before it
    can begin a final write.
    """
    task = asyncio.create_task(asyncio.to_thread(func, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            if on_cancel is not None:
                on_cancel()
        finally:
            await wait_task_until_stopped(task)
        raise


class JobStatus(Enum):
    """Enumeration for the possible statuses of a background job."""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

@dataclass
class JobInfo:
    """
    A data class to hold all information about a single background job.
    This makes it easy to track the job's progress, status, and results.
    """
    job_id: str
    status: JobStatus
    start_time: datetime
    end_time: Optional[datetime] = None
    total_files: int = 0
    processed_files: int = 0
    current_file: Optional[str] = None
    status_message: Optional[str] = None
    estimated_duration: Optional[float] = None
    actual_duration: Optional[float] = None
    errors: List[str] = None
    result: Optional[Dict[str, Any]] = None
    path: Optional[str] = None
    is_dependency: bool = False
    last_update_time: Optional[datetime] = None
    error_type: Optional[str] = None
    traceback: Optional[str] = None
    timeout_seconds: Optional[float] = None
    cancel_requested: bool = False
    cancel_reason: Optional[str] = None

    def __post_init__(self):
        """Ensures the errors list is initialized after the object is created."""
        if self.errors is None:
            self.errors = []

    @property
    def progress_percentage(self) -> float:
        """Calculates the completion percentage of the job."""
        if self.total_files == 0:
            return 0.0
        return (self.processed_files / self.total_files) * 100

    @property
    def estimated_time_remaining(self) -> Optional[float]:
        """Calculates the estimated time remaining based on the average time per file."""
        if self.status != JobStatus.RUNNING or self.processed_files == 0:
            return None
        elapsed = (datetime.now() - self.start_time).total_seconds()
        avg_time_per_file = elapsed / self.processed_files
        remaining_files = self.total_files - self.processed_files
        return remaining_files * avg_time_per_file

class JobManager:
    """
    A thread-safe manager for creating, updating, and retrieving information
    about background jobs. It stores job information in memory.
    """
    def __init__(self):
        self.jobs: Dict[str, JobInfo] = {}
        self.lock = threading.Lock() # A lock to ensure thread-safe access to the jobs dictionary.
        self._done_events: Dict[str, threading.Event] = {}
        self._deadlines: Dict[str, float] = {}
        self._timeout_timers: Dict[str, threading.Timer] = {}
        self._deferred_errors: Dict[str, Exception] = {}
        self._worker_tasks: Dict[str, asyncio.Task] = {}
        self._commit_in_progress: set[str] = set()
        self._commit_events: Dict[str, threading.Event] = {}
        self._commit_cancel_reasons: Dict[str, str] = {}

    @staticmethod
    def _snapshot_job(job: JobInfo) -> JobInfo:
        """Return an isolated view so callers cannot mutate manager state."""
        return copy.deepcopy(job)

    def _stop_timeout_timer_locked(self, job_id: str) -> None:
        timer = self._timeout_timers.pop(job_id, None)
        if timer is not None:
            timer.cancel()

    def _timeout_job(self, job_id: str) -> None:
        """Request cooperative cancellation when a job deadline expires."""
        with self.lock:
            job = self.jobs.get(job_id)
            deadline = self._deadlines.get(job_id)
            if (
                job is None
                or deadline is None
                or job.status in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED)
            ):
                self._timeout_timers.pop(job_id, None)
                return

            remaining = deadline - time.monotonic()
            if remaining > 0:
                timer = threading.Timer(
                    min(remaining, threading.TIMEOUT_MAX), self._timeout_job, args=(job_id,)
                )
                timer.daemon = True
                self._timeout_timers[job_id] = timer
                timer.start()
                return

            # The timer has fired. The public request method performs the same
            # locked state transition as an explicit cancellation request.
            self._timeout_timers.pop(job_id, None)

        self.request_cancel(job_id, reason="timeout")

    def create_job(
        self,
        path: str,
        is_dependency: bool = False,
        timeout_seconds: Optional[float] = None,
    ) -> str:
        """Creates a new job, assigns it a unique ID, and stores it."""
        if timeout_seconds is not None:
            if isinstance(timeout_seconds, bool):
                raise ValueError("timeout_seconds must be a positive finite number")
            try:
                timeout_seconds = float(timeout_seconds)
            except (OverflowError, TypeError, ValueError) as exc:
                raise ValueError("timeout_seconds must be a positive finite number") from exc
            if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
                raise ValueError("timeout_seconds must be a positive finite number")

        job_id = str(uuid.uuid4())
        with self.lock:
            self.jobs[job_id] = JobInfo(
                job_id=job_id,
                status=JobStatus.PENDING,
                start_time=datetime.now(),
                path=path,
                is_dependency=is_dependency,
                timeout_seconds=timeout_seconds,
            )
            self._done_events[job_id] = threading.Event()
            if timeout_seconds is not None:
                deadline = time.monotonic() + timeout_seconds
                self._deadlines[job_id] = deadline
                timer = threading.Timer(
                    min(timeout_seconds, threading.TIMEOUT_MAX),
                    self._timeout_job,
                    args=(job_id,),
                )
                timer.daemon = True
                self._timeout_timers[job_id] = timer
                timer.start()
        return job_id

    def update_job(self, job_id: str, **kwargs):
        """Update active progress details without changing lifecycle state.

        Lifecycle transitions are owned by ``start_job`` and the finalizers.
        RUNNING requires a registered worker task so stale-job cleanup can
        confirm it has stopped before releasing waiters. Only progress fields
        can be changed through this method; cancellation and deadline state is
        owned by the lifecycle methods.
        """
        with self.lock:
            if job_id in self.jobs:
                job = self.jobs[job_id]
                if (
                    job.status in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED)
                    or job_id in self._commit_in_progress
                ):
                    return
                if kwargs.get("status") in (
                    JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED
                ):
                    return
                progress = {
                    key: value
                    for key, value in kwargs.items()
                    if key in _JOB_PROGRESS_FIELDS
                }
                if not progress:
                    return
                for key, value in progress.items():
                    setattr(job, key, value)
                job.last_update_time = datetime.now()

    def _request_cancel_locked(self, job: JobInfo, reason: str) -> bool:
        if job.status in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED):
            return False
        if job.job_id in self._commit_in_progress:
            # A successful ready-marker commit wins, but retain the request so
            # a failed callback cannot silently resume indexing or lose waiters.
            self._commit_cancel_reasons.setdefault(job.job_id, reason)
            return False
        if not job.cancel_requested:
            job.cancel_requested = True
            job.cancel_reason = reason
            job.last_update_time = datetime.now()
        return True

    def _fail_if_cancelled_worker_stopped_locked(self, job: JobInfo) -> None:
        """Wake waiters if a cancelled worker stopped without finalizing."""
        worker = self._worker_tasks.get(job.job_id)
        if (
            job.status == JobStatus.RUNNING
            and job.cancel_requested
            and job.job_id not in self._commit_in_progress
            and worker is not None
            and worker.done()
        ):
            self._fail_stopped_worker_locked(job)

    def _worker_task_done(self, job_id: str, worker_task: asyncio.Future) -> None:
        """Finalize an unexpectedly stopped worker after cancellation."""
        with self.lock:
            job = self.jobs.get(job_id)
            if (
                job is None
                or self._worker_tasks.get(job_id) is not worker_task
            ):
                return
            self._fail_if_cancelled_worker_stopped_locked(job)

    def _finish_cancelled_locked(self, job: JobInfo, error: Optional[Exception] = None) -> None:
        """Finalize a stopped job while the manager lock is held."""
        if job.job_id in self._commit_in_progress:
            return
        if not job.cancel_requested:
            self._request_cancel_locked(job, "requested")
        if job.cancel_reason == "timeout" and job.timeout_seconds is not None:
            message = f"Indexing timed out after {job.timeout_seconds:g} seconds"
        elif job.cancel_reason == "timeout":
            message = "Indexing timed out"
        elif job.cancel_reason == "source_removed":
            message = "Indexing cancelled because its source path is unavailable"
        elif job.cancel_reason == "stale":
            message = "Indexing cancelled after the job stopped making progress"
        else:
            message = "Indexing cancelled"
        if error is not None:
            message = f"{message}: {error}"
            job.error_type = type(error).__name__
            trace = "".join(
                traceback_module.format_exception(type(error), error, error.__traceback__)
            )
            if len(trace) > MAX_TRACEBACK_CHARS:
                marker = "[traceback truncated; showing final frames]\n"
                trace = marker + trace[-(MAX_TRACEBACK_CHARS - len(marker)):]
            job.traceback = trace
        else:
            job.error_type = "TimeoutError" if job.cancel_reason == "timeout" else "CancelledError"
        self._stop_timeout_timer_locked(job.job_id)
        job.status = JobStatus.CANCELLED
        job.status_message = message
        job.errors = [message]
        job.end_time = datetime.now()
        job.last_update_time = job.end_time
        self._worker_tasks.pop(job.job_id, None)
        done_event = self._done_events.get(job.job_id)
        if done_event is not None:
            done_event.set()

    def start_job(self, job_id: str, worker_task: asyncio.Task) -> bool:
        """Atomically claim a pending job before its worker can write data.

        ``worker_task`` lets stale-job cleanup distinguish a slow, still-live
        worker from one that exited without recording a terminal result.
        """
        with self.lock:
            job = self.jobs.get(job_id)
            if (
                job is None
                or job.status != JobStatus.PENDING
                or job_id in self._commit_in_progress
            ):
                return False
            deadline = self._deadlines.get(job_id)
            if deadline is not None and time.monotonic() >= deadline:
                self._request_cancel_locked(job, "timeout")
            if job.cancel_requested:
                self._finish_cancelled_locked(job)
                return False
            if worker_task is None:
                raise ValueError("worker_task is required to start a job")
            if worker_task.done():
                return False
            job.status = JobStatus.RUNNING
            job.start_time = datetime.now()
            job.last_update_time = job.start_time
            self._worker_tasks[job_id] = worker_task
            if isinstance(worker_task, asyncio.Future):
                worker_task.add_done_callback(
                    lambda finished_task: self._worker_task_done(job_id, finished_task)
                )
            return True

    def request_cancel(self, job_id: str, reason: str = "requested") -> bool:
        """Signal a running or pending job to stop at its next safe boundary."""
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                return False
            if job.cancel_requested:
                return True
            deadline = self._deadlines.get(job_id)
            if deadline is not None and time.monotonic() >= deadline:
                reason = "timeout"
            requested = self._request_cancel_locked(job, reason)
            if requested and job.status == JobStatus.PENDING and job_id not in self._commit_in_progress:
                # A pending job has not registered a worker and cannot be
                # writing, so its cancellation is already quiescent.
                self._finish_cancelled_locked(job)
            else:
                self._fail_if_cancelled_worker_stopped_locked(job)
            return requested

    def is_cancel_requested(self, job_id: str) -> bool:
        """Return whether cancellation was requested or the job deadline expired."""
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                return False
            if job.status == JobStatus.CANCELLED:
                return True
            deadline = self._deadlines.get(job_id)
            if (
                deadline is not None
                and time.monotonic() >= deadline
                and job.status not in (JobStatus.COMPLETED, JobStatus.FAILED)
            ):
                self._request_cancel_locked(job, "timeout")
            self._fail_if_cancelled_worker_stopped_locked(job)
            return job.cancel_requested

    def raise_if_cancelled(self, job_id: str) -> None:
        """Raise after the caller reaches a boundary where no write is active."""
        if not self.is_cancel_requested(job_id):
            return
        with self.lock:
            job = self.jobs.get(job_id)
            reason = job.cancel_reason if job is not None else "requested"
            timeout_seconds = job.timeout_seconds if job is not None else None
        if reason == "timeout" and timeout_seconds is not None:
            raise JobCancelledError(f"Indexing timed out after {timeout_seconds:g} seconds")
        if reason == "timeout":
            raise JobCancelledError("Indexing timed out")
        raise JobCancelledError("Indexing cancellation requested")

    def _complete_job_locked(self, job: JobInfo, kwargs: Dict[str, Any]) -> None:
        """Publish completion and wake waiters while the manager lock is held."""
        for key, value in kwargs.items():
            if hasattr(job, key):
                setattr(job, key, value)
        job.status = JobStatus.COMPLETED
        self._stop_timeout_timer_locked(job.job_id)
        job.end_time = kwargs.get("end_time", datetime.now())
        job.last_update_time = datetime.now()
        self._worker_tasks.pop(job.job_id, None)
        done_event = self._done_events.get(job.job_id)
        if done_event is not None:
            done_event.set()

    def complete_job(self, job_id: str, *, before_commit=None, **kwargs) -> bool:
        """Atomically publish a completed job after its durable work is ready.

        ``before_commit`` runs after cancellation/deadline checks while this job
        is marked as committing. That keeps its final commit atomic with
        cancellation without holding the manager-wide lock during a database
        write. Once the callback succeeds, completion wins even if the deadline
        passes during its durable write.

        Lifecycle fields are owned by this manager and cannot be overridden by
        completion metadata.
        """
        forbidden_fields = _JOB_COMPLETION_FORBIDDEN_FIELDS.intersection(kwargs)
        if forbidden_fields:
            names = ", ".join(sorted(forbidden_fields))
            raise ValueError(f"complete_job cannot override lifecycle fields: {names}")

        waiting_for_commit = None
        owns_commit = False
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                return False
            if job.status == JobStatus.COMPLETED:
                return True
            if job.status in (JobStatus.FAILED, JobStatus.CANCELLED):
                return False
            waiting_for_commit = self._commit_events.get(job_id)
            if waiting_for_commit is None:
                deadline = self._deadlines.get(job_id)
                if deadline is not None and time.monotonic() >= deadline:
                    self._request_cancel_locked(job, "timeout")
                if job.cancel_requested:
                    return False
                if before_commit is None:
                    self._complete_job_locked(job, kwargs)
                    return True
                waiting_for_commit = threading.Event()
                self._commit_events[job_id] = waiting_for_commit
                self._commit_in_progress.add(job_id)
                owns_commit = True

        if not owns_commit:
            # Another caller already owns the durable completion callback.
            # Wait for its result instead of starting a second publication.
            waiting_for_commit.wait()
            with self.lock:
                job = self.jobs.get(job_id)
                return job is not None and job.status == JobStatus.COMPLETED

        try:
            before_commit()
        except BaseException:
            with self.lock:
                self._commit_in_progress.discard(job_id)
                deferred_reason = self._commit_cancel_reasons.pop(job_id, None)
                job = self.jobs.get(job_id)
                if deferred_reason is not None and job is not None:
                    self._request_cancel_locked(job, deferred_reason)
                    if job.status == JobStatus.PENDING:
                        self._finish_cancelled_locked(job)
                    else:
                        self._fail_if_cancelled_worker_stopped_locked(job)
                self._commit_events.pop(job_id, None)
                waiting_for_commit.set()
            raise

        with self.lock:
            self._commit_in_progress.discard(job_id)
            self._commit_cancel_reasons.pop(job_id, None)
            self._commit_events.pop(job_id, None)
            job = self.jobs.get(job_id)
            if job is None:
                waiting_for_commit.set()
                return False
            if job.status == JobStatus.COMPLETED:
                waiting_for_commit.set()
                return True
            if job.status in (JobStatus.FAILED, JobStatus.CANCELLED):
                waiting_for_commit.set()
                return False
            self._complete_job_locked(job, kwargs)
            waiting_for_commit.set()
            return True

    def fail_cleanup_job(self, job_id: str, error: Exception) -> bool:
        """Report cleanup failure without claiming that cancellation succeeded.

        A stopped worker is not enough to call a job CANCELLED when its partial
        graph could not be removed. FAILED keeps that repository unavailable
        to callers that use indexed-repository discovery and records the cleanup
        error for cancellation waiters.
        """
        trace = "".join(traceback_module.format_exception(type(error), error, error.__traceback__))
        if len(trace) > MAX_TRACEBACK_CHARS:
            marker = "[traceback truncated; showing final frames]\n"
            trace = marker + trace[-(MAX_TRACEBACK_CHARS - len(marker)):]
        with self.lock:
            job = self.jobs.get(job_id)
            if (
                job is None
                or job.status == JobStatus.COMPLETED
                or job_id in self._commit_in_progress
            ):
                return False
            self._stop_timeout_timer_locked(job_id)
            deferred_error = self._deferred_errors.pop(job_id, None)
            parts = []
            if deferred_error is not None:
                parts.append(str(deferred_error))
            elif job.errors:
                parts.append(job.errors[0])
            parts.append(f"partial index cleanup failed: {error}")
            message = "; ".join(parts)
            job.status = JobStatus.FAILED
            job.status_message = message
            job.errors = [message]
            job.error_type = type(error).__name__
            job.traceback = trace
            job.end_time = datetime.now()
            job.last_update_time = job.end_time
            self._worker_tasks.pop(job_id, None)
            done_event = self._done_events.get(job_id)
            if done_event is not None:
                done_event.set()
            return True

    def finish_cancelled(self, job_id: str, error: Optional[Exception] = None) -> bool:
        """Mark cancellation complete after worker and graph cleanup have stopped."""
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None or job_id in self._commit_in_progress:
                return False
            deferred_error = self._deferred_errors.pop(job_id, None)
            if deferred_error is not None:
                if error is None:
                    error = deferred_error
                else:
                    error = RuntimeError(
                        f"{deferred_error}; cancellation cleanup also failed: {error}"
                    )
            if job.status == JobStatus.CANCELLED:
                done_event = self._done_events.get(job_id)
                if done_event is not None:
                    done_event.set()
                return True
            if job.status in (JobStatus.COMPLETED, JobStatus.FAILED):
                return False
            self._finish_cancelled_locked(job, error)
            return True

    def finish_job(self, job_id: str) -> None:
        """Finalize a deferred cancellation or wake waiters after cleanup."""
        with self.lock:
            job = self.jobs.get(job_id)
            if job_id in self._commit_in_progress:
                return
            if (
                job is not None
                and job.cancel_requested
                and job.status not in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED)
            ):
                deferred_error = self._deferred_errors.pop(job_id, None)
                self._finish_cancelled_locked(job, deferred_error)
                return
            if job is None or job.status in (
                JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED
            ):
                self._worker_tasks.pop(job_id, None)
                done_event = self._done_events.get(job_id)
                if done_event is not None:
                    done_event.set()

    def cancel_job(self, job_id: str) -> Dict[str, Any]:
        """Request cancellation and wait until the worker is quiescent."""
        done_event = None
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                return {
                    "success": False,
                    "cancelled": False,
                    "status": "not_found",
                    "message": f"Job '{job_id}' was not found.",
                }
            terminal = job.status in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED)
            if terminal:
                done_event = self._done_events.get(job_id)
                if done_event is None or done_event.is_set():
                    return {
                        "success": job.status == JobStatus.CANCELLED,
                        "cancelled": job.status == JobStatus.CANCELLED,
                        "status": job.status.value,
                        "message": job.status_message or f"Job is already {job.status.value}.",
                    }
            elif job_id in self._commit_in_progress:
                deadline = self._deadlines.get(job_id)
                reason = "timeout" if deadline is not None and time.monotonic() >= deadline else "requested"
                self._request_cancel_locked(job, reason)
                done_event = self._done_events.get(job_id)
            else:
                deadline = self._deadlines.get(job_id)
                reason = "timeout" if deadline is not None and time.monotonic() >= deadline else "requested"
                self._request_cancel_locked(job, reason)
                if job.status == JobStatus.PENDING:
                    # A worker claims PENDING atomically before its first write.
                    # If cancellation wins this race, any scheduled worker sees
                    # the terminal state and returns without starting.
                    self._finish_cancelled_locked(job)
                else:
                    self._fail_if_cancelled_worker_stopped_locked(job)
                done_event = self._done_events.get(job_id)

        if done_event is not None:
            done_event.wait()

        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                return {
                    "success": False,
                    "cancelled": False,
                    "status": "not_found",
                    "message": f"Job '{job_id}' was removed before cancellation completed.",
                }
            cancelled = job.status == JobStatus.CANCELLED
            return {
                "success": cancelled,
                "cancelled": cancelled,
                "status": job.status.value,
                "message": job.status_message or f"Job ended with status {job.status.value}.",
            }

    async def cancel_job_async(self, job_id: str) -> Dict[str, Any]:
        """Async equivalent of ``cancel_job`` without occupying a worker thread."""
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                return {
                    "success": False,
                    "cancelled": False,
                    "status": "not_found",
                    "message": f"Job '{job_id}' was not found.",
                }
            if job.status not in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED):
                deadline = self._deadlines.get(job_id)
                reason = "timeout" if deadline is not None and time.monotonic() >= deadline else "requested"
                self._request_cancel_locked(job, reason)
                if job_id not in self._commit_in_progress:
                    if job.status == JobStatus.PENDING:
                        self._finish_cancelled_locked(job)
                    else:
                        self._fail_if_cancelled_worker_stopped_locked(job)
            done_event = self._done_events.get(job_id)

        async def wait_and_result() -> Dict[str, Any]:
            while done_event is not None and not done_event.is_set():
                await asyncio.sleep(0.01)
            with self.lock:
                job = self.jobs.get(job_id)
                if job is None:
                    return {
                        "success": False,
                        "cancelled": False,
                        "status": "not_found",
                        "message": f"Job '{job_id}' was removed before cancellation completed.",
                    }
                cancelled = job.status == JobStatus.CANCELLED
                return {
                    "success": cancelled,
                    "cancelled": cancelled,
                    "status": job.status.value,
                    "message": job.status_message or f"Job ended with status {job.status.value}.",
                }

        waiter = asyncio.create_task(wait_and_result())
        try:
            return await asyncio.shield(waiter)
        except asyncio.CancelledError:
            await wait_task_until_stopped(waiter)
            raise

    def fail_job(self, job_id: str, error: Exception, status: JobStatus = JobStatus.FAILED, *, finish: bool = True):
        """Record bounded diagnostics while preserving the existing errors[0] contract."""
        if status == JobStatus.CANCELLED:
            raise ValueError("Use request_cancel and finish_cancelled after worker cleanup")
        if status != JobStatus.FAILED:
            raise ValueError("fail_job can only record FAILED jobs; use complete_job for successful completion")
        trace = "".join(traceback_module.format_exception(type(error), error, error.__traceback__))
        if len(trace) > MAX_TRACEBACK_CHARS:
            marker = "[traceback truncated; showing final frames]\n"
            trace = marker + trace[-(MAX_TRACEBACK_CHARS - len(marker)):]
        cancellation_won = False
        with self.lock:
            job = self.jobs.get(job_id)
            if (
                job is None
                or job.status in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED)
                or job_id in self._commit_in_progress
            ):
                return
            deadline = self._deadlines.get(job_id)
            if deadline is not None and time.monotonic() >= deadline:
                self._request_cancel_locked(job, "timeout")
            if job.cancel_requested:
                cancellation_won = finish
                if not finish:
                    self._deferred_errors[job_id] = error
            else:
                self._stop_timeout_timer_locked(job_id)
                job.status = status
                job.end_time = datetime.now()
                job.errors = [str(error)]
                job.error_type = type(error).__name__
                job.traceback = trace
                job.last_update_time = job.end_time
                if finish:
                    self._worker_tasks.pop(job_id, None)
                    done_event = self._done_events.get(job_id)
                    if done_event is not None:
                        done_event.set()
        if cancellation_won:
            self.finish_cancelled(job_id, error=error)

    def get_job(self, job_id: str) -> Optional[JobInfo]:
        """Retrieves an isolated snapshot of a single job."""
        with self.lock:
            job = self.jobs.get(job_id)
            return self._snapshot_job(job) if job is not None else None

    def list_jobs(self) -> List[JobInfo]:
        """Returns isolated snapshots of all jobs currently in the manager."""
        with self.lock:
            return [self._snapshot_job(job) for job in self.jobs.values()]

    def find_active_job_by_path(self, path: str) -> Optional[JobInfo]:
        """Finds the most recent, currently active (pending or running) job for a given path."""
        with self.lock:
            path_obj = Path(path).resolve()
            
            matching_jobs = sorted(
                [job for job in self.jobs.values() if job.path and Path(job.path).resolve() == path_obj],
                key=lambda j: j.start_time,
                reverse=True
            )

            for job in matching_jobs:
                if job.status in [JobStatus.PENDING, JobStatus.RUNNING]:
                    return self._snapshot_job(job)
                    
            return None

    def list_active_jobs(self) -> List[JobInfo]:
        """Return all jobs that are still PENDING or RUNNING (#1536)."""
        with self.lock:
            return [
                self._snapshot_job(job) for job in self.jobs.values()
                if job.status in (JobStatus.PENDING, JobStatus.RUNNING)
            ]

    def cleanup_old_jobs(self, max_age_hours: int = 24):
        """Removes old jobs from memory to prevent memory leaks.

        Terminal jobs (COMPLETED/FAILED/CANCELLED) are aged by end_time,
        falling back to start_time.

        Jobs still PENDING/RUNNING are never deleted outright (#1537): a
        healthy long-running index updates its progress constantly, and
        deleting its record makes update_job silently no-op and
        check_job_status report not_found while the worker keeps writing.
        Instead, an active job with *no progress update* for the whole window
        is signalled to stop. A live worker remains active until it cleans up
        and records its terminal state; a stopped or unregistered worker is
        failed with an explicit cleanup-unconfirmed outcome.
        """
        cutoff_time = datetime.now() - timedelta(hours=max_age_hours)
        with self.lock:
            active = (JobStatus.PENDING, JobStatus.RUNNING)
            jobs_to_remove = [
                job_id for job_id, job in self.jobs.items()
                if job.status not in active
                and (job.end_time or job.start_time) < cutoff_time
            ]
            for job_id in jobs_to_remove:
                self._stop_timeout_timer_locked(job_id)
                del self.jobs[job_id]
                self._done_events.pop(job_id, None)
                self._deadlines.pop(job_id, None)
                self._deferred_errors.pop(job_id, None)
                self._worker_tasks.pop(job_id, None)

            for job in self.jobs.values():
                if job.status in active:
                    if job.job_id in self._commit_in_progress:
                        continue
                    last_progress = job.last_update_time or job.start_time
                    stale = last_progress < cutoff_time
                    if stale and not job.cancel_requested:
                        self._request_cancel_locked(job, "stale")
                    if job.status == JobStatus.PENDING and job.cancel_requested:
                        # No worker has claimed this job, so it cannot be
                        # writing. Finalize it now; a later start_job call
                        # will reject the terminal state.
                        self._finish_cancelled_locked(job)
                    elif job.status == JobStatus.RUNNING and (
                        stale or job.cancel_reason == "stale"
                    ):
                        worker = self._worker_tasks.get(job.job_id)
                        if worker is not None and worker.done():
                            # The worker is confirmed stopped, but cleanup was
                            # not acknowledged. FAILED keeps any partial index
                            # unavailable and wakes cancellation waiters without
                            # claiming that graph cleanup succeeded.
                            self._fail_stopped_worker_locked(job)
                        elif worker is None:
                            job.status_message = (
                                f"No progress update for {max_age_hours}h; "
                                "worker state unavailable, cancellation pending"
                            )
                        else:
                            job.status_message = (
                                f"No progress update for {max_age_hours}h; cancellation requested"
                            )

    def _fail_stopped_worker_locked(self, job: JobInfo) -> None:
        """Fail a stale job whose worker exited without finalizing it."""
        message = "Job worker stopped before finalizing; partial work cleanup was not confirmed"
        self._stop_timeout_timer_locked(job.job_id)
        job.status = JobStatus.FAILED
        job.status_message = message
        job.errors = [message]
        job.error_type = "WorkerStoppedError"
        job.end_time = datetime.now()
        job.last_update_time = job.end_time
        self._worker_tasks.pop(job.job_id, None)
        done_event = self._done_events.get(job.job_id)
        if done_event is not None:
            done_event.set()
