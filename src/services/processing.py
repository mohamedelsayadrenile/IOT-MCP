import unicodedata
from typing import Any

DevicePair = tuple[str, dict[str, Any]]


def normalize(value: str) -> str:
    return unicodedata.normalize("NFC", value).strip().casefold()


def annotate_readings(
    readings: list[dict[str, Any]], stale_after_seconds: int
) -> tuple[list[dict[str, Any]], int]:
    annotated: list[dict[str, Any]] = []
    stale_count = 0
    for reading in readings:
        age = reading.get("age_seconds")
        is_stale = isinstance(age, (int, float)) and age > stale_after_seconds
        if is_stale:
            stale_count += 1
        annotated.append({**reading, "is_stale": is_stale})
    return annotated, stale_count


def iter_devices(snapshot: dict[str, Any]) -> list[DevicePair]:
    pairs: list[DevicePair] = []
    for project in snapshot.get("projects") or []:
        project_name = project.get("project_name", "")
        for device in project.get("devices") or []:
            pairs.append((project_name, device))
    return pairs


def match_device(
    pairs: list[DevicePair], query: str
) -> tuple[DevicePair | None, list[str]]:
    normalized = normalize(query)

    for pair in pairs:
        if pair[1].get("device_id") == query.strip():
            return pair, []

    for pair in pairs:
        if normalize(str(pair[1].get("device_name", ""))) == normalized:
            return pair, []

    partial = [
        pair
        for pair in pairs
        if normalized in normalize(str(pair[1].get("device_name", "")))
    ]
    if len(partial) == 1:
        return partial[0], []
    if len(partial) > 1:
        return None, [str(pair[1].get("device_name", "")) for pair in partial]

    return None, []


def build_devices_response(devices: list[dict[str, Any]]) -> dict[str, Any]:
    return {"count": len(devices), "devices": devices}


def build_readings_response(
    snapshot: dict[str, Any], device: str | None, stale_after_seconds: int
) -> dict[str, Any]:
    if device is None:
        return _build_all_readings(snapshot, stale_after_seconds)

    pairs = iter_devices(snapshot)
    match, candidates = match_device(pairs, device)
    if match is None:
        return _build_unmatched(device, pairs, candidates)

    return _build_device_readings(
        snapshot, match, stale_after_seconds
    )


def _build_all_readings(
    snapshot: dict[str, Any], stale_after_seconds: int
) -> dict[str, Any]:
    projects: list[dict[str, Any]] = []
    device_count = 0
    reading_count = 0
    stale_count = 0

    for project in snapshot.get("projects") or []:
        devices: list[dict[str, Any]] = []
        for device in project.get("devices") or []:
            readings, stale = annotate_readings(
                device.get("readings") or [], stale_after_seconds
            )
            device_count += 1
            reading_count += len(readings)
            stale_count += stale
            devices.append({**device, "readings": readings})
        projects.append({**project, "devices": devices})

    return {
        "generated_at": snapshot.get("generated_at"),
        "device_count": device_count,
        "reading_count": reading_count,
        "stale_count": stale_count,
        "projects": projects,
    }


def _build_device_readings(
    snapshot: dict[str, Any], match: DevicePair, stale_after_seconds: int
) -> dict[str, Any]:
    project_name, device = match
    readings, stale_count = annotate_readings(
        device.get("readings") or [], stale_after_seconds
    )
    return {
        "matched": True,
        "generated_at": snapshot.get("generated_at"),
        "project_name": project_name,
        "device_id": device.get("device_id"),
        "device_name": device.get("device_name"),
        "reading_count": len(readings),
        "stale_count": stale_count,
        "readings": readings,
    }


def _build_unmatched(
    query: str, pairs: list[DevicePair], candidates: list[str]
) -> dict[str, Any]:
    available = [str(device.get("device_name", "")) for _, device in pairs]
    if candidates:
        return {
            "matched": False,
            "message": (
                f"{query!r} matches more than one device. "
                "Retry with one of the candidates."
            ),
            "candidates": candidates,
            "available_devices": available,
        }
    return {
        "matched": False,
        "message": f"No device matching {query!r}.",
        "available_devices": available,
    }
