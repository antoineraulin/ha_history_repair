# History Repair (`history_repair`)

Custom Home Assistant integration to scan and repair zero-value drops (`0` / `0.0`) in state history and long-term statistics (`statistics`, `statistics_short_term`).

## Installation
Copy `custom_components/history_repair` into your Home Assistant `<config>/custom_components/` directory and restart Home Assistant.

## Services

### 1. `history_repair.find_zero_values`
Scans history states for an entity and returns detected zero intervals alongside the last valid value preceding the drop.

**Developer Tools / Service Call (YAML):**
```yaml
service: history_repair.find_zero_values
data:
  entity_id: sensor.temperature_salon
  start_time: "2026-07-29T00:00:00Z"
  end_time: "2026-07-29T23:59:59Z"
```

**Response Example:**
```json
{
  "entity_id": "sensor.temperature_salon",
  "zero_intervals_found": 1,
  "intervals": [
    {
      "start_time": "2026-07-29T14:10:00+00:00",
      "end_time": "2026-07-29T14:45:00+00:00",
      "start_ts": 1785334200.0,
      "end_ts": 1785336300.0,
      "zero_count": 7,
      "previous_valid_value": 21.4
    }
  ]
}
```

### 2. `history_repair.fix_zero_values`
Repairs zero-value intervals by forward-filling the last valid numeric state ($V_{prev}$) into the `states` table and adjusting corresponding `statistics` / `statistics_short_term` entries.

**Developer Tools / Service Call (YAML):**
```yaml
service: history_repair.fix_zero_values
data:
  entity_id:
    - sensor.temperature_salon
  dry_run: true          # Set false to commit database changes
  fix_statistics: true   # Also fix statistics & statistics_short_term
```

**Response Example:**
```json
{
  "results": {
    "sensor.temperature_salon": {
      "dry_run": true,
      "states_modified": 7,
      "statistics_modified": 2,
      "repaired_intervals": [
        {
          "start_time": "2026-07-29T14:10:00+00:00",
          "end_time": "2026-07-29T14:45:00+00:00",
          "replacement_value": 21.4
        }
      ]
    }
  }
}
```

### 3. `history_repair.erase_history`
Permanently deletes an entity's full history across the `states`, `statistics_short_term`, and `statistics` tables. Optional `start_time` / `end_time` bound the erase. `dry_run` defaults to `true`, in which case nothing is deleted and only the matching row counts are returned.

**Developer Tools / Service Call (YAML):**
```yaml
service: history_repair.erase_history
data:
  entity_id:
    - sensor.temperature_salon
  start_time: "2026-07-29T00:00:00Z"   # optional
  end_time: "2026-07-29T23:59:59Z"     # optional
  dry_run: true                        # Set false to permanently delete
```

**Response Example:**
```json
{
  "results": {
    "sensor.temperature_salon": {
      "dry_run": true,
      "states_deleted": 120,
      "statistics_short_term_deleted": 24,
      "statistics_deleted": 6
    }
  }
}
```
