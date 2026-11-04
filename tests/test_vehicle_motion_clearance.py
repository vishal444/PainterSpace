"""Vehicle approach spacing follows asset footprints and moving trajectories."""
from pathlib import Path
import math
import sys

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "shot_library"))

from motion_synthesis import (ActorPerformance, Keyframe, MotionSynthesizer,
                              HUMAN_ROUTE_CLEARANCE_M, VEHICLE_EXIT_ROUTE_BUFFER_M,
                              VEHICLE_TRAVEL_CLEARANCE_M)
from crowd_spacing import ActorEnvelopes
from semantic_references import ResolvedTarget
from timeline_resolver import ResolvedBeat


@pytest.mark.parametrize("bounds_a,bounds_b", [([2, 1.5, 4], [2, 1.5, 4]),
                                                   ([3, 2, 7], [1.5, 1.2, 3])])
def test_opposing_vehicles_keep_footprint_clearance_throughout_drive(bounds_a, bounds_b):
    manifest = {"assets": [
        {"id": "vehicle.a", "tags": ["vehicle"], "bounds_m": bounds_a},
        {"id": "vehicle.b", "tags": ["vehicle"], "bounds_m": bounds_b},
    ]}
    states = {
        "a": {"asset_id": "vehicle.a", "is_vehicle": True, "position": [-15, 0, 0], "facing_deg": 90},
        "b": {"asset_id": "vehicle.b", "is_vehicle": True, "position": [15, 0, 0], "facing_deg": 270},
    }
    synth = MotionSynthesizer(manifest, fps=24)
    performances = {}
    for actor, target in (("a", "b"), ("b", "a")):
        target_position = states[target]["position"]
        resolved = ResolvedTarget(np.array(target_position, dtype=float), 0, 0.35, "actor", target)
        beat = ResolvedBeat(f"drive_{actor}", actor, "drive_toward", 0, 3, 3,
                            {"target": target, "mode": "stop_short", "gap": "contact"})
        perf, error = synth.synthesize_actor(actor, states[actor], [beat],
                                             {beat.beat_id: resolved}, performances, 3,
                                             all_initial_states=states)
        assert error is None
        performances[actor] = perf

    required = (math.hypot(bounds_a[0], bounds_a[2])
                + math.hypot(bounds_b[0], bounds_b[2])) / 2 + VEHICLE_TRAVEL_CLEARANCE_M
    for frame in range(73):
        t = frame / 24
        a_pos, _ = performances["a"].sample(t)
        b_pos, _ = performances["b"].sample(t)
        assert math.hypot(a_pos[0] - b_pos[0], a_pos[2] - b_pos[2]) >= required - 1e-4


def test_clearance_failure_when_waiting_position_is_invaded():
    manifest = {"assets": [{"id": "vehicle", "tags": ["vehicle"], "bounds_m": [2, 1, 4]}]}
    states = {
        "a": {"asset_id": "vehicle", "is_vehicle": True, "position": [0, 0, 0]},
        "b": {"asset_id": "vehicle", "is_vehicle": True, "position": [1, 0, 0]},
    }
    beat = ResolvedBeat("drive", "a", "drive_to", 0, 2, 2, {"target": "marker"})
    resolved = ResolvedTarget(np.array([10, 0, 0]), 0, 0, "marker", "marker")
    _, error = MotionSynthesizer(manifest).synthesize_actor(
        "a", states["a"], [beat], {"drive": resolved}, {}, 2, all_initial_states=states)
    assert error is not None
    assert error.error_code == "MOVING_OBJECT_CLEARANCE"


