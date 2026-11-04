"""Seat roots follow vehicle and occupant dimensions without weakening roof checks."""
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "shot_library"))

from motion_synthesis import ActorPerformance, Keyframe, MotionSynthesizer
from shot_linter import ShotLinter
from timeline_resolver import ResolvedBeat


@pytest.mark.parametrize(
    "roof, occupant_height, height_scale, anchor_y, expected_y",
    [
        (1.5, 1.8, 1.0, 0.35, -0.3),
        (2.3, 1.6, 1.0, 0.4, 0.4),
        (1.4, 2.0, 1.0, 0.25, -0.6),
        (1.5, 1.8, 1.1, 0.35, -0.48),
    ],
)
def test_seat_root_fits_occupant_below_explicit_cabin_roof(
    roof, occupant_height, height_scale, anchor_y, expected_y
):
    manifest = {"assets": [
        {"id": "vehicle", "cabin": {"max": [1, roof, 2]},
         "anchors": {"driver_seat": {"pos": [-0.4, anchor_y, -0.2], "facing_deg": 15}}},
        {"id": "person", "bounds_max": [0.3, occupant_height, 0.2]},
    ]}
    synth = MotionSynthesizer(manifest)
    vehicle = ActorPerformance("vehicle_1", is_vehicle=True, asset_id="vehicle")

    position, facing = synth._resolve_vehicle_seat_anchor(
        "vehicle_1", "driver_seat", {"vehicle_1": vehicle}, "person", height_scale)

    assert position == pytest.approx([-0.4, expected_y, -0.2])
    assert facing == 15
    assert position[1] + occupant_height * height_scale <= roof


def test_seat_root_uses_vehicle_bounds_when_cabin_is_unspecified():
    manifest = {"assets": [
        {"id": "car", "bounds_m": [2.0, 1.47, 4.8],
         "anchors": {"driver_seat": {"pos": [-0.36, 0.35, -0.15]}}},
        {"id": "person", "collision": {"character": {"height_m": 1.8}}},
    ]}
    synth = MotionSynthesizer(manifest)
    vehicle = ActorPerformance("car_1", is_vehicle=True, asset_id="car")

    position, _ = synth._resolve_vehicle_seat_anchor(
        "car_1", "driver_seat", {"car_1": vehicle}, "person")

    assert position[1] == pytest.approx(1.47 - 1.8)


def test_occupant_roof_window_ends_before_vehicle_exit():
    manifest = {"assets": [
        {"id": "car", "bounds_m": [2.0, 1.47, 4.8],
         "anchors": {"driver_seat": {"pos": [-0.36, 0.35, -0.15]},
                     "door_fl_stand": {"pos": [-1.3, 0, 0.1]}}},
        {"id": "person", "bounds_max": [0.3, 1.8, 0.2]},
    ], "clips": {"driving": {}, "sitting": {}, "standing_up": {}}}
    car = ActorPerformance("car_1", is_vehicle=True, asset_id="car")
    car.keyframes = [Keyframe(0, [0, 0, 0], 0), Keyframe(4, [0, 0, 0], 0)]
    beats = [
        ResolvedBeat("drive_1", "human_1", "drive", 0, 2, 2, {"vehicle": "car_1"}),
        ResolvedBeat("exit_1", "human_1", "exit_vehicle", 2, 4, 2, {"vehicle": "car_1"}),
    ]
    state = {"asset_id": "person", "position": [-0.36, 0.35, -0.15],
             "facing_deg": 0, "at_reference": "car_1:driver_seat"}

    perf, error = MotionSynthesizer(manifest).synthesize_actor(
        "human_1", state, beats, {}, {"car_1": car}, 4)

    assert error is None
    occupant_rules = [rule for rule in perf.allowed_contacts if rule.get("is_occupant")]
    assert occupant_rules
    assert {(rule["t0"], rule["t1"]) for rule in occupant_rules} == {(0, 2), (2, 3)}
    assert perf.actions[0]["clip"] == "driving"
    assert [action["clip"] for action in perf.actions[1:]] == ["sitting", "standing_up"]
    assert any(k.position[1] < -0.15 for k in perf.keyframes)
    assert all(k.vehicle_supported for k in perf.keyframes if k.position[1] < -0.15)
    exit_frames = [k for k in perf.keyframes if k.time_s >= 2]
    midway = min(exit_frames, key=lambda k: abs(k.time_s - 3))
    assert midway.position[0] == pytest.approx(exit_frames[-1].position[0])
    assert midway.position[1] == pytest.approx(exit_frames[0].position[1])
    assert ShotLinter().lint_performance(perf, {})[0]


def test_underground_root_without_vehicle_support_still_fails_lint():
    perf = ActorPerformance("pedestrian")
    perf.keyframes = [Keyframe(0, [0, -0.3, 0], 0), Keyframe(1, [0, -0.3, 0], 0)]

    passed, errors = ShotLinter().lint_performance(perf, {})

    assert not passed
    assert any("underground" in error for error in errors)
