
import asyncio
import pytest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from codegraphcontext.core.jobs import JobCancelledError, JobManager, JobStatus


class _WorkerProbe:
    def __init__(self):
        self.stopped = False

    def done(self):
        return self.stopped


class TestJobManager:
    """
    Unit tests for JobManager logic.
    """

    def test_create_job(self):
        manager = JobManager()
        job_id = manager.create_job("/tmp")
        
        assert job_id is not None
        job = manager.get_job(job_id)
        assert job.status == JobStatus.PENDING
        # JobInfo uses 'type' is not a field, strict dataclass. Check path instead?
        assert job.path == "/tmp"

    @pytest.mark.asyncio
    async def test_job_info_accessors_return_detached_lifecycle_snapshots(self, tmp_path):
        """Mutating returned job details cannot bypass manager transitions."""
        manager = JobManager()
        accessors = (
            lambda job_id, path: manager.get_job(job_id),
            lambda job_id, _path: manager.list_jobs()[-1],
            lambda _job_id, path: manager.find_active_job_by_path(str(path)),
            lambda job_id, _path: next(
                job for job in manager.list_active_jobs() if job.job_id == job_id
            ),
        )

        for index, get_snapshot in enumerate(accessors):
            path = tmp_path / f"snapshot-{index}"
            job_id = manager.create_job(str(path))
            snapshot = get_snapshot(job_id, path)
            assert snapshot is not None
            snapshot.status = JobStatus.CANCELLED
            snapshot.cancel_requested = True
            snapshot.errors.append("external mutation")

            stored = manager.get_job(job_id)
            assert stored.status == JobStatus.PENDING
            assert stored.cancel_requested is False
            assert stored.errors == []

        job_id = manager.create_job(str(tmp_path / "async-cancel"))
        result = await asyncio.wait_for(manager.cancel_job_async(job_id), timeout=1)
        assert result["cancelled"] is True
        assert manager.get_job(job_id).status == JobStatus.CANCELLED

    def test_update_job_status(self):
        manager = JobManager()
        job_id = manager.create_job("/tmp")
        
        # Claim the worker before updating progress.
        assert manager.start_job(job_id, _WorkerProbe())
        manager.update_job(job_id, processed_files=50, total_files=100)
        
        job = manager.get_job(job_id)
        assert job.status == JobStatus.RUNNING
        assert job.progress_percentage == 50.0

    def test_update_job_cannot_enter_running_without_a_worker(self):
        manager = JobManager()
        job_id = manager.create_job("/unclaimed")

        manager.update_job(job_id, status=JobStatus.RUNNING)

        assert manager.get_job(job_id).status == JobStatus.PENDING

    def test_update_job_cannot_reopen_or_mutate_lifecycle_fields(self):
        manager = JobManager()
        job_id = manager.create_job("/lifecycle", timeout_seconds=10)
        first_worker = _WorkerProbe()

        assert manager.start_job(job_id, first_worker)
        assert manager.request_cancel(job_id)
        manager.update_job(
            job_id,
            status=JobStatus.PENDING,
            cancel_requested=False,
            cancel_reason=None,
            timeout_seconds=100,
        )

        job = manager.get_job(job_id)
        assert job.status == JobStatus.RUNNING
        assert job.cancel_requested is True
        assert job.cancel_reason == "requested"
        assert job.timeout_seconds == 10
        assert not manager.start_job(job_id, _WorkerProbe())
        assert not manager.complete_job(job_id)
        assert manager.finish_cancelled(job_id)
        assert manager.get_job(job_id).status == JobStatus.CANCELLED

    def test_complete_job_cannot_mutate_cancellation_fields(self):
        manager = JobManager()
        job_id = manager.create_job("/completion-lifecycle")

        with pytest.raises(ValueError, match="lifecycle fields"):
            manager.complete_job(
                job_id,
                cancel_requested=True,
                cancel_reason="injected",
            )

        job = manager.get_job(job_id)
        assert job.status == JobStatus.PENDING
        assert job.cancel_requested is False
        assert job.cancel_reason is None
        assert manager.is_cancel_requested(job_id) is False

    def test_start_job_requires_a_live_registered_worker(self):
        manager = JobManager()
        job_id = manager.create_job("/unclaimed")

        with pytest.raises(ValueError, match="worker_task is required"):
            manager.start_job(job_id, None)

        stopped_worker = _WorkerProbe()
        stopped_worker.stopped = True
        assert not manager.start_job(job_id, stopped_worker)
        assert manager.get_job(job_id).status == JobStatus.PENDING

    def test_job_not_found(self):
        manager = JobManager()
        job = manager.get_job("non_existent_id")
        assert job is None

    def test_list_active_jobs_includes_pending_and_running_only(self):
        """#1536: switch_context must see PENDING as well as RUNNING."""
        manager = JobManager()
        pending_id = manager.create_job("/pending")
        running_id = manager.create_job("/running")
        done_id = manager.create_job("/done")
        failed_id = manager.create_job("/failed")
        cancelled_id = manager.create_job("/cancelled")

        assert manager.start_job(running_id, _WorkerProbe())
        manager.complete_job(done_id)
        manager.fail_job(failed_id, RuntimeError("test failure"))
        manager.request_cancel(cancelled_id)
        manager.finish_cancelled(cancelled_id)

        active_ids = {job.job_id for job in manager.list_active_jobs()}
        assert active_ids == {pending_id, running_id}

    def test_cancel_and_completion_race_has_one_terminal_winner(self):
        """A cancellation that wins the completion race cannot be overwritten."""
        manager = JobManager()
        job_id = manager.create_job("/race")
        barrier = Barrier(3)

        def cancel():
            barrier.wait()
            return manager.request_cancel(job_id)

        def complete():
            barrier.wait()
            return manager.complete_job(job_id)

        with ThreadPoolExecutor(max_workers=2) as pool:
            cancel_future = pool.submit(cancel)
            complete_future = pool.submit(complete)
            barrier.wait()
            cancel_won = cancel_future.result(timeout=2)
            completed = complete_future.result(timeout=2)

        assert cancel_won is not completed
        if cancel_won:
            assert manager.finish_cancelled(job_id)
            manager.update_job(job_id, status=JobStatus.COMPLETED)
            assert manager.get_job(job_id).status == JobStatus.CANCELLED
        else:
            assert manager.get_job(job_id).status == JobStatus.COMPLETED
            assert not manager.request_cancel(job_id)

    def test_before_commit_callback_is_atomic_with_cancellation(self):
        manager = JobManager()
        job_id = manager.create_job("/commit-race")
        callback_started = Event()
        release_callback = Event()
        cancel_started = Event()
        callback_calls = []

        def before_commit():
            callback_calls.append(True)
            callback_started.set()
            assert release_callback.wait(timeout=2)

        def complete():
            return manager.complete_job(job_id, before_commit=before_commit)

        def cancel():
            cancel_started.set()
            return manager.request_cancel(job_id)

        with ThreadPoolExecutor(max_workers=2) as pool:
            complete_future = pool.submit(complete)
            assert callback_started.wait(timeout=2)
            cancel_future = pool.submit(cancel)
            assert cancel_started.wait(timeout=2)
            release_callback.set()
            assert complete_future.result(timeout=2) is True
            assert cancel_future.result(timeout=2) is False

        assert callback_calls == [True]
        assert manager.get_job(job_id).status == JobStatus.COMPLETED

    def test_blocked_commit_callback_does_not_block_other_job_cancellation(self):
        manager = JobManager()
        committing_job_id = manager.create_job("/slow-commit")
        other_job_id = manager.create_job("/independent-job")
        callback_started = Event()
        release_callback = Event()

        def before_commit():
            callback_started.set()
            assert release_callback.wait(timeout=2)

        with ThreadPoolExecutor(max_workers=2) as pool:
            completion = pool.submit(
                manager.complete_job,
                committing_job_id,
                before_commit=before_commit,
            )
            assert callback_started.wait(timeout=2)
            cancellation = pool.submit(manager.request_cancel, other_job_id)
            try:
                assert cancellation.result(timeout=1) is True
            finally:
                release_callback.set()
            assert completion.result(timeout=1) is True

    @pytest.mark.asyncio
    async def test_async_cancel_waiter_waits_for_in_progress_completion(self):
        manager = JobManager()
        job_id = manager.create_job("/async-commit")
        callback_started = Event()
        release_callback = Event()

        def before_commit():
            callback_started.set()
            assert release_callback.wait(timeout=2)

        completion = asyncio.create_task(
            asyncio.to_thread(
                manager.complete_job,
                job_id,
                before_commit=before_commit,
            )
        )
        assert await asyncio.to_thread(callback_started.wait, 2)
        waiter = asyncio.create_task(manager.cancel_job_async(job_id))
        await asyncio.sleep(0.02)
        assert not waiter.done()

        release_callback.set()
        assert await asyncio.wait_for(completion, timeout=1) is True
        result = await asyncio.wait_for(waiter, timeout=1)

        assert result["status"] == JobStatus.COMPLETED.value
        assert result["cancelled"] is False

    @pytest.mark.asyncio
    async def test_cancel_during_failed_commit_is_preserved_for_cleanup(self):
        manager = JobManager()
        job_id = manager.create_job("/failed-commit-cancel")
        assert manager.start_job(job_id, _WorkerProbe())
        callback_started = Event()
        release_callback = Event()

        def before_commit():
            callback_started.set()
            assert release_callback.wait(timeout=2)
            raise RuntimeError("ready marker write failed")

        completion = asyncio.create_task(
            asyncio.to_thread(
                manager.complete_job,
                job_id,
                before_commit=before_commit,
            )
        )
        assert await asyncio.to_thread(callback_started.wait, 2)
        waiter = asyncio.create_task(manager.cancel_job_async(job_id))
        await asyncio.sleep(0.02)
        assert not waiter.done()

        release_callback.set()
        with pytest.raises(RuntimeError, match="ready marker write failed"):
            await completion

        assert manager.is_cancel_requested(job_id)
        assert manager.get_job(job_id).status == JobStatus.RUNNING
        assert manager.finish_cancelled(job_id)
        result = await asyncio.wait_for(waiter, timeout=1)

        assert result["status"] == JobStatus.CANCELLED.value
        assert result["cancelled"] is True

    @pytest.mark.asyncio
    async def test_pending_cancel_during_failed_commit_releases_waiter(self):
        manager = JobManager()
        job_id = manager.create_job("/pending-failed-commit-cancel")
        callback_started = Event()
        release_callback = Event()

        def before_commit():
            callback_started.set()
            assert release_callback.wait(timeout=2)
            raise RuntimeError("ready marker write failed")

        completion = asyncio.create_task(
            asyncio.to_thread(
                manager.complete_job,
                job_id,
                before_commit=before_commit,
            )
        )
        assert await asyncio.to_thread(callback_started.wait, 2)
        waiter = asyncio.create_task(manager.cancel_job_async(job_id))
        await asyncio.sleep(0.02)

        release_callback.set()
        with pytest.raises(RuntimeError, match="ready marker write failed"):
            await completion

        try:
            assert manager.get_job(job_id).status == JobStatus.CANCELLED
            assert manager._done_events[job_id].is_set()
        finally:
            if not manager._done_events[job_id].is_set():
                manager.finish_cancelled(job_id)

        result = await asyncio.wait_for(waiter, timeout=1)
        assert result["status"] == JobStatus.CANCELLED.value

    @pytest.mark.asyncio
    async def test_stopped_worker_during_failed_commit_releases_cancel_waiter(self):
        manager = JobManager()
        job_id = manager.create_job("/stopped-worker-failed-commit-cancel")
        callback_started = Event()
        release_callback = Event()

        def before_commit():
            callback_started.set()
            assert release_callback.wait(timeout=2)
            raise RuntimeError("ready marker write failed")

        completion = asyncio.create_task(
            asyncio.to_thread(
                manager.complete_job,
                job_id,
                before_commit=before_commit,
            )
        )

        async def worker():
            return await asyncio.shield(completion)

        worker_task = asyncio.create_task(worker())
        assert manager.start_job(job_id, worker_task)
        assert await asyncio.to_thread(callback_started.wait, 2)

        waiter = asyncio.create_task(manager.cancel_job_async(job_id))
        await asyncio.sleep(0.02)
        worker_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker_task

        release_callback.set()
        with pytest.raises(RuntimeError, match="ready marker write failed"):
            await completion

        done, _ = await asyncio.wait({waiter}, timeout=0.2)
        if not done:
            # Release the intentionally stuck waiter so the pre-fix test fails
            # promptly instead of leaving a pending task at loop shutdown.
            manager.fail_cleanup_job(job_id, RuntimeError("test cleanup"))
            await waiter
            pytest.fail("cancellation waiter was not released after worker shutdown")
        result = waiter.result()

        assert result["status"] == JobStatus.FAILED.value
        assert result["cancelled"] is False
        assert manager.get_job(job_id).error_type == "WorkerStoppedError"
        assert manager._done_events[job_id].is_set()

    def test_successful_before_commit_wins_if_deadline_expires_during_callback(self):
        manager = JobManager()
        job_id = manager.create_job("/commit-timeout", timeout_seconds=10)
        marker_published = []

        def before_commit():
            marker_published.append(True)
            manager._deadlines[job_id] = 0.0

        assert manager.complete_job(job_id, before_commit=before_commit)

        job = manager.get_job(job_id)
        assert marker_published == [True]
        assert job.status == JobStatus.COMPLETED
        assert job.cancel_requested is False
        assert job.cancel_reason is None

    def test_before_commit_failure_does_not_publish_completion(self):
        manager = JobManager()
        job_id = manager.create_job("/commit-failure")
        assert manager.start_job(job_id, _WorkerProbe())

        def fail_before_commit():
            raise RuntimeError("ready marker failed")

        with pytest.raises(RuntimeError, match="ready marker failed"):
            manager.complete_job(job_id, before_commit=fail_before_commit)

        assert manager.get_job(job_id).status == JobStatus.RUNNING
        manager.fail_job(job_id, RuntimeError("ready marker failed"))

    def test_cleanup_failure_is_failed_and_wakes_cancel_waiter(self):
        manager = JobManager()
        job_id = manager.create_job("/cleanup-failure")
        assert manager.start_job(job_id, _WorkerProbe())
        assert manager.request_cancel(job_id)

        assert manager.fail_cleanup_job(job_id, RuntimeError("delete failed"))

        job = manager.get_job(job_id)
        assert job.status == JobStatus.FAILED
        assert "partial index cleanup failed: delete failed" in job.errors[0]
        result = manager.cancel_job(job_id)
        assert result["status"] == JobStatus.FAILED.value
        assert result["cancelled"] is False

    def test_repeated_cancel_is_idempotent_and_waiter_sees_quiescence(self):
        """Repeated callers share the stop request and observe its final status."""
        manager = JobManager()
        job_id = manager.create_job("/repeated")
        assert manager.request_cancel(job_id)
        assert manager.request_cancel(job_id)
        assert manager.is_cancel_requested(job_id)
        assert manager.finish_cancelled(job_id)

        result = manager.cancel_job(job_id)

        assert result["status"] == JobStatus.CANCELLED.value
        assert result["cancelled"] is True
        assert manager.get_job(job_id).status == JobStatus.CANCELLED

    def test_cancel_pending_job_finishes_without_a_worker(self):
        manager = JobManager()
        job_id = manager.create_job("/pending")

        result = manager.cancel_job(job_id)

        assert result["status"] == JobStatus.CANCELLED.value
        assert manager.get_job(job_id).status == JobStatus.CANCELLED

    @pytest.mark.asyncio
    async def test_cancel_pending_job_async_finishes_without_a_worker(self):
        manager = JobManager()
        job_id = manager.create_job("/pending")

        result = await manager.cancel_job_async(job_id)

        assert result["status"] == JobStatus.CANCELLED.value
        assert manager.get_job(job_id).status == JobStatus.CANCELLED

    @pytest.mark.asyncio
    async def test_deferred_failure_finalizer_completes_cancellation_before_waking_waiter(self):
        manager = JobManager()
        job_id = manager.create_job("/deferred-cancel")
        assert manager.start_job(job_id, asyncio.current_task())
        assert manager.request_cancel(job_id)
        waiter = asyncio.create_task(manager.cancel_job_async(job_id))
        await asyncio.sleep(0)

        manager.fail_job(job_id, RuntimeError("write failed"), finish=False)
        assert manager.get_job(job_id).status == JobStatus.RUNNING
        manager.finish_job(job_id)

        result = await waiter
        assert result["cancelled"] is True
        assert result["status"] == JobStatus.CANCELLED.value
        job = manager.get_job(job_id)
        assert job.status == JobStatus.CANCELLED
        assert "write failed" in job.status_message
        assert job.error_type == "RuntimeError"
        assert "RuntimeError: write failed" in job.traceback

    @pytest.mark.asyncio
    async def test_finish_job_does_not_release_waiter_before_running_worker_stops(self):
        manager = JobManager()
        job_id = manager.create_job("/finish-running")
        worker_started = asyncio.Event()
        allow_worker_to_stop = asyncio.Event()

        async def worker():
            worker_started.set()
            await allow_worker_to_stop.wait()
            manager.finish_cancelled(job_id)

        worker_task = asyncio.create_task(worker())
        assert manager.start_job(job_id, worker_task)
        await worker_started.wait()

        # A cleanup notification without a cancellation must not signal that
        # the still-running worker is quiescent to a future cancel caller.
        manager.finish_job(job_id)
        assert not manager._done_events[job_id].is_set()

        waiter = asyncio.create_task(manager.cancel_job_async(job_id))
        await asyncio.sleep(0.02)
        assert not waiter.done()

        allow_worker_to_stop.set()
        result = await asyncio.wait_for(waiter, timeout=1)
        await worker_task

        assert result["cancelled"] is True
        assert result["status"] == JobStatus.CANCELLED.value

    def test_start_job_finalizes_a_prestart_cancel_without_starting_work(self):
        manager = JobManager()
        job_id = manager.create_job("/pending")
        assert manager.request_cancel(job_id)

        assert not manager.start_job(job_id, _WorkerProbe())
        assert manager.get_job(job_id).status == JobStatus.CANCELLED

    def test_start_job_allows_only_one_worker_claim(self):
        manager = JobManager()
        job_id = manager.create_job("/pending")

        assert manager.start_job(job_id, _WorkerProbe())
        assert not manager.start_job(job_id, _WorkerProbe())
        assert manager.get_job(job_id).status == JobStatus.RUNNING

    def test_fail_job_cannot_mark_cancelled_before_cleanup(self):
        manager = JobManager()
        job_id = manager.create_job("/cancelled")

        with pytest.raises(ValueError, match="after worker cleanup"):
            manager.fail_job(
                job_id, RuntimeError("cancelled"), status=JobStatus.CANCELLED
            )

        assert manager.get_job(job_id).status == JobStatus.PENDING

    def test_fail_job_cannot_mark_completed_without_completion_finalizer(self):
        manager = JobManager()
        job_id = manager.create_job("/completed")
        assert manager.start_job(job_id, _WorkerProbe())
        ready_marker_calls = []

        with pytest.raises(ValueError, match="use complete_job"):
            manager.fail_job(
                job_id, RuntimeError("not a failure"), status=JobStatus.COMPLETED
            )

        assert manager.get_job(job_id).status == JobStatus.RUNNING
        assert manager.complete_job(
            job_id, before_commit=lambda: ready_marker_calls.append(True)
        )
        assert manager.get_job(job_id).status == JobStatus.COMPLETED
        assert ready_marker_calls == [True]

    def test_fail_job_preserves_timeout_if_deadline_expires_before_finalization(self):
        manager = JobManager()
        job_id = manager.create_job("/timeout", timeout_seconds=10)
        manager._deadlines[job_id] = 0.0

        manager.fail_job(job_id, RuntimeError("operation failed at deadline"))

        job = manager.get_job(job_id)
        assert job.status == JobStatus.CANCELLED
        assert job.cancel_reason == "timeout"
        assert job.error_type == "RuntimeError"
        assert "timed out after 10 seconds" in job.status_message
        assert "operation failed at deadline" in job.status_message

    @pytest.mark.asyncio
    async def test_timeout_requests_cancellation_while_worker_is_blocked(self):
        manager = JobManager()
        job_id = manager.create_job("/blocked-timeout", timeout_seconds=0.05)
        worker_started = asyncio.Event()
        release_worker = asyncio.Event()

        async def worker():
            worker_started.set()
            await release_worker.wait()
            try:
                manager.raise_if_cancelled(job_id)
            except JobCancelledError:
                manager.finish_cancelled(job_id)

        worker_task = asyncio.create_task(worker())
        assert manager.start_job(job_id, worker_task)
        await worker_started.wait()

        try:
            loop = asyncio.get_running_loop()
            end_time = loop.time() + 1
            while not manager.get_job(job_id).cancel_requested and loop.time() < end_time:
                await asyncio.sleep(0.005)

            job = manager.get_job(job_id)
            assert job.cancel_requested is True
            assert job.cancel_reason == "timeout"
            assert job.status == JobStatus.RUNNING
            assert not manager._done_events[job_id].is_set()

            waiter = asyncio.create_task(manager.cancel_job_async(job_id))
            await asyncio.sleep(0.02)
            assert not waiter.done()
        finally:
            release_worker.set()

        await asyncio.wait_for(worker_task, timeout=1)
        if "waiter" in locals():
            result = await asyncio.wait_for(waiter, timeout=1)
            assert result["status"] == JobStatus.CANCELLED.value
            assert result["cancelled"] is True

    def test_create_job_rejects_timeout_integer_that_overflows_float_conversion(self):
        manager = JobManager()

        with pytest.raises(ValueError, match="positive finite number"):
            manager.create_job("/overflow", timeout_seconds=10**10000)

    def test_update_job_cannot_bypass_terminal_finalizers(self):
        manager = JobManager()
        job_id = manager.create_job("/terminal")

        manager.update_job(job_id, status=JobStatus.COMPLETED)
        manager.update_job(job_id, status=JobStatus.FAILED)
        manager.update_job(job_id, status=JobStatus.CANCELLED)

        assert manager.get_job(job_id).status == JobStatus.PENDING


    def test_cleanup_never_deletes_active_jobs(self):
        """#1537: a healthy long-running job must survive the age sweep."""
        from datetime import datetime, timedelta
        manager = JobManager()
        running_id = manager.create_job("/monorepo")
        assert manager.start_job(running_id, _WorkerProbe())
        # Simulate a job that started long ago but reported progress recently.
        manager.jobs[running_id].start_time = datetime.now() - timedelta(hours=48)
        manager.update_job(running_id, processed_files=10_000)

        manager.cleanup_old_jobs(max_age_hours=24)

        job = manager.get_job(running_id)
        assert job is not None, "active job was deleted mid-run"
        assert job.status == JobStatus.RUNNING

    def test_cleanup_fails_stalled_job_without_a_live_worker_and_wakes_waiter(self):
        """A dead worker cannot leave a stale job and its cancellation waiter active."""
        from datetime import datetime, timedelta
        manager = JobManager()
        stalled_id = manager.create_job("/crashed")
        worker = _WorkerProbe()
        assert manager.start_job(stalled_id, worker)
        worker.stopped = True
        manager.jobs[stalled_id].start_time = datetime.now() - timedelta(hours=48)
        manager.jobs[stalled_id].last_update_time = datetime.now() - timedelta(hours=30)

        manager.cleanup_old_jobs(max_age_hours=24)

        job = manager.get_job(stalled_id)
        assert job is not None
        assert job.status == JobStatus.FAILED
        assert job.cancel_requested is True
        assert job.cancel_reason == "stale"
        assert job.error_type == "WorkerStoppedError"
        assert "cleanup was not confirmed" in job.status_message
        result = asyncio.run(manager.cancel_job_async(stalled_id))
        assert result["status"] == JobStatus.FAILED.value

    @pytest.mark.asyncio
    async def test_cleanup_waits_for_stale_worker_to_stop_before_failing_job(self):
        from datetime import datetime, timedelta
        manager = JobManager()
        stalled_id = manager.create_job("/slow")
        worker_release = asyncio.Event()
        worker = asyncio.create_task(worker_release.wait())
        assert manager.start_job(stalled_id, worker)
        manager.jobs[stalled_id].start_time = datetime.now() - timedelta(hours=48)
        manager.jobs[stalled_id].last_update_time = datetime.now() - timedelta(hours=30)

        manager.cleanup_old_jobs(max_age_hours=24)

        job = manager.get_job(stalled_id)
        assert job.status == JobStatus.RUNNING
        assert job.cancel_requested is True
        assert "cancellation requested" in job.status_message

        waiter = asyncio.create_task(manager.cancel_job_async(stalled_id))
        await asyncio.sleep(0)
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker
        manager.cleanup_old_jobs(max_age_hours=24)

        result = await waiter
        assert result["status"] == JobStatus.FAILED.value
        assert manager.get_job(stalled_id).error_type == "WorkerStoppedError"

    @pytest.mark.asyncio
    async def test_cancel_waiter_released_when_worker_stops_without_finalizer(self):
        from datetime import datetime, timedelta

        manager = JobManager()
        job_id = manager.create_job("/cancelled-worker")
        worker = asyncio.create_task(asyncio.Event().wait())
        assert manager.start_job(job_id, worker)

        waiter = asyncio.create_task(manager.cancel_job_async(job_id))
        await asyncio.sleep(0)
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker

        released_without_sweep = manager._done_events[job_id].is_set()
        if not released_without_sweep:
            # Keep the regression assertion focused while draining the waiter
            # on the pre-fix implementation, which only notices dead workers
            # during stale-job cleanup.
            manager.jobs[job_id].last_update_time = datetime.now() - timedelta(days=2)
            manager.cleanup_old_jobs(max_age_hours=1)
        result = await waiter

        assert released_without_sweep is True
        assert result["status"] == JobStatus.FAILED.value
        assert manager.get_job(job_id).error_type == "WorkerStoppedError"

    def test_cleanup_finalizes_stale_pending_jobs_that_have_no_worker(self):
        from datetime import datetime, timedelta
        manager = JobManager()
        pending_id = manager.create_job("/queued")
        manager.jobs[pending_id].last_update_time = datetime.now() - timedelta(hours=30)

        manager.cleanup_old_jobs(max_age_hours=24)

        job = manager.get_job(pending_id)
        assert job.status == JobStatus.CANCELLED
        assert job.cancel_reason == "stale"
        assert not manager.start_job(pending_id, _WorkerProbe())
        assert manager.cancel_job(pending_id)["status"] == JobStatus.CANCELLED.value

    def test_cleanup_still_removes_old_terminal_jobs(self):
        from datetime import datetime, timedelta
        manager = JobManager()
        done_id = manager.create_job("/done")
        manager.complete_job(done_id, end_time=datetime.now() - timedelta(hours=48))
        # update_job stamps last_update_time but aging uses end_time for
        # terminal jobs, so backdate it explicitly.
        manager.jobs[done_id].end_time = datetime.now() - timedelta(hours=48)

        manager.cleanup_old_jobs(max_age_hours=24)

        assert manager.get_job(done_id) is None