@pytest.mark.parametrize("bounds", [[2, 1.5, 4], [3, 2, 7]])
def test_exit_ends_outside_own_vehicle_footprint(bounds):
    manifest = {"assets": [
        {"id": "vehicle", "tags": ["vehicle"], "bounds_m": bounds,
         "anchors": {"driver_seat": {"pos": [-0.4, 0.3, 0]},
                     "door_fl_stand": {"pos": [-1.2, 0, 0]}}},
        {"id": "person", "bounds_max": [0.3, 1.8, 0.2]},
    ], "clips": {"opening_door_inwards": {}}}
    car = ActorPerformance("car", is_vehicle=True, asset_id="vehicle")
    car.keyframes = [Keyframe(0, [0, 0, 0], 0), Keyframe(2, [0, 0, 0], 0)]
    beat = ResolvedBeat("exit", "driver", "exit_vehicle", 0, 2, 2, {"vehicle": "car"})
    state = {"asset_id": "person", "position": [-0.4, 0.3, 0],
             "at_reference": "car:driver_seat"}
    perf, error = MotionSynthesizer(manifest).synthesize_actor(
        "driver", state, [beat], {}, {"car": car}, 2)
    assert error is None
    exit_position, _ = perf.sample(2)
    required = (math.hypot(bounds[0], bounds[2]) / 2
                + HUMAN_ROUTE_CLEARANCE_M + VEHICLE_EXIT_ROUTE_BUFFER_M)
    assert math.hypot(exit_position[0], exit_position[2]) >= required - 1e-4

    # The next travel beat must still see the host vehicle as an obstacle.
    obstacles = MotionSynthesizer(manifest)._collect_obstacles_at_time(
        2, 4, "driver", exit_position, [-exit_position[0], 0, 0], {"car": car})
    assert obstacles


def test_pedestrian_route_goes_around_vehicle_footprints():
    synth = MotionSynthesizer({"assets": []})
    obstacles = [{"cx": 0, "cz": 0, "radius": 2.8},
                 {"cx": 5, "cz": 1, "radius": 1.5}]
    route = synth._plan_smooth_path([-4, 0, 0], [8, 0, 0], obstacles)
    assert len(route) > 2
    for start, end in zip(route, route[1:]):
        for obstacle in obstacles:
            vx, vz = end[0] - start[0], end[2] - start[2]
            u = max(0.0, min(1.0, ((obstacle["cx"] - start[0]) * vx
                                    + (obstacle["cz"] - start[2]) * vz) / (vx * vx + vz * vz)))
            distance = math.hypot(start[0] + u * vx - obstacle["cx"],
                                  start[2] + u * vz - obstacle["cz"])
            assert distance >= obstacle["radius"] - 1e-4


@pytest.mark.parametrize("travel_reach,pose_reach", [(0.7, 0.8), (1.15, 1.4)])
def test_actor_destination_and_route_reserve_animated_reach(travel_reach, pose_reach):
    manifest = {"assets": [{"id": "vehicle", "bounds_m": [2.0, 1.5, 4.0]}]}
    envelopes = ActorEnvelopes(
        {"person": 0.3}, {}, buffer_m=0.2,
        solid_travel_reach={"run_to": travel_reach},
        solid_beat_reach={"pose": pose_reach},
    )
    synth = MotionSynthesizer(manifest, solid_envelopes=envelopes)
    vehicle = ActorPerformance("vehicle", is_vehicle=True, asset_id="vehicle")
    vehicle.keyframes = [Keyframe(0, [0, 0, 0], 0), Keyframe(5, [0, 0, 0], 0)]
    target = ActorPerformance("target")
    target.keyframes = [Keyframe(0, [0, 0, 0], 0), Keyframe(5, [0, 0, 0], 0)]
    move = ResolvedBeat("move", "person", "run_to", 0, 2, 2, {})
    pose = ResolvedBeat("pose", "person", "perform_activity", 2, 5, 3, {})
    arrival_radius = synth._arrival_action_radius("person", move, [move, pose])
    assert arrival_radius >= pose_reach + envelopes.buffer_m

    position = synth._spaced_actor_destination(
        "person", "target", [0, 0, 0], [8, 0, 0], 2,
        {"vehicle": vehicle, "target": target}, None, None,
        actor_radius_m=arrival_radius,
    )
    assert position is not None
    vehicle_radius = math.hypot(2.0, 4.0) / 2
    assert math.hypot(position[0], position[2]) >= vehicle_radius + arrival_radius

    route_radius = synth._solid_action_radius("person", move.beat_id, move.function)
    obstacles = synth._collect_obstacles_at_time(
        0, 2, "person", [8, 0, 0], position,
        {"vehicle": vehicle}, actor_radius_m=route_radius,
    )
    assert obstacles[0]["radius"] == pytest.approx(vehicle_radius + travel_reach + envelopes.buffer_m)


