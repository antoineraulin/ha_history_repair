"""History Repair custom integration for Home Assistant."""
import logging
from typing import Any

import voluptuous as vol

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.db_schema import (
    States,
    StatesMeta,
    Statistics,
    StatisticsMeta,
    StatisticsShortTerm,
)
from homeassistant.components.recorder.util import session_scope
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    SERVICE_ERASE_HISTORY,
    SERVICE_FIND_ZERO_VALUES,
    SERVICE_FIX_ZERO_VALUES,
)

_LOGGER = logging.getLogger(__name__)

# ponytail: single schema definition for minimal duplication
TIME_SCHEMA = {
    vol.Optional("start_time"): cv.datetime,
    vol.Optional("end_time"): cv.datetime,
}

FIND_SCHEMA = vol.Schema(
    {
        vol.Required("entity_id"): cv.entity_ids,
        **TIME_SCHEMA,
    }
)

FIX_SCHEMA = vol.Schema(
    {
        vol.Required("entity_id"): cv.entity_ids,
        vol.Optional("dry_run", default=True): cv.boolean,
        vol.Optional("fix_statistics", default=True): cv.boolean,
        **TIME_SCHEMA,
    }
)

ERASE_SCHEMA = vol.Schema(
    {
        vol.Required("entity_id"): cv.entity_ids,
        vol.Optional("dry_run", default=True): cv.boolean,
        **TIME_SCHEMA,
    }
)


def _is_zero(val: str | None) -> bool:
    """Return True if state string represents numeric zero."""
    if val is None:
        return False
    try:
        return float(val) == 0.0
    except ValueError:
        return False


def _to_float(val: Any) -> float | None:
    """Convert state value to float if valid."""
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _ts_from_dt(dt_val: Any) -> float | None:
    """Convert datetime object to POSIX float timestamp."""
    if dt_val is None:
        return None
    if isinstance(dt_val, (int, float)):
        return float(dt_val)
    return dt_util.as_timestamp(dt_val)


def _find_zero_intervals(session: Any, entity_id: str, start_ts: float | None, end_ts: float | None) -> dict[str, Any]:
    """Find zero-value intervals and previous valid values for an entity."""
    meta = session.query(StatesMeta).filter(StatesMeta.entity_id == entity_id).first()
    if not meta:
        return {"entity_id": entity_id, "zero_intervals_found": 0, "intervals": []}

    # Locate initial V_prev before start_ts if start_ts is specified
    # ponytail: query recent rows before start_ts to find the latest valid non-zero float
    v_prev: float | None = None
    if start_ts is not None:
        prev_rows = (
            session.query(States.state)
            .filter(States.metadata_id == meta.metadata_id, States.last_updated_ts < start_ts)
            .order_by(States.last_updated_ts.desc())
            .limit(100)
            .all()
        )
        for prev_row in prev_rows:
            f_val = _to_float(prev_row.state)
            if f_val is not None and not _is_zero(prev_row.state):
                v_prev = f_val
                break

    query = session.query(States.state, States.last_updated_ts).filter(States.metadata_id == meta.metadata_id)
    if start_ts is not None:
        query = query.filter(States.last_updated_ts >= start_ts)
    if end_ts is not None:
        query = query.filter(States.last_updated_ts <= end_ts)

    rows = query.order_by(States.last_updated_ts.asc()).all()

    intervals = []
    current_interval: dict[str, Any] | None = None

    for state_str, ts in rows:
        f_val = _to_float(state_str)
        if _is_zero(state_str):
            if current_interval is None:
                current_interval = {
                    "start_ts": ts,
                    "end_ts": ts,
                    "zero_count": 1,
                    "previous_valid_value": v_prev,
                }
            else:
                current_interval["end_ts"] = ts
                current_interval["zero_count"] += 1
        else:
            if current_interval is not None:
                intervals.append(current_interval)
                current_interval = None
            if f_val is not None:
                v_prev = f_val

    if current_interval is not None:
        intervals.append(current_interval)

    # Format intervals for HA ServiceResponse output
    formatted = [
        {
            "start_time": dt_util.utc_from_timestamp(inv["start_ts"]).isoformat(),
            "end_time": dt_util.utc_from_timestamp(inv["end_ts"]).isoformat(),
            "start_ts": inv["start_ts"],
            "end_ts": inv["end_ts"],
            "zero_count": inv["zero_count"],
            "previous_valid_value": inv["previous_valid_value"],
        }
        for inv in intervals
    ]

    return {
        "entity_id": entity_id,
        "zero_intervals_found": len(formatted),
        "intervals": formatted,
    }


