"""All-hours autonomous workbench scheduler.

A deterministic state machine (no APScheduler dependency required):

* supports same-day, overnight and all-day (``start == end``) windows;
* polls every 20 seconds, claims PENDING tasks by priority and dispatches handlers;
* stops claiming FETCH/DISCOVER 15 minutes before the end;
* runs the slow-brain/report phase at the end;
* supports graceful shutdown (optionally generating a run report) and a manual
  "start now" override.
"""

from __future__ import annotations

import logging
import threading
import time as time_module
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Any, Callable, Iterable, Mapping

from db.sqlite_manager import SQLiteManager
from scheduler.morning_reporter import MorningReporter

log = logging.getLogger(__name__)


def _cfg(config: Any, dotted: str, default: Any = None) -> Any:
    if config is None:
        return default
    if isinstance(config, Mapping):
        node: Any = config
        for part in dotted.split("."):
            if not isinstance(node, Mapping) or part not in node:
                return default
            node = node[part]
        return node
    getter = getattr(config, "get", None)
    return getter(dotted, default) if callable(getter) else default


def _parse_hhmm(value: str, default: time) -> time:
    try:
        hour, minute = str(value or "").split(":", 1)
        return time(int(hour), int(minute))
    except Exception:
        return default


@dataclass
class WorkerStatus:
    enabled: bool
    state: str
    in_window: bool
    window_start: str
    window_end: str
    queue: dict[str, int]
    last_report: str = ""
    force: bool = False
    always_on: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "state": self.state,
            "in_window": self.in_window,
            "window_start": self.window_start,
            "window_end": self.window_end,
            "queue": dict(self.queue),
            "last_report": self.last_report,
            "force": self.force,
            "always_on": self.always_on,
        }


