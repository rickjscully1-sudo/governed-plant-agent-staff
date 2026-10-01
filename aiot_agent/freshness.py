"""Evidence freshness SLA (fail-closed when plant telemetry is too old).

Physical AI / IoT control: consequential actions must not ride on stale
historian (or other time-series) evidence. Architecture enforces the SLA
even if a human pre-approved the tool.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, List, Optional, Union

# Tools whose payloads must satisfy the freshness SLA before HIGH-risk execute.
DEFAULT_FRESHNESS_TOOLS = frozenset({"iot_historian_query"})

# Six hours: tight enough for condition-monitoring demos, loose enough for
# multi-hour fixture windows in the plant scenarios.
DEFAULT_MAX_AGE_SECONDS = 6 * 3600.0


def parse_timestamp(value: Any) -> Optional[datetime]:
    """Parse common ISO-8601 / Zulu timestamps into aware UTC datetimes."""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _walk_timestamps(obj: Any, out: List[datetime], depth: int = 0) -> None:
    if depth > 8:
        return
    if isinstance(obj, dict):
        for key, val in obj.items():
            key_l = str(key).lower()
            if key_l in (
                "ts",
                "timestamp",
                "as_of",
                "observed_at",
                "ingested_at",
                "sample_time",
                "last_sample_ts",
            ):
                parsed = parse_timestamp(val)
                if parsed is not None:
                    out.append(parsed)
            else:
                _walk_timestamps(val, out, depth + 1)
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            _walk_timestamps(item, out, depth + 1)


def newest_timestamp(payload: Any) -> Optional[datetime]:
    """Return the newest recognizable timestamp in a tool payload, if any."""
    found: List[datetime] = []
    _walk_timestamps(payload, found)
    if not found:
        return None
    return max(found)


@dataclass(frozen=True)
class FreshnessVerdict:
    ok: bool
    status: str  # fresh | stale | missing_timestamp | clock_skew
    newest: Optional[datetime]
    age_seconds: Optional[float]
    max_age_seconds: float
    now: datetime
    detail: str

    def to_audit_dict(self) -> dict:
        return {
            "freshness_ok": self.ok,
            "freshness_status": self.status,
            "newest_ts": self.newest.isoformat() if self.newest else None,
            "age_seconds": self.age_seconds,
            "max_age_seconds": self.max_age_seconds,
            "evaluated_at": self.now.isoformat(),
            "detail": self.detail,
        }


def evaluate_freshness(
    payload: Any,
    now: datetime,
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
) -> FreshnessVerdict:
    """Judge whether evidence in *payload* is fresh enough at *now*."""
    parsed_now = parse_timestamp(now)
    if parsed_now is None:
        parsed_now = datetime.now(timezone.utc)
    now = parsed_now

    newest = newest_timestamp(payload)
    if newest is None:
        return FreshnessVerdict(
            ok=False,
            status="missing_timestamp",
            newest=None,
            age_seconds=None,
            max_age_seconds=float(max_age_seconds),
            now=now,
            detail=(
                "No usable timestamp found in evidence payload; "
                "fail-closed freshness SLA."
            ),
        )

    age = (now - newest).total_seconds()
    if age < 0:
        return FreshnessVerdict(
            ok=False,
            status="clock_skew",
            newest=newest,
            age_seconds=age,
            max_age_seconds=float(max_age_seconds),
            now=now,
            detail=(
                "Evidence timestamp is ahead of evaluation clock "
                "({0}s); fail-closed.".format(int(age))
            ),
        )

    if age > float(max_age_seconds):
        return FreshnessVerdict(
            ok=False,
            status="stale",
            newest=newest,
            age_seconds=age,
            max_age_seconds=float(max_age_seconds),
            now=now,
            detail=(
                "Newest evidence is {0:.0f}s old (SLA max {1:.0f}s); "
                "fail-closed.".format(age, float(max_age_seconds))
            ),
        )

    return FreshnessVerdict(
        ok=True,
        status="fresh",
        newest=newest,
        age_seconds=age,
        max_age_seconds=float(max_age_seconds),
        now=now,
        detail="Evidence within freshness SLA ({0:.0f}s old).".format(age),
    )


def resolve_now(
    explicit: Optional[Union[datetime, str]] = None,
    scenario_reference: Optional[Union[datetime, str]] = None,
) -> datetime:
    """Prefer explicit clock, then scenario reference_now, else wall UTC."""
    for candidate in (explicit, scenario_reference):
        if candidate is None:
            continue
        if isinstance(candidate, datetime):
            if candidate.tzinfo is None:
                return candidate.replace(tzinfo=timezone.utc)
            return candidate.astimezone(timezone.utc)
        parsed = parse_timestamp(candidate)
        if parsed is not None:
            return parsed
    return datetime.now(timezone.utc)