def _fix_zero_values_sync(
    hass: HomeAssistant,
    entity_ids: list[str],
    start_ts: float | None,
    end_ts: float | None,
    dry_run: bool,
    fix_statistics: bool,
) -> dict[str, Any]:
    """Execute zero-value repair across states and statistics tables."""
    # ponytail: session_scope handles commit on exit if read_only=False, or rollback if read_only=True
    with session_scope(hass=hass, read_only=dry_run) as session:
        results = {}

        for entity_id in entity_ids:
            scan_res = _find_zero_intervals(session, entity_id, start_ts, end_ts)
            intervals = scan_res["intervals"]

            states_modified = 0
            stats_modified = 0
            repaired_intervals = []

            meta = session.query(StatesMeta).filter(StatesMeta.entity_id == entity_id).first()
            stat_meta = session.query(StatisticsMeta).filter(StatisticsMeta.statistic_id == entity_id).first() if fix_statistics else None

            for inv in intervals:
                v_prev = inv["previous_valid_value"]
                if v_prev is None:
                    # ponytail: skip intervals where no prior valid value exists
                    continue

                inv_start, inv_end = inv["start_ts"], inv["end_ts"]

                # 1. Update states table
                if meta:
                    state_rows = (
                        session.query(States)
                        .filter(
                            States.metadata_id == meta.metadata_id,
                            States.last_updated_ts >= inv_start,
                            States.last_updated_ts <= inv_end,
                        )
                        .all()
                    )
                    for row in state_rows:
                        if _is_zero(row.state):
                            row.state = str(v_prev)
                            states_modified += 1

                # 2. Update statistics tables (Statistics & StatisticsShortTerm)
                if stat_meta:
                    for table_cls, duration_sec in ((StatisticsShortTerm, 300), (Statistics, 3600)):
                        stat_rows = (
                            session.query(table_cls)
                            .filter(
                                table_cls.metadata_id == stat_meta.id,
                                table_cls.start_ts <= inv_end,
                                table_cls.start_ts >= inv_start - duration_sec,
                            )
                            .all()
                        )
                        for s_row in stat_rows:
                            updated = False
                            if s_row.min is not None and s_row.min == 0.0:
                                s_row.min = v_prev
                                updated = True
                            if s_row.max is not None and s_row.max == 0.0:
                                s_row.max = v_prev
                                updated = True
                            if s_row.state is not None and s_row.state == 0.0:
                                s_row.state = v_prev
                                updated = True
                            # ponytail: also fix mean if any column was corrupted — a bad min/max/state implies a bad mean
                            if s_row.mean is not None and (s_row.mean == 0.0 or updated):
                                s_row.mean = v_prev
                                updated = True
                            if updated:
                                stats_modified += 1

                repaired_intervals.append(
                    {
                        "start_time": inv["start_time"],
                        "end_time": inv["end_time"],
                        "replacement_value": v_prev,
                    }
                )

            results[entity_id] = {
                "dry_run": dry_run,
                "states_modified": states_modified,
                "statistics_modified": stats_modified,
                "repaired_intervals": repaired_intervals,
            }

        return {"results": results}