def test_vehicle_exit_reserves_space_for_next_travel_clip():
    manifest = {"assets": [
        {"id": "vehicle", "bounds_m": [2.0, 1.5, 4.0],
         "anchors": {"door_fl_stand": {"pos": [-1.2, 0, 0]}}},
        {"id": "person"},
    ], "clips": {"sitting": {}, "standing_up": {}, "run": {}}}
    envelopes = ActorEnvelopes(
        {"person": 0.3}, {}, buffer_m=0.2,
        solid_travel_reach={"run_to": 1.3},
    )
    vehicle = ActorPerformance("vehicle", is_vehicle=True, asset_id="vehicle")
    vehicle.keyframes = [Keyframe(0, [0, 0, 0], 0), Keyframe(4, [0, 0, 0], 0)]
    beats = [
        ResolvedBeat("exit", "person", "exit_vehicle", 0, 2, 2, {"vehicle": "vehicle"}),
        ResolvedBeat("run", "person", "run_to", 2, 4, 2, {"target": "marker"}),
    ]
    target = ResolvedTarget(np.array([-8, 0, 0], dtype=float), 0, 0, "marker", "marker")
    state = {"asset_id": "person", "position": [-0.3, 0.3, 0],
             "at_reference": "vehicle:driver_seat"}
    performance, error = MotionSynthesizer(manifest, solid_envelopes=envelopes).synthesize_actor(
        "person", state, beats, {"run": target}, {"vehicle": vehicle}, 4,
    )
    assert error is None
    exit_position, _ = performance.sample(2)
    assert math.hypot(exit_position[0], exit_position[2]) >= math.hypot(2, 4) / 2 + 1.3 + 0.2


def test_vehicle_exit_stays_relative_to_vehicle_while_it_moves_and_turns():
    manifest = {"assets": [
        {"id": "vehicle", "bounds_m": [2.0, 1.5, 4.0],
         "anchors": {"driver_seat": {"pos": [-0.3, 0.3, 0]},
                     "door_fl_stand": {"pos": [-1.2, 0, 0]}}},
        {"id": "person"},
    ], "clips": {"sitting": {}, "standing_up": {}}}
    car = ActorPerformance("car", is_vehicle=True, asset_id="vehicle")
    car.keyframes = [Keyframe(0, [0, 0, 0], 0), Keyframe(2, [4, 0, 1], 90)]
    beat = ResolvedBeat("exit", "driver", "exit_vehicle", 0, 2, 2, {"vehicle": "car"})
    state = {"asset_id": "person", "position": [-0.3, 0.3, 0],
             "at_reference": "car:driver_seat"}
    performance, error = MotionSynthesizer(manifest).synthesize_actor(
        "driver", state, [beat], {}, {"car": car}, 2,
    )
    assert error is None
    clearance = math.hypot(2, 4) / 2 + HUMAN_ROUTE_CLEARANCE_M + VEHICLE_EXIT_ROUTE_BUFFER_M
    for time_s in (1, 1.5, 2):
        position, _ = performance.sample(time_s)
        car_position, _ = car.sample(time_s)
        assert math.hypot(position[0] - car_position[0],
                          position[2] - car_position[2]) >= clearance - 1e-4
