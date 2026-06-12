from dataclasses import dataclass, field


@dataclass
class MusicPollState:
    pid: int | None = None
    state: str = "stopped"
    crashes_recent: int = 0
    last_heartbeat_age_s: int | None = None


@dataclass
class BarkState:
    last_run_time: str | None = None
    state: str = "idle"
    crashes_recent: int = 0


@dataclass
class ScheduledState:
    last_audit: str | None = None
    last_work_extract: str | None = None
    last_diary: str | None = None
    last_miner: str | None = None
    last_weekly: str | None = None
    window: str = "unknown"
    daily_done: bool = False


@dataclass
class DaemonState:
    pid: int | None = None
    boot_token: int | None = None
    started_at: str = ""
    started_at_ts: float = 0.0
    uptime_seconds: int = 0


@dataclass
class ApiState:
    calls_this_hour: int = 0
    limit: int = 30


@dataclass
class DsphantomState:
    daemon: DaemonState = field(default_factory=DaemonState)
    music: MusicPollState = field(default_factory=MusicPollState)
    bark: BarkState = field(default_factory=BarkState)
    scheduled: ScheduledState = field(default_factory=ScheduledState)
    api: ApiState = field(default_factory=ApiState)
    errors: list[dict] = field(default_factory=list)
    music_toggle: dict = field(default_factory=lambda: {"enabled": False})
    cooling_until: str | None = None
    active_time: str | None = None
    last_updated: str | None = None

    def to_dict(self) -> dict:
        return {
            "daemon": {"pid": self.daemon.pid, "boot_token": self.daemon.boot_token,
                       "started_at": self.daemon.started_at, "started_at_ts": self.daemon.started_at_ts,
                       "uptime_seconds": self.daemon.uptime_seconds},
            "music": {"pid": self.music.pid, "state": self.music.state,
                      "crashes_recent": self.music.crashes_recent,
                      "last_heartbeat_age_s": self.music.last_heartbeat_age_s},
            "bark": {"last_run_time": self.bark.last_run_time, "state": self.bark.state,
                     "crashes_recent": self.bark.crashes_recent},
            "scheduled": {"last_audit": self.scheduled.last_audit,
                          "last_work_extract": self.scheduled.last_work_extract,
                          "last_diary": self.scheduled.last_diary,
                          "last_miner": self.scheduled.last_miner,
                          "last_weekly": self.scheduled.last_weekly,
                          "window": self.scheduled.window,
                          "daily_done": self.scheduled.daily_done},
            "api": {"calls_this_hour": self.api.calls_this_hour, "limit": self.api.limit},
            "errors": self.errors,
            "music_toggle": self.music_toggle,
            "cooling_until": self.cooling_until,
            "active_time": self.active_time,
            "last_updated": self.last_updated,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "DsphantomState":
        dd = d.get("daemon", {})
        dm = d.get("music", {})
        db = d.get("bark", {})
        ds = d.get("scheduled", {})
        da = d.get("api", {})
        return cls(
            daemon=DaemonState(pid=dd.get("pid"), boot_token=dd.get("boot_token"),
                               started_at=dd.get("started_at", ""),
                               started_at_ts=dd.get("started_at_ts", 0.0),
                               uptime_seconds=dd.get("uptime_seconds", 0)),
            music=MusicPollState(pid=dm.get("pid"), state=dm.get("state", "stopped"),
                                 crashes_recent=dm.get("crashes_recent", 0),
                                 last_heartbeat_age_s=dm.get("last_heartbeat_age_s")),
            bark=BarkState(last_run_time=db.get("last_run_time"), state=db.get("state", "idle"),
                          crashes_recent=db.get("crashes_recent", 0)),
            scheduled=ScheduledState(**ds),
            api=ApiState(calls_this_hour=da.get("calls_this_hour", 0), limit=da.get("limit", 30)),
            errors=d.get("errors", []),
            music_toggle=d.get("music_toggle", {"enabled": False}),
            cooling_until=d.get("cooling_until"),
            active_time=d.get("active_time"),
            last_updated=d.get("last_updated"),
        )