def _erase_history_sync(
    hass: HomeAssistant,
    entity_ids: list[str],
    start_ts: float | None,
    end_ts: float | None,
    dry_run: bool,
) -> dict[str, Any]:
    """Erase an entity's history across states and statistics tables."""
    # ponytail: count() on dry_run so a read-only session never issues a DELETE
    def _count_or_delete(query: Any) -> int:
        if dry_run:
            return query.count() or 0
        return query.delete(synchronize_session=False) or 0

    # ponytail: session_scope handles commit on exit if read_only=False, or rollback if read_only=True
    with session_scope(hass=hass, read_only=dry_run) as session:
        results = {}

        for entity_id in entity_ids:
            states_deleted = 0
            stats_short_deleted = 0
            stats_long_deleted = 0

            meta = session.query(StatesMeta).filter(StatesMeta.entity_id == entity_id).first()
            if meta:
                q = session.query(States).filter(States.metadata_id == meta.metadata_id)
                if start_ts is not None:
                    q = q.filter(States.last_updated_ts >= start_ts)
                if end_ts is not None:
                    q = q.filter(States.last_updated_ts <= end_ts)
                states_deleted = _count_or_delete(q)

            stat_meta = session.query(StatisticsMeta).filter(StatisticsMeta.statistic_id == entity_id).first()
            if stat_meta:
                for table_cls, name in ((StatisticsShortTerm, "short"), (Statistics, "long")):
                    q = session.query(table_cls).filter(table_cls.metadata_id == stat_meta.id)
                    if start_ts is not None:
                        q = q.filter(table_cls.start_ts >= start_ts)
                    if end_ts is not None:
                        q = q.filter(table_cls.start_ts <= end_ts)
                    deleted = _count_or_delete(q)
                    if name == "short":
                        stats_short_deleted = deleted
                    else:
                        stats_long_deleted = deleted

            results[entity_id] = {
                "dry_run": dry_run,
                "states_deleted": states_deleted,
                "statistics_short_term_deleted": stats_short_deleted,
                "statistics_deleted": stats_long_deleted,
            }

        return {"results": results}


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the History Repair component from configuration.yaml (if present)."""
    _register_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: Any) -> bool:
    """Set up History Repair from a config entry (UI)."""
    _register_services(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: Any) -> bool:
    """Unload a config entry."""
    # ponytail: clean up services if no remaining entries
    current_entries = hass.config_entries.async_entries(DOMAIN) if hasattr(hass, "config_entries") else []
    if len(current_entries) <= 1:
        if hass.services.has_service(DOMAIN, SERVICE_FIND_ZERO_VALUES):
            hass.services.async_remove(DOMAIN, SERVICE_FIND_ZERO_VALUES)
        if hass.services.has_service(DOMAIN, SERVICE_FIX_ZERO_VALUES):
            hass.services.async_remove(DOMAIN, SERVICE_FIX_ZERO_VALUES)
        if hass.services.has_service(DOMAIN, SERVICE_ERASE_HISTORY):
            hass.services.async_remove(DOMAIN, SERVICE_ERASE_HISTORY)
    return True


def _register_services(hass: HomeAssistant) -> None:
    """Register custom services if not already registered."""
    _LOGGER.debug(
        "Register services called"
    )
    if hass.services.has_service(DOMAIN, SERVICE_FIND_ZERO_VALUES):
        _LOGGER.debug(
            "Find zero values service exist, return"
        )
        return

    async def handle_find_zero_values(call: ServiceCall) -> ServiceResponse:
        entity_ids = call.data["entity_id"]
        if isinstance(entity_ids, str):
            entity_ids = [entity_ids]

        start_ts = _ts_from_dt(call.data.get("start_time"))
        end_ts = _ts_from_dt(call.data.get("end_time"))

        instance = get_instance(hass)
        return await instance.async_add_executor_job(
            _find_zero_intervals_entry, hass, entity_ids, start_ts, end_ts
        )

    async def handle_fix_zero_values(call: ServiceCall) -> ServiceResponse:
        entity_ids = call.data["entity_id"]
        if isinstance(entity_ids, str):
            entity_ids = [entity_ids]

        start_ts = _ts_from_dt(call.data.get("start_time"))
        end_ts = _ts_from_dt(call.data.get("end_time"))
        dry_run = call.data.get("dry_run", True)
        fix_statistics = call.data.get("fix_statistics", True)

        instance = get_instance(hass)
        return await instance.async_add_executor_job(
            _fix_zero_values_sync, hass, entity_ids, start_ts, end_ts, dry_run, fix_statistics
        )

    async def handle_erase_history(call: ServiceCall) -> ServiceResponse:
        entity_ids = call.data["entity_id"]
        if isinstance(entity_ids, str):
            entity_ids = [entity_ids]

        start_ts = _ts_from_dt(call.data.get("start_time"))
        end_ts = _ts_from_dt(call.data.get("end_time"))
        dry_run = call.data.get("dry_run", True)

        instance = get_instance(hass)
        return await instance.async_add_executor_job(
            _erase_history_sync, hass, entity_ids, start_ts, end_ts, dry_run
        )

    _LOGGER.debug(
        "async register find zero"
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_FIND_ZERO_VALUES,
        handle_find_zero_values,
        schema=FIND_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )

    _LOGGER.debug(
        "async register fix zero"
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_FIX_ZERO_VALUES,
        handle_fix_zero_values,
        schema=FIX_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )

    _LOGGER.debug(
        "async register erase history"
    )

    hass.services.async_register(
        DOMAIN,
        SERVICE_ERASE_HISTORY,
        handle_erase_history,
        schema=ERASE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )


def _find_zero_intervals_entry(
    hass: HomeAssistant, entity_ids: list[str], start_ts: float | None, end_ts: float | None
) -> dict[str, Any]:
    with session_scope(hass=hass, read_only=True) as session:
        results = {
            eid: _find_zero_intervals(session, eid, start_ts, end_ts)
            for eid in entity_ids
        }
        return {"results": results}
