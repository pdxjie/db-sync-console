from __future__ import annotations

from typing import Any

from .config import Config
from .store import SyncStore
from .sync import SyncManager


class JobScheduler:
    def __init__(self, config: Config, store: SyncStore, manager: SyncManager):
        self.config = config
        self.store = store
        self.manager = manager
        self.scheduler = None

    def start(self) -> None:
        try:
            from apscheduler.schedulers.background import BackgroundScheduler
        except ImportError as exc:
            raise RuntimeError("APScheduler is required for scheduled jobs.") from exc

        if self.scheduler and self.scheduler.running:
            return
        self.scheduler = BackgroundScheduler(timezone=self.config.app.timezone)
        self.reload()
        self.scheduler.start()

    def shutdown(self) -> None:
        if self.scheduler and self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    def reload(self) -> None:
        if not self.scheduler:
            return
        for scheduled_job in self.scheduler.get_jobs():
            scheduled_job.remove()
        for job in self.store.list_jobs():
            if job["schedule_enabled"] and job["cron_expr"]:
                self._add_job(job)

    def snapshot(self) -> dict[str, Any]:
        if not self.scheduler:
            return {"running": False, "jobs": []}
        jobs = []
        for item in self.scheduler.get_jobs():
            jobs.append(
                {
                    "id": item.id,
                    "name": item.name,
                    "next_run_time": item.next_run_time.isoformat() if item.next_run_time else None,
                }
            )
        return {"running": self.scheduler.running, "jobs": jobs}

    def _add_job(self, job: dict[str, Any]) -> None:
        from apscheduler.triggers.cron import CronTrigger

        trigger = CronTrigger.from_crontab(job["cron_expr"], timezone=self.config.app.timezone)
        self.scheduler.add_job(
            lambda job_id=job["id"]: self.manager.start_job(job_id, source="schedule"),
            trigger=trigger,
            id=f"sync-job-{job['id']}",
            name=job["name"],
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )
