"""Configuration loading and validation.

The config file is TOML. Every enrolled repository is listed explicitly; GitHub access
alone never enrolls a repository or authorizes sending its source to Claude.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import tomllib
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from pathlib import Path

from . import runtime
from .readme import HUMAN_OWNED_DEFAULT, LLM_SECTIONS, DETERMINISTIC_SECTIONS, SECTION_IDS

PUBLISH_MODES = ("preview", "review_pr", "auto_pr", "direct")  # direct: [[repo]] opt-in only
DISCOVERY_MODES = ("preview", "review_pr", "auto_pr")
SCHEDULE_MODES = ("manual", "interval", "daily", "weekly")
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
API_EXPORT = ("allowlist", "none")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

# USD per million tokens. Values from Anthropic's published pricing (checked 2026-11-09).
# cache_write uses the 5-minute write rate (1.25x input); the monitor does not request caching,
# so it is only used to price any cache tokens the API reports.
DEFAULT_PRICING = {
    "claude-sonnet-5-5": {"input": 2.0, "output": 10.0, "cache_write": 2.5, "cache_read": 0.2},
    "claude-opus-5-5": {"input": 4.0, "output": 20.0, "cache_write": 5.0, "cache_read": 0.2},
    "claude-haiku-5-5": {"input": 0.1, "output": 0.5, "cache_write": 0.125, "cache_read": 0.01},
}


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Budget:
    per_run_usd: float | None = None
    daily_usd: float | None = None
    monthly_usd: float | None = None

    @property
    def configured(self) -> bool:
        return None not in (self.per_run_usd, self.daily_usd, self.monthly_usd)


@dataclass(frozen=True)
class Schedule:
    mode: str = "interval"
    interval_minutes: int = 15
    time: str = "09:00"
    weekdays: tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri")
    timezone: str = "local"  # local follows this computer's clock, including daylight saving


@dataclass(frozen=True)
class RepoConfig:
    name: str
    branch: str = ""  # empty = the repository's current default branch
    readme_paths: tuple[str, ...] = ("README.md",)
    source_include: tuple[str, ...] = ()
    source_exclude: tuple[str, ...] = ()
    template_version: int = 1
    managed_sections: tuple[str, ...] = ()
    disabled_sections: tuple[str, ...] = ()
    publish_mode: str = "preview"
    api_export: str = "none"
    monthly_usd: float | None = None
    daily_usd: float | None = None
    paused: bool = False
    readme_auto: bool = False  # discovered repositories: use the README actually present at the root

    @property
    def llm_sections(self) -> tuple[str, ...]:
        return tuple(s for s in self.managed_sections if s in LLM_SECTIONS and s not in self.disabled_sections)

    @property
    def deterministic_sections(self) -> tuple[str, ...]:
        return tuple(s for s in self.managed_sections if s in DETERMINISTIC_SECTIONS and s not in self.disabled_sections)

    def fingerprint_part(self) -> str:
        """Hash of the settings that change what Claude is asked. Publishing mode, limits and
        pause state are excluded so changing them never triggers a paid regeneration."""
        data = asdict(self)
        # readme_auto only says how readme_paths was found; the resolved path is still included.
        for key in ("publish_mode", "daily_usd", "monthly_usd", "paused", "readme_auto"):
            data.pop(key)
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True)
class Discovery:
    """Account-wide discovery. Discovered repositories without a [[repo]] entry are maintained
    with these defaults and never send source to Claude (api_export is always "none")."""
    owner: str = ""  # empty: discovery off, only [[repo]] entries are monitored
    refresh_hours: float = 6
    publish_mode: str = "review_pr"
    managed_sections: tuple[str, ...] = ("project-structure", "recent-changes")
    adoptions_per_day: int = 3
    exclude: tuple[str, ...] = ()  # listed in the inventory, never processed

    @property
    def enabled(self) -> bool:
        return bool(self.owner)

    def repo(self, name: str) -> RepoConfig:
        return RepoConfig(name=name, readme_auto=True, managed_sections=self.managed_sections,
                          publish_mode=self.publish_mode, api_export="none")


@dataclass(frozen=True)
class Config:
    path: Path
    poll_interval_minutes: int = 5
    model: str = "claude-sonnet-5-5"
    effort: str = "medium"
    max_output_tokens: int = 8000
    request_timeout_seconds: int = 120
    max_attempts: int = 2
    run_timeout_seconds: int = 600
    max_repos_per_run: int = 10
    check_wait_minutes: int = 30
    bot_branch: str = "readme-monitor/update"
    max_file_bytes: int = 60_000
    max_payload_chars: int = 180_000
    quiet_minutes: int = 30                 # no paid generation until the branch has been still this long
    min_generation_interval_hours: float = 12  # per repository, between paid generations
    max_postpone_hours: float = 24          # constant commits stop delaying generation after this
    budget: Budget = field(default_factory=Budget)
    pacing: str = "even"                    # even: month-to-date spend follows the calendar; none
    pricing: dict = field(default_factory=dict)
    repos: tuple[RepoConfig, ...] = ()
    discovery: Discovery = field(default_factory=Discovery)
    export_restricted: tuple[str, ...] = ()  # never sent to Claude, whatever else is configured
    host_lock: str = ""                     # [installation]: only this computer name may be activated
    schedule: Schedule = field(default_factory=Schedule)

    def price(self) -> dict | None:
        return self.pricing.get(self.model) or DEFAULT_PRICING.get(self.model)

    def repo(self, name: str) -> RepoConfig:
        for r in self.repos:
            if r.name.lower() == name.lower():
                return r
        raise ConfigError(f"repository {name!r} is not enrolled in {self.path}")

    def is_restricted(self, name: str) -> bool:
        return name.lower() in {n.lower() for n in self.export_restricted}


def _safe_rel_path(p: str, what: str) -> str:
    norm = p.replace("\\", "/")
    if not norm or norm.startswith("/") or ":" in norm or any(part in ("..", "") for part in norm.split("/")):
        raise ConfigError(f"{what} {p!r} must be a relative path inside the repository")
    return norm


def _num(value, what, minimum=0, maximum=None, kind=float):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{what} must be a number")
    if value < minimum or (maximum is not None and value > maximum):
        raise ConfigError(f"{what} must be between {minimum} and {maximum}")
    return kind(value)


def _str_list(value, what) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"{what} must be a list of strings")
    return tuple(value)


def _repo(raw: dict) -> RepoConfig:
    known = set(RepoConfig.__dataclass_fields__)
    unknown = set(raw) - known
    if unknown:
        raise ConfigError(f"unknown repo keys: {sorted(unknown)}")
    name = raw.get("name", "")
    if not REPO_RE.match(name):
        raise ConfigError(f"repo name {name!r} must look like owner/repository")
    readmes = tuple(_safe_rel_path(p, "readme path") for p in _str_list(raw.get("readme_paths", ["README.md"]), "readme_paths"))
    if not readmes:
        raise ConfigError(f"{name}: readme_paths must not be empty")
    managed = _str_list(raw.get("managed_sections"), "managed_sections")
    if not managed:
        managed = tuple(s for s in SECTION_IDS if s not in HUMAN_OWNED_DEFAULT)
    disabled = _str_list(raw.get("disabled_sections"), "disabled_sections")
    for s in managed + disabled:
        if s not in SECTION_IDS:
            raise ConfigError(f"{name}: unknown section id {s!r}; valid ids: {', '.join(SECTION_IDS)}")
    mode = raw.get("publish_mode", "preview")
    if mode not in PUBLISH_MODES:
        raise ConfigError(f"{name}: publish_mode must be one of {PUBLISH_MODES}")
    export = raw.get("api_export", "none")
    if export not in API_EXPORT:
        raise ConfigError(f"{name}: api_export must be one of {API_EXPORT}")
    includes = _str_list(raw.get("source_include"), "source_include")
    if export == "allowlist" and not includes:
        raise ConfigError(f"{name}: api_export = 'allowlist' requires a non-empty source_include list")
    if raw.get("template_version", 1) != 1:
        raise ConfigError(f"{name}: only template_version 1 (PopL Repository Standard v1) is available")
    return RepoConfig(
        name=name,
        branch=str(raw.get("branch", "")),
        readme_paths=readmes,
        source_include=includes,
        source_exclude=_str_list(raw.get("source_exclude"), "source_exclude"),
        template_version=1,
        managed_sections=managed,
        disabled_sections=disabled,
        publish_mode=mode,
        api_export=export,
        monthly_usd=_num(raw.get("monthly_usd"), f"{name}: monthly_usd"),
        daily_usd=_num(raw.get("daily_usd"), f"{name}: daily_usd"),
        paused=bool(raw.get("paused", False)),
    )


def load(path: Path) -> Config:
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: invalid TOML: {e}") from e
    unknown = set(raw) - {"monitor", "budget", "pricing", "repo", "discovery", "policy", "installation", "schedule"}
    if unknown:
        raise ConfigError(f"unknown top-level sections: {sorted(unknown)}")
    m = raw.get("monitor", {})
    b = raw.get("budget", {})
    repos = tuple(_repo(r) for r in raw.get("repo", []))
    names = [r.name.lower() for r in repos]
    if len(names) != len(set(names)):
        raise ConfigError("a repository is enrolled more than once")
    pol = raw.get("policy", {})
    if set(pol) - {"export_restricted"}:
        raise ConfigError(f"unknown policy keys: {sorted(set(pol) - {'export_restricted'})}")
    restricted = _str_list(pol.get("export_restricted"), "policy.export_restricted")
    for r in repos:
        if r.api_export == "allowlist" and r.name.lower() in {n.lower() for n in restricted}:
            raise ConfigError(f"{r.name} is export-restricted ([policy] export_restricted); it needs its own "
                              "destination-specific approval before any source may be sent to Claude")
    discovery = _discovery(raw.get("discovery", {}))
    pacing = b.get("pacing", "even")
    if pacing not in ("even", "none"):
        raise ConfigError("budget.pacing must be even or none")
    pricing = {}
    for model, p in raw.get("pricing", {}).items():
        try:
            pricing[model] = {k: float(p[k]) for k in ("input", "output", "cache_write", "cache_read")}
        except (KeyError, TypeError, ValueError) as e:
            raise ConfigError(f"pricing.{model} needs numeric input, output, cache_write, cache_read") from e
    effort = m.get("effort", "medium")
    if effort not in ("low", "medium", "high"):
        raise ConfigError("monitor.effort must be low, medium or high")
    inst = raw.get("installation", {})
    if set(inst) - {"host_lock"}:
        raise ConfigError(f"unknown installation keys: {sorted(set(inst) - {'host_lock'})}")
    host_lock = inst.get("host_lock", "")
    if not isinstance(host_lock, str) or not re.fullmatch(r"[A-Za-z0-9-]{0,63}", host_lock):
        raise ConfigError("installation.host_lock must be a computer name (letters, digits and hyphens) or empty")
    poll = _num(m.get("poll_interval_minutes", 5), "poll_interval_minutes", 1, 1440, int)
    bot_branch = m.get("bot_branch", "readme-monitor/update")
    if not re.fullmatch(r"[A-Za-z0-9._/-]+", bot_branch) or ".." in bot_branch:
        raise ConfigError("monitor.bot_branch contains unsupported characters")
    return Config(
        path=path,
        poll_interval_minutes=poll,
        model=str(m.get("model", "claude-sonnet-5-5")),
        effort=effort,
        max_output_tokens=_num(m.get("max_output_tokens", 8000), "max_output_tokens", 1000, 32000, int),
        request_timeout_seconds=_num(m.get("request_timeout_seconds", 120), "request_timeout_seconds", 10, 600, int),
        max_attempts=_num(m.get("max_attempts", 2), "max_attempts", 1, 3, int),
        run_timeout_seconds=_num(m.get("run_timeout_seconds", 600), "run_timeout_seconds", 60, 3600, int),
        max_repos_per_run=_num(m.get("max_repos_per_run", 10), "max_repos_per_run", 1, 100, int),
        check_wait_minutes=_num(m.get("check_wait_minutes", 30), "check_wait_minutes", 0, 1440, int),
        bot_branch=bot_branch,
        max_file_bytes=_num(m.get("max_file_bytes", 60_000), "max_file_bytes", 1000, 500_000, int),
        max_payload_chars=_num(m.get("max_payload_chars", 180_000), "max_payload_chars", 10_000, 600_000, int),
        quiet_minutes=_num(m.get("quiet_minutes", 30), "quiet_minutes", 0, 1440, int),
        min_generation_interval_hours=_num(m.get("min_generation_interval_hours", 12), "min_generation_interval_hours", 0, 720),
        max_postpone_hours=_num(m.get("max_postpone_hours", 24), "max_postpone_hours", 1, 720),
        budget=Budget(
            per_run_usd=_num(b.get("per_run_usd"), "budget.per_run_usd"),
            daily_usd=_num(b.get("daily_usd"), "budget.daily_usd"),
            monthly_usd=_num(b.get("monthly_usd"), "budget.monthly_usd"),
        ),
        pacing=pacing,
        pricing=pricing,
        repos=repos,
        discovery=discovery,
        export_restricted=restricted,
        host_lock=host_lock,
        schedule=_schedule(raw["schedule"], poll) if "schedule" in raw else Schedule(interval_minutes=poll),
    )


def _schedule(d: dict, poll: int) -> Schedule:
    """Without [schedule] the task runs every poll_interval_minutes, as earlier versions did."""
    unknown = set(d) - set(Schedule.__dataclass_fields__)
    if unknown:
        raise ConfigError(f"unknown schedule keys: {sorted(unknown)}")
    mode = d.get("mode", "interval")
    if mode not in SCHEDULE_MODES:
        raise ConfigError(f"schedule.mode must be one of {SCHEDULE_MODES}")
    time = d.get("time", "09:00")
    if not isinstance(time, str) or not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", time):
        raise ConfigError("schedule.time must be HH:MM (24-hour clock)")
    days = _str_list(d.get("weekdays", list(Schedule.weekdays)), "schedule.weekdays")
    if any(x not in WEEKDAYS for x in days):
        raise ConfigError(f"schedule.weekdays may only contain {', '.join(WEEKDAYS)}")
    if mode == "weekly" and not days:
        raise ConfigError("schedule.weekdays must name at least one day in weekly mode")
    tz = d.get("timezone", "local")
    if tz not in ("local", "utc"):
        raise ConfigError("schedule.timezone must be local or utc")
    if mode == "weekly" and tz == "utc":
        # Measured on a real Task Scheduler: weekdays are counted in local days even for a UTC time,
        # so "Wed 02:00 UTC" would fire on Thursday UTC in a zone behind UTC.
        raise ConfigError("a weekly schedule uses this computer's time: set schedule.timezone = \"local\" "
                          "(Windows counts weekdays in local time, so a UTC weekday could be a day off)")
    return Schedule(mode=mode, interval_minutes=_num(d.get("interval_minutes", poll), "schedule.interval_minutes", 1, 1440, int),
                    time=time, weekdays=tuple(x for x in WEEKDAYS if x in days), timezone=tz)


def _discovery(d: dict) -> Discovery:
    unknown = set(d) - set(Discovery.__dataclass_fields__)
    if unknown:
        raise ConfigError(f"unknown discovery keys: {sorted(unknown)}")
    owner = str(d.get("owner", ""))
    if owner and not re.fullmatch(r"[A-Za-z0-9-]+", owner):
        raise ConfigError("discovery.owner must be a GitHub account name")
    mode = d.get("publish_mode", "review_pr")
    if mode not in DISCOVERY_MODES:  # direct commits need a per-repository opt-in
        raise ConfigError(f"discovery.publish_mode must be one of {DISCOVERY_MODES}")
    managed = _str_list(d.get("managed_sections", ["project-structure", "recent-changes"]), "discovery.managed_sections")
    for s in managed:
        if s not in DETERMINISTIC_SECTIONS:
            # Claude-written sections need source export, which discovery never grants.
            raise ConfigError(f"discovery.managed_sections may only contain {sorted(DETERMINISTIC_SECTIONS)}; "
                              f"enroll the repository with [[repo]] to manage {s!r}")
    exclude = _str_list(d.get("exclude"), "discovery.exclude")
    for n in exclude:
        if not REPO_RE.match(n):
            raise ConfigError(f"discovery.exclude entry {n!r} must look like owner/repository")
    return Discovery(owner=owner, refresh_hours=_num(d.get("refresh_hours", 6), "discovery.refresh_hours", 0.25, 168),
                     publish_mode=mode, managed_sections=managed,
                     adoptions_per_day=_num(d.get("adoptions_per_day", 3), "discovery.adoptions_per_day", 0, 50, int),
                     exclude=exclude)


def default_home() -> Path:
    env = os.environ.get("READMEMONITOR_HOME")
    if env:
        return Path(env).expanduser().resolve()
    pointed = runtime._pointed_home()  # the folder this Windows account activated
    if pointed is not None:
        return pointed.resolve()
    if os.name == "nt":
        # Not %LOCALAPPDATA%: MSIX-packaged apps redirect AppData writes to a private copy.
        return (Path.home() / "ReadmeMonitor").resolve()
    return (Path.home() / ".local" / "share" / "ReadmeMonitor").resolve()


# ---- writer -------------------------------------------------------------------------------

WRITTEN_MARK = "# Written by ReadmeMonitor; sha256="


def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        if v != v or v in (float("inf"), float("-inf")):
            raise ConfigError("numbers in config.toml must be finite")
        return repr(v)
    if isinstance(v, str):
        return _toml_str(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    raise ConfigError(f"cannot write {type(v).__name__} to config.toml")


def _toml_str(s: str) -> str:
    """A TOML basic string. ensure_ascii=False: JSON's surrogate-pair escapes for emoji are not valid
    TOML; JSON still escapes the C0 controls, and DEL must be escaped for TOML by hand."""
    return json.dumps(s, ensure_ascii=False).replace("\x7f", "\\u007f")


def _toml_key(k: str) -> str:
    return k if re.fullmatch(r"[A-Za-z0-9_-]+", k) else _toml_str(k)


def _is_tables(v) -> bool:
    return isinstance(v, list) and bool(v) and all(isinstance(x, dict) for x in v)


def _table(lines: list[str], keys: list[str], d: dict, array: bool = False):
    scalars = {k: v for k, v in d.items() if not isinstance(v, dict) and not _is_tables(v)}
    if keys and (array or scalars or not d):
        lines += ["", ("[[{}]]" if array else "[{}]").format(".".join(_toml_key(k) for k in keys))]
    lines += [f"{_toml_key(k)} = {_toml_value(v)}" for k, v in scalars.items()]
    for k, v in d.items():
        if isinstance(v, dict):
            _table(lines, keys + [k], v)
        elif _is_tables(v):
            for item in v:
                _table(lines, keys + [k], item, array=True)


def dumps(raw: dict) -> str:
    """TOML text for our schema: strings, numbers, booleans, lists of strings, tables, the [[repo]]
    array and quoted keys such as [pricing."model"]. Comments are not kept; save() backs them up."""
    lines: list[str] = []
    _table(lines, [], raw)
    return "\n".join(lines).lstrip("\n") + "\n"


def load_raw(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: invalid TOML: {e}") from e


def save(path: Path, raw: dict) -> Config:
    """Validate the new text with load() first, keep one copy of a file a person wrote, then
    replace atomically, so a bad edit can never leave the worker without a valid config."""
    body = dumps(raw)
    text = f"{WRITTEN_MARK}{hashlib.sha256(body.encode()).hexdigest()}\n{body}"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{secrets.token_hex(6)}.tmp")  # unique: two writers never share one temp file
    tmp.write_text(text, encoding="utf-8")
    try:
        cfg = load(tmp)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    if path.exists():
        old = path.read_text(encoding="utf-8")
        head, _, rest = old.partition("\n")
        if not (head.startswith(WRITTEN_MARK) and head[len(WRITTEN_MARK):] == hashlib.sha256(rest.encode()).hexdigest()):
            path.with_name(f"{path.name}.pre-ui-{datetime.now().strftime('%Y%m%dT%H%M%S')}.bak").write_text(old, encoding="utf-8")
    os.replace(tmp, path)
    return replace(cfg, path=path)