class NightWorker:
    def __init__(
        self,
        db: SQLiteManager,
        *,
        workspace_dir: str,
        config: Any = None,
        reporter: MorningReporter | None = None,
        clock: Callable[[], datetime] | None = None,
        poll_seconds: float = 20.0,
        handlers: Mapping[str, Callable[[dict[str, Any]], Any]] | None = None,
    ):
        self.db = db
        self.workspace = workspace_dir
        self.config = config
        self.reporter = reporter or MorningReporter(db, workspace_dir)
        self.clock = clock or (lambda: datetime.now().astimezone())
        self.poll_seconds = float(poll_seconds)
        self.handlers: dict[str, Callable[[dict[str, Any]], Any]] = dict(handlers or {})
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._state = "IDLE"
        self._force = False
        self._force_until: datetime | None = None
        self._run_started: datetime | None = None
        self._night_active = False
        self._session_ended = False
        self._last_report = ""

    # ----------------------------------------------------------------- config
    @property
    def enabled(self) -> bool:
        return bool(_cfg(self.config, "night_shift.enabled", True))

    @property
    def start_time(self) -> time:
        return _parse_hhmm(
            str(_cfg(self.config, "night_shift.start", "18:00")), time(18, 0)
        )

    @property
    def end_time(self) -> time:
        return _parse_hhmm(
            str(_cfg(self.config, "night_shift.end", "08:00")), time(8, 0)
        )

    @property
    def stop_new_fetch_minutes(self) -> int:
        return int(_cfg(self.config, "night_shift.stop_new_fetch_minutes", 15) or 15)

    @property
    def always_on(self) -> bool:
        """``start == end`` means a 24/7 workbench window."""
        return self.start_time == self.end_time

    @property
    def max_runtime_minutes(self) -> int:
        return int(_cfg(self.config, "night_shift.max_runtime_minutes", 840) or 840)

    # ----------------------------------------------------------------- window
    def is_in_window(self, now: datetime | None = None) -> bool:
        if not self.enabled and not self._force:
            return False
        now = now or self.clock()
        current = now.timetz().replace(tzinfo=None)
        start = self.start_time
        end = self.end_time
        if start == end:
            return True
        if start < end:
            return start <= current < end
        return current >= start or current < end

    def _minutes_until_end(self, now: datetime | None = None) -> float:
        now = now or self.clock()
        if self.always_on:
            return 24 * 60.0
        end = datetime.combine(now.date(), self.end_time).replace(tzinfo=now.tzinfo)
        if end <= now:
            end += timedelta(days=1)
        return max(0.0, (end - now).total_seconds() / 60.0)

    # ------------------------------------------------------------------ tasks
    def register_handler(self, task_type: str, handler: Callable[[dict[str, Any]], Any]) -> None:
        self.handlers[str(task_type).upper()] = handler

    def run_once(self, *, exclude_types: Iterable[str] = ()) -> dict[str, Any] | None:
        task = self.db.claim_next_task(exclude_types=exclude_types)
        if task is None:
            return None
        handler = self.handlers.get(str(task.get("task_type", "")).upper())
        success = False
        error = None
        if handler is None:
            error = f"没有注册 {task.get('task_type')} 处理器"
        else:
            try:
                result = handler(task)
                success = result is not False
            except Exception as exc:
                error = str(exc)
                log.exception("Task %s failed: %s", task.get("task_id"), exc)
        self.db.finish_task(int(task["task_id"]), success=success, error=error)
        task["result_status"] = "DONE" if success else "FAILED"
        task["result_error"] = error
        return task

    # --------------------------------------------------------------- runtime
    def start(self, *, force: bool = False) -> bool:
        if self._thread is not None and self._thread.is_alive():
            return False
        self._stop.clear()
        self._state = "RUNNING"
        self._force = force
        self._run_started = self.clock()
        self._force_until = (
            self._run_started + timedelta(minutes=self.max_runtime_minutes)
            if force
            else None
        )
        self._night_active = bool(force)
        self._session_ended = False
        self._thread = threading.Thread(
            target=self._loop, name="acdb-workbench-worker", daemon=True
        )
        self._thread.start()
        self.db.log_event(
            "INFO", "night", "工作台已启动" + ("（手动强制）" if force else "")
        )
        return True

    def stop(
        self,
        *,
        graceful: bool = True,
        timeout: float = 15.0,
        generate_report: bool = False,
    ) -> None:
        was_active = self._night_active
        self._stop.set()
        self._state = "STOPPING" if graceful else "STOPPED"
        if graceful and self._thread is not None:
            self._thread.join(timeout=timeout)
        if generate_report and was_active:
            self._end_night(self.clock())
        self._thread = None
        self._state = "STOPPED"
        self._force = False
        self._force_until = None
        self._night_active = False
        self._session_ended = True
        self.db.log_event("INFO", "night", "工作台已停止")

    def _loop(self) -> None:
        while not self._stop.is_set():
            now = self.clock()
            if self._force and self._force_until and now >= self._force_until:
                self._force = False
                if self._night_active:
                    self._end_night(now)
                    self._night_active = False
                    self._session_ended = True
                self._state = "IDLE"
            in_window = self.is_in_window(now)
            if not in_window and not self._force:
                if self._night_active:
                    self._end_night(now)
                    self._night_active = False
                self._session_ended = False
                self._state = "IDLE"
                self._stop.wait(min(self.poll_seconds * 5, 60))
                continue

            if self._session_ended:
                self._state = "IDLE"
                self._stop.wait(min(self.poll_seconds * 5, 60))
                continue

            if not self._night_active:
                self._night_active = True
                self.db.log_event("INFO", "night", "进入自动运行时间段")
            self._state = "RUNNING"
            near_end = (
                in_window
                and not self.always_on
                and self._minutes_until_end(now) <= self.stop_new_fetch_minutes
            )
            exclude = ("FETCH", "DISCOVER") if near_end and not self._force else ()
            task = self.run_once(exclude_types=exclude)
            if task is not None:
                continue
            if near_end and not self._force:
                self._end_night(now)
                self._night_active = False
                self._session_ended = True
                self._state = "IDLE"
                self._stop.wait(min(self.poll_seconds * 5, 60))
                continue
            # Manual override outside the window can idle until stopped.
            self._stop.wait(self.poll_seconds)
        self._state = "STOPPED"

    def _end_night(self, now: datetime) -> None:
        self._state = "SLOW_BRAIN"
        self.db.log_event("INFO", "night", "进入跨文献深度归纳与运行简报生成阶段")
        handler = self.handlers.get("REPORT")
        if handler is not None:
            try:
                handler({"task_type": "REPORT", "payload_json": "{}"})
            except Exception:
                log.exception("Report handler failed")
        try:
            report = self.reporter.generate()
            self._last_report = str(report)
        except Exception:
            log.exception("Morning report generation failed")
        self.db.log_event("INFO", "night", "工作台进入空闲待命")

    # ----------------------------------------------------------------- status
    def status(self) -> WorkerStatus:
        now = self.clock()
        return WorkerStatus(
            enabled=self.enabled,
            state=self._state,
            in_window=self.is_in_window(now),
            window_start=self.start_time.strftime("%H:%M"),
            window_end=self.end_time.strftime("%H:%M"),
            queue=self.db.count_tasks(),
            last_report=self._last_report,
            force=self._force,
            always_on=self.always_on,
        )
