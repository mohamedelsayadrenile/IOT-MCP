import unicodedata

from src.services.processing import (
    annotate_readings,
    build_devices_response,
    build_readings_response,
    iter_devices,
    match_device,
    normalize,
)

SNAPSHOT = {
    "generated_at": "2026-09-07T08:20:42.110Z",
    "projects": [
        {
            "project_name": "Paradise Farms",
            "devices": [
                {"device_id": "id-green", "device_name": "Greenhouse Climate Control",
                 "readings": []},
                {"device_id": "id-unit", "device_name": "GreenHouse Control Unit",
                 "readings": []},
                {"device_id": "id-water", "device_name": "City water Monitoring System",
                 "readings": []},
                # Arabic name, decomposed (NFD) as it may arrive over JSON.
                {"device_id": "id-ar",
                 "device_name": unicodedata.normalize("NFD", "محطة الري"),
                 "readings": []},
            ],
        }
    ],
}

PAIRS = iter_devices(SNAPSHOT)


def match_name(query: str) -> str | None:
    match, _ = match_device(PAIRS, query)
    return None if match is None else match[1]["device_name"]


def test_iter_devices_flattens_with_project_name():
    assert len(PAIRS) == 4
    assert PAIRS[0][0] == "Paradise Farms"


def test_exact_device_id_wins():
    assert match_name("id-water") == "City water Monitoring System"


def test_exact_name_is_case_insensitive():
    assert match_name("greenhouse climate control") == "Greenhouse Climate Control"


def test_exact_name_beats_substring():
    """'GreenHouse Control Unit' is also a substring candidate for this query."""
    assert match_name("Greenhouse Climate Control") == "Greenhouse Climate Control"


def test_unique_substring_matches():
    assert match_name("city water") == "City water Monitoring System"


def test_ambiguous_substring_returns_candidates_not_a_guess():
    match, candidates = match_device(PAIRS, "greenhouse")
    assert match is None
    assert sorted(candidates) == [
        "GreenHouse Control Unit",
        "Greenhouse Climate Control",
    ]


def test_no_match_returns_no_candidates():
    assert match_device(PAIRS, "nonexistent") == (None, [])


def test_arabic_name_matches_across_composition_forms():
    """The stored name is NFD; the query is NFC. Both must normalise to one form."""
    assert match_name("محطة الري") is not None


def test_whitespace_is_ignored():
    assert match_name("  id-green  ") == "Greenhouse Climate Control"
    assert match_name("  city water  ") == "City water Monitoring System"


def test_normalize_folds_case_and_composition():
    assert normalize(" ÄBC ") == normalize(unicodedata.normalize("NFD", "äbc"))


def test_annotate_readings_flags_stale_at_the_boundary():
    readings = [
        {"sensor": "a", "age_seconds": 3},
        {"sensor": "b", "age_seconds": 3600},       # equal to threshold: fresh
        {"sensor": "c", "age_seconds": 3601},       # over threshold: stale
        {"sensor": "d", "age_seconds": 43_440_508},
    ]
    annotated, stale_count = annotate_readings(readings, 3600)

    assert [r["is_stale"] for r in annotated] == [False, False, True, True]
    assert stale_count == 2
    # Original fields survive untouched.
    assert annotated[0]["sensor"] == "a"


def test_annotate_readings_handles_missing_age():
    annotated, stale_count = annotate_readings([{"sensor": "a"}], 3600)
    assert annotated[0]["is_stale"] is False
    assert stale_count == 0


# --- response builders -------------------------------------------------------

READING_SNAPSHOT = {
    "generated_at": "2026-09-07T08:20:42.110Z",
    "projects": [
        {
            "project_name": "Paradise Farms",
            "devices": [
                {
                    "device_id": "id-green",
                    "device_name": "Greenhouse Climate Control",
                    "readings": [
                        {"sensor": "ambient_temp", "value": 31.4, "age_seconds": 3},
                        {"sensor": "Temp_Bot", "value": 29.4, "age_seconds": 43_000_000},
                    ],
                },
                {
                    "device_id": "id-water",
                    "device_name": "City water Monitoring System",
                    "readings": [{"sensor": "ph", "value": 7.1, "age_seconds": 12}],
                },
            ],
        }
    ],
}


def test_build_devices_response_counts():
    response = build_devices_response([{"_id": "a", "name": "A"}])
    assert response == {"count": 1, "devices": [{"_id": "a", "name": "A"}]}


def test_unfiltered_response_summarises_and_annotates():
    response = build_readings_response(READING_SNAPSHOT, None, 3600)

    assert response["generated_at"] == "2026-09-07T08:20:42.110Z"
    assert response["device_count"] == 2
    assert response["reading_count"] == 3
    assert response["stale_count"] == 1

    readings = response["projects"][0]["devices"][0]["readings"]
    assert [r["is_stale"] for r in readings] == [False, True]
    # Project and device metadata survives.
    assert response["projects"][0]["project_name"] == "Paradise Farms"
    assert response["projects"][0]["devices"][0]["device_name"] == (
        "Greenhouse Climate Control"
    )


def test_filtered_response_is_flattened():
    response = build_readings_response(READING_SNAPSHOT, "city water", 3600)

    assert response["matched"] is True
    assert response["device_name"] == "City water Monitoring System"
    assert response["device_id"] == "id-water"
    assert response["project_name"] == "Paradise Farms"
    assert response["reading_count"] == 1
    assert response["stale_count"] == 0
    # Flattened: no project/device nesting on a single-device response.
    assert "projects" not in response


def test_filtered_response_reports_staleness():
    response = build_readings_response(READING_SNAPSHOT, "id-green", 3600)
    assert response["reading_count"] == 2
    assert response["stale_count"] == 1


def test_no_match_response_lists_available_devices():
    response = build_readings_response(READING_SNAPSHOT, "nope", 3600)

    assert response["matched"] is False
    assert "No device matching" in response["message"]
    assert response["available_devices"] == [
        "Greenhouse Climate Control",
        "City water Monitoring System",
    ]
    assert "candidates" not in response


def test_ambiguous_response_lists_candidates():
    response = build_readings_response(READING_SNAPSHOT, "o", 3600)

    assert response["matched"] is False
    assert len(response["candidates"]) == 2
    assert "more than one device" in response["message"]


def test_empty_snapshot_does_not_crash():
    response = build_readings_response({}, None, 3600)
    assert response["device_count"] == 0
    assert response["reading_count"] == 0
    assert response["projects"] == []
