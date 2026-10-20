"""Manifest schema extensions and enrichment for PainterSpace Shot Function Library.

Enriches manifest.json with:
- Clip metadata: category, duration, loop, travel, intensity, mood tags, body region, reach.
- Gait table: walk, jog, run, sneak speeds and turn rates.
- Character capabilities: body radius, jump height/distance, reach, speed limits.
- Vehicle dynamics: accel, decel (gentle, firm, emergency), wheelbase, turn radius.
- Collider surfaces: top faces (roof, hood, trunk, etc.) for jump_on / sit_on.
- Scene zones & markers: start, stop_mark, center points.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

CLIP_CATEGORIES: Dict[str, Dict[str, Any]] = {
    # Locomotion
    "walk": {
        "category": "locomotion",
        "loop": True,
        "travel": True,
        "intensity": "normal",
        "mood_tags": ["calm", "neutral"],
        "body_region": "full_body",
        "speed_mps": 1.4,
        "footfalls": 2,
        "duration_s": 1.167,
        "f_clip": 1.71,
        "stair_suitability": "fallback",
        "turn_rate_dps": 180.0,
    },
    "run": {
        "category": "locomotion",
        "loop": True,
        "travel": True,
        "intensity": "high",
        "mood_tags": ["urgent", "energetic"],
        "body_region": "full_body",
        "speed_mps": 4.2,
        "footfalls": 2,
        "duration_s": 0.708,
        "f_clip": 2.82,
        "stair_suitability": "fallback",
        "turn_rate_dps": 270.0,
    },
    "jogging_with_box": {
        "category": "locomotion",
        "loop": True,
        "travel": True,
        "intensity": "medium",
        "mood_tags": ["active"],
        "body_region": "full_body",
        "speed_mps": 2.2,
        "turn_rate_dps": 220.0,
    },
    "slow_jog_backwards": {
        "category": "locomotion",
        "loop": True,
        "travel": True,
        "intensity": "medium",
        "mood_tags": ["cautious", "retreating"],
        "body_region": "full_body",
        "speed_mps": 1.6,
        "turn_rate_dps": 140.0,
    },
    "start_walking": {
        "category": "locomotion",
        "loop": False,
        "travel": True,
        "intensity": "normal",
        "mood_tags": ["neutral"],
        "body_region": "full_body",
        "speed_mps": 1.2,
        "turn_rate_dps": 150.0,
    },
    "sad_walk_arc_right": {
        "category": "locomotion",
        "loop": True,
        "travel": True,
        "intensity": "low",
        "mood_tags": ["sneaking", "cautious", "sad"],
        "body_region": "full_body",
        "speed_mps": 0.8,
        "turn_rate_dps": 120.0,
    },
    "crawling": {
        "category": "locomotion",
        "loop": True,
        "travel": True,
        "intensity": "low",
        "mood_tags": ["stealth", "sneaking", "injured"],
        "body_region": "full_body",
        "speed_mps": 0.6,
        "turn_rate_dps": 90.0,
    },
    "swimming": {
        "category": "locomotion",
        "loop": True,
        "travel": True,
        "intensity": "medium",
        "mood_tags": ["athletic"],
        "body_region": "full_body",
        "speed_mps": 1.0,
        "turn_rate_dps": 100.0,
    },

    # Stair Locomotion & Cadence
    "walking_up_the_stairs": {
        "category": "stair_locomotion",
        "loop": True,
        "travel": True,
        "intensity": "normal",
        "mood_tags": ["ascending", "cadence"],
        "body_region": "full_body",
        "speed_mps": 0.9,
        "footfalls": 2,
        "duration_s": 1.167,
        "f_clip": 1.71,
        "stair_suitability": "ascent",
        "turn_rate_dps": 120.0,
    },
    "running_up_stairs": {
        "category": "stair_locomotion",
        "loop": True,
        "travel": True,
        "intensity": "high",
        "mood_tags": ["urgent", "athletic", "ascending"],
        "body_region": "full_body",
        "speed_mps": 4.0,
        "footfalls": 2,
        "duration_s": 0.583,
        "f_clip": 3.43,
        "stair_suitability": "ascent",
        "turn_rate_dps": 180.0,
    },
    "descending_stairs": {
        "category": "stair_locomotion",
        "loop": False,
        "travel": True,
        "intensity": "normal",
        "mood_tags": ["descending", "braked"],
        "body_region": "full_body",
        "speed_mps": 0.9,
        "footfalls": 2,
        "duration_s": 0.917,
        "f_clip": 2.18,
        "stair_suitability": "descent",
        "turn_rate_dps": 100.0,
    },
    "descending_stairs_fast": {
        "category": "stair_locomotion",
        "loop": False,
        "travel": True,
        "intensity": "high",
        "mood_tags": ["urgent", "descending", "braked"],
        "body_region": "full_body",
        "speed_mps": 0.9,
        "footfalls": 2,
        "duration_s": 0.417,
        "f_clip": 4.80,
        "stair_suitability": "descent",
        "turn_rate_dps": 140.0,
    },

    # Jump & interaction
    "jump": {
        "category": "jump",
        "loop": False,
        "travel": True,
        "intensity": "high",
        "mood_tags": ["athletic", "evasive"],
        "body_region": "full_body",
        "takeoff_sec": 0.35,
        "apex_sec": 0.85,
        "land_sec": 1.45,
        "max_height_m": 0.90,
    },
    "jumping_down": {
        "category": "jump",
        "loop": False,
        "travel": True,
        "intensity": "high",
        "mood_tags": ["athletic", "descending"],
        "body_region": "full_body",
        "takeoff_sec": 0.25,
        "land_sec": 0.95,
    },
    "climbing": {
        "category": "jump",
        "loop": False,
        "travel": True,
        "intensity": "medium",
        "mood_tags": ["athletic"],
        "body_region": "full_body",
    },

    # Reactions
    "reaction": {
        "category": "reaction",
        "loop": False,
        "travel": False,
        "intensity": "medium",
        "mood_tags": ["startled", "surprised", "shocked"],
        "body_region": "full_body",
    },
    "spooked": {
        "category": "reaction",
        "loop": False,
        "travel": False,
        "intensity": "high",
        "mood_tags": ["scared", "terrified", "spooked"],
        "body_region": "full_body",
    },
    "dodging_right": {
        "category": "reaction",
        "loop": False,
        "travel": True,
        "intensity": "high",
        "mood_tags": ["evasive", "dodge", "urgent"],
        "body_region": "full_body",
        "evasion_dist_m": 1.4,
        "duration_s": 1.42,
    },
    "falling": {
        "category": "reaction",
        "loop": False,
        "travel": False,
        "intensity": "high",
        "mood_tags": ["fall", "down", "stumble"],
        "body_region": "full_body",
    },
    "head_hit_during_fight": {
        "category": "reaction",
        "loop": False,
        "travel": False,
        "intensity": "high",
        "mood_tags": ["hit", "impact", "pain"],
        "body_region": "upper_body",
    },
    "soccer_tripping_down": {
        "category": "reaction",
        "loop": False,
        "travel": False,
        "intensity": "high",
        "mood_tags": ["trip", "fall"],
        "body_region": "full_body",
    },
    "defeat_expression": {
        "category": "reaction",
        "loop": False,
        "travel": False,
        "intensity": "low",
        "mood_tags": ["relieved", "defeated", "disappointed"],
        "body_region": "upper_body",
    },
    "nervously_look_around": {
        "category": "reaction",
        "loop": False,
        "travel": False,
        "intensity": "low",
        "mood_tags": ["nervous", "suspicious", "cautious"],
        "body_region": "upper_body",
    },

    # Social & Gestures
    "acknowledging": {
        "category": "gesture",
        "loop": False,
        "travel": False,
        "intensity": "low",
        "mood_tags": ["calm", "nod", "talking", "friendly"],
        "body_region": "upper_body",
    },
    "pointing_forward": {
        "category": "gesture",
        "loop": False,
        "travel": False,
        "intensity": "medium",
        "mood_tags": ["point_at", "directing", "arguing", "heated"],
        "body_region": "upper_body",
    },
    "cheering": {
        "category": "gesture",
        "loop": False,
        "travel": False,
        "intensity": "high",
        "mood_tags": ["excited", "happy", "celebrating"],
        "body_region": "upper_body",
    },
    "clapping": {
        "category": "gesture",
        "loop": True,
        "travel": False,
        "intensity": "medium",
        "mood_tags": ["applause", "calm", "pleased"],
        "body_region": "upper_body",
    },
    "arm_stretching": {
        "category": "gesture",
        "loop": False,
        "travel": False,
        "intensity": "low",
        "mood_tags": ["relaxed", "casual", "idle"],
        "body_region": "upper_body",
    },
    "silly_dancing": {
        "category": "gesture",
        "loop": True,
        "travel": False,
        "intensity": "high",
        "mood_tags": ["dancing", "joyful", "eccentric"],
        "body_region": "full_body",
    },
    "singing": {
        "category": "gesture",
        "loop": True,
        "travel": False,
        "intensity": "medium",
        "mood_tags": ["expressive", "performing"],
        "body_region": "upper_body",
    },
    "kiss": {
        "category": "gesture",
        "loop": False,
        "travel": False,
        "intensity": "low",
        "mood_tags": ["affectionate", "greeting"],
        "body_region": "upper_body",
    },
    "praying": {
        "category": "gesture",
        "loop": True,
        "travel": False,
        "intensity": "low",
        "mood_tags": ["pleading", "reverent"],
        "body_region": "upper_body",
    },
    "pulling_lever": {
        "category": "gesture",
        "loop": False,
        "travel": False,
        "intensity": "medium",
        "mood_tags": ["interaction", "operating"],
        "body_region": "upper_body",
    },
    "opening_door_inwards": {
        "category": "gesture",
        "loop": False,
        "travel": False,
        "intensity": "medium",
        "mood_tags": ["door", "open"],
        "body_region": "upper_body",
    },

    # Combat & Contact
    "elbow_punch": {
        "category": "combat",
        "loop": False,
        "travel": False,
        "intensity": "high",
        "mood_tags": ["strike", "punch", "aggressive", "hitting"],
        "body_region": "upper_body",
        "reach_m": 0.60,
        "strike_limb": "RightArm",
        "contact_window_rel": [0.35, 0.55],
    },
    "left_hook": {
        "category": "combat",
        "loop": False,
        "travel": False,
        "intensity": "high",
        "mood_tags": ["strike", "punch", "aggressive", "hitting", "beating"],
        "body_region": "upper_body",
        "reach_m": 0.75,
        "strike_limb": "LeftHand",
        "contact_window_rel": [0.30, 0.50],
    },
    "kick_to_the_groin": {
        "category": "combat",
        "loop": False,
        "travel": False,
        "intensity": "high",
        "mood_tags": ["strike", "kick", "aggressive"],
        "body_region": "full_body",
        "reach_m": 0.95,
        "strike_limb": "RightFoot",
        "contact_window_rel": [0.40, 0.60],
    },
    "boxing_block": {
        "category": "combat",
        "loop": False,
        "travel": False,
        "intensity": "medium",
        "mood_tags": ["defensive", "cover", "block"],
        "body_region": "upper_body",
        "reach_m": 0.45,
    },
    "chapa_giratoria": {
        "category": "combat",
        "loop": False,
        "travel": True,
        "intensity": "high",
        "mood_tags": ["kick", "strike", "martial_arts"],
        "body_region": "full_body",
        "reach_m": 1.10,
        "strike_limb": "RightFoot",
        "contact_window_rel": [0.45, 0.65],
    },
    "martelo_2": {
        "category": "combat",
        "loop": False,
        "travel": True,
        "intensity": "high",
        "mood_tags": ["kick", "strike", "martial_arts"],
        "body_region": "full_body",
        "reach_m": 1.05,
        "strike_limb": "LeftFoot",
        "contact_window_rel": [0.40, 0.60],
    },
    "brutal_assassination": {
        "category": "combat",
        "loop": False,
        "travel": False,
        "intensity": "high",
        "mood_tags": ["attack", "strike", "takedown"],
        "body_region": "full_body",
        "reach_m": 0.85,
    },

    # Sitting & Rest
    "sitting": {
        "category": "sit",
        "loop": True,
        "travel": False,
        "intensity": "low",
        "mood_tags": ["seated", "resting"],
        "body_region": "full_body",
        "seat_offset_y": -0.45,
    },
    "sitting_clap": {
        "category": "sit",
        "loop": True,
        "travel": False,
        "intensity": "low",
        "mood_tags": ["seated", "applause"],
        "body_region": "full_body",
    },
    "crouch_to_stand": {
        "category": "sit",
        "loop": False,
        "travel": False,
        "intensity": "medium",
        "mood_tags": ["rise", "stand_up"],
        "body_region": "full_body",
    },
    "kneeling_down": {
        "category": "sit",
        "loop": False,
        "travel": False,
        "intensity": "low",
        "mood_tags": ["kneel", "crouch"],
        "body_region": "full_body",
    },
}

CHARACTER_CAPABILITIES = {
    "body_radius_m": 0.35,
    "max_jump_height_m": 0.90,
    "jump_distance_m": 2.0,
    "reach_m": 0.75,
    "run_speed_limit_mps": 5.0,
    "dodge_speed_mps": 3.8,
}

CHARACTER_GAIT_TABLE = {
    "walk": {
        "clip": "walk",
        "speed_mps": 1.4,
        "turn_rate_dps": 180.0,
    },
    "jog": {
        "clip": "jogging_with_box",
        "speed_mps": 2.2,
        "turn_rate_dps": 220.0,
    },
    "run": {
        "clip": "run",
        "speed_mps": 4.2,
        "turn_rate_dps": 270.0,
    },
    "sneak": {
        "clip": "sad_walk_arc_right",
        "speed_mps": 0.8,
        "turn_rate_dps": 120.0,
    },
}

CHARACTER_APPEARANCE_VARIANTS = [
    {
        "id": "blue_hero",
        "color_variant": "blue",
        "tint_rgb": [0.22, 0.45, 0.85],
        "height_scale": 1.00,
        "descriptive_tags": ["blue jacket", "average build", "hero"],
    },
    {
        "id": "red_jacket",
        "color_variant": "red",
        "tint_rgb": [0.85, 0.22, 0.18],
        "height_scale": 1.06,
        "descriptive_tags": ["tall", "red jacket", "mechanic"],
    },
    {
        "id": "green_jacket",
        "color_variant": "green",
        "tint_rgb": [0.24, 0.72, 0.35],
        "height_scale": 0.94,
        "descriptive_tags": ["short", "green jacket", "bystander"],
    },
    {
        "id": "yellow_jacket",
        "color_variant": "yellow",
        "tint_rgb": [0.88, 0.74, 0.18],
        "height_scale": 1.02,
        "descriptive_tags": ["yellow jacket", "average build", "driver"],
    },
    {
        "id": "dark_coat",
        "color_variant": "charcoal",
        "tint_rgb": [0.22, 0.22, 0.25],
        "height_scale": 1.05,
        "descriptive_tags": ["tall", "dark coat", "detective"],
    },
    {
        "id": "purple_jacket",
        "color_variant": "purple",
        "tint_rgb": [0.72, 0.38, 0.82],
        "height_scale": 0.96,
        "descriptive_tags": ["short", "purple jacket", "witness"],
    },
]

VEHICLE_DYNAMICS = {
    "max_speed_mps": 16.0,
    "accel_mps2": 4.0,
    "brake_gentle_mps2": 2.0,
    "brake_firm_mps2": 4.5,
    "brake_emergency_mps2": 7.5,
    "wheelbase_m": 2.7,
    "turn_radius_m": 5.5,
    "door_parts": ["door_fl", "door_fr", "door_rl", "door_rr", "hood", "trunk"],
}

CAR_SURFACES = {
    "roof": {
        "height_m": 1.47,
        "area_m2": 2.1,
        "center": [0.0, 1.47, -0.2],
        "usable_for": ["jump_on", "sit_on"],
    },
    "hood": {
        "height_m": 0.88,
        "area_m2": 1.4,
        "center": [0.0, 0.88, -1.6],
        "usable_for": ["jump_on", "sit_on"],
    },
    "trunk": {
        "height_m": 0.85,
        "area_m2": 1.1,
        "center": [0.0, 0.85, 1.5],
        "usable_for": ["jump_on", "sit_on"],
    },
}

SCENE_ZONE_MARKERS = {
    "lane_east": {
        "start": {"pos": [1.61, 0.0, -11.0], "facing_deg": 0.0, "note": "North entrance"},
        "stop_mark": {"pos": [1.61, 0.0, 5.0], "facing_deg": 0.0, "note": "Mid-bridge stop point"},
        "center": {"pos": [1.61, 0.0, 0.0], "facing_deg": 0.0, "note": "Center of northbound lane"},
    },
    "lane_west": {
        "start": {"pos": [-1.61, 0.0, 11.0], "facing_deg": 180.0, "note": "South entrance"},
        "stop_mark": {"pos": [-1.61, 0.0, -5.0], "facing_deg": 180.0, "note": "Mid-bridge stop point"},
        "center": {"pos": [-1.61, 0.0, 0.0], "facing_deg": 180.0, "note": "Center of southbound lane"},
    },
    "roadway": {
        "start": {"pos": [-0.60, 0.0, 9.5], "facing_deg": 0.0, "note": "South roadway entrance"},
        "north_start": {"pos": [-0.60, 0.0, -9.5], "facing_deg": 180.0, "note": "North roadway entrance"},
        "stop_mark": {"pos": [-0.60, 0.0, 4.15], "facing_deg": 0.0, "note": "South stopping position"},
        "north_stop": {"pos": [-0.60, 0.0, -3.80], "facing_deg": 180.0, "note": "North stopping position"},
        "clear_south": {"pos": [-1.90, 0.0, 0.60], "facing_deg": 0.0, "note": "Driver 1 clear point past car front bumper"},
        "clear_north": {"pos": [0.70, 0.0, -0.60], "facing_deg": 180.0, "note": "Driver 2 clear point past car front bumper"},
        "center_south": {"pos": [-0.60, 0.0, 0.60], "facing_deg": 0.0, "note": "Driver 1 standoff point facing north"},
        "center_north": {"pos": [-0.60, 0.0, -0.60], "facing_deg": 180.0, "note": "Driver 2 standoff point facing south"},
        "center": {"pos": [-0.60, 0.0, 0.0], "facing_deg": 0.0, "note": "Center of roadway"},
    },
    "sidewalk": {
        "start": {"pos": [3.10, 0.01, 9.5], "facing_deg": 0.0, "note": "South sidewalk start"},
        "stop_mark": {"pos": [3.10, 0.01, -2.0], "facing_deg": 0.0, "note": "Sidewalk stop mark"},
        "center": {"pos": [3.10, 0.01, 0.0], "facing_deg": 0.0, "note": "Center of sidewalk"},
    },
}


def enrich_manifest(manifest: Dict[str, Any], project_root: Optional[Path] = None) -> Dict[str, Any]:
    """Enrich the manifest dictionary in-memory with shot function library schemas."""
    m = dict(manifest)

    # 1. Enrich clips
    clips = dict(m.get("clips", {}))
    for name, cinfo in clips.items():
        enriched = dict(cinfo)
        cat_info = CLIP_CATEGORIES.get(name, {})
        for k, v in cat_info.items():
            enriched.setdefault(k, v)
        # Ensure duration_s is present
        if "duration_s" not in enriched:
            # default fallback: if stride_speed_mps given or default 24fps
            enriched["duration_s"] = cat_info.get("duration_s", 1.5)
        clips[name] = enriched
    m["clips"] = clips

    # 2. Enrich assets
    assets = list(m.get("assets", []))
    for asset in assets:
        aid = asset.get("id", "")
        # Human characters
        if aid in ("character.human", "character.male", "character.female", "humans.xbot", "humans.male", "humans.female") or "character" in asset.get("tags", []):
            asset.setdefault("capabilities", CHARACTER_CAPABILITIES)
            asset.setdefault("gait_table", CHARACTER_GAIT_TABLE)
            asset.setdefault("appearance_variants", CHARACTER_APPEARANCE_VARIANTS)
        # Vehicles
        elif aid.startswith("cars.") or aid.startswith("vehicle.") or "car" in asset.get("tags", []):
            asset.setdefault("dynamics", VEHICLE_DYNAMICS)
            asset.setdefault("surfaces", CAR_SURFACES)
            anchors = dict(asset.get("anchors", {}))
            anchors.setdefault("driver_seat", {"pos": [-0.36, 0.35, -0.15], "facing_deg": 0.0, "note": "Driver seat position facing forward behind steering wheel"})
            anchors.setdefault("passenger_seat", {"pos": [0.36, 0.35, -0.15], "facing_deg": 0.0, "note": "Front passenger seat position facing forward"})
            anchors.setdefault("seat_rear_left", {"pos": [-0.36, 0.35, 0.75], "facing_deg": 0.0, "note": "Rear left passenger seat facing forward"})
            anchors.setdefault("seat_rear_right", {"pos": [0.36, 0.35, 0.75], "facing_deg": 0.0, "note": "Rear right passenger seat facing forward"})
            anchors.setdefault("hood_strike", {"pos": [0.0, 0.0, -1.9], "facing_deg": 0.0, "note": "Front hood strike point"})
            anchors.setdefault("door_fl_strike", {"pos": [-1.2, 0.0, -0.2], "facing_deg": 90.0, "note": "Driver side strike point"})
            anchors.setdefault("door_fl_stand", {"pos": [-1.35, 0.0, 0.15], "facing_deg": 90.0, "note": "Driver side stand point facing car door"})
            anchors.setdefault("door_fr_stand", {"pos": [1.35, 0.0, 0.15], "facing_deg": 270.0, "note": "Passenger side stand point facing car door"})
            anchors.setdefault("door_rl_stand", {"pos": [-1.35, 0.0, 0.85], "facing_deg": 90.0, "note": "Rear driver-side stand point facing car door"})
            anchors.setdefault("door_rr_stand", {"pos": [1.35, 0.0, 0.85], "facing_deg": 270.0, "note": "Rear passenger-side stand point facing car door"})
            asset["anchors"] = anchors
        # Buildings / Architectural Assets (Levels and Stairs)
        elif aid.startswith("buildings.") or "building" in asset.get("tags", []) or "stairs" in asset:
            levels = list(asset.get("levels", []))
            if not levels:
                levels = [{"id": "L0", "floor_y": 0.0}]
            asset["levels"] = levels

            if "stairs" in asset:
                stairs = dict(asset["stairs"])
                anchors = dict(asset.get("anchors", {}))
                for s_id, s_data in stairs.items():
                    b_anch = s_data.get("anchors", {}).get("bottom") or f"{s_id}_bottom"
                    t_anch = s_data.get("anchors", {}).get("top") or f"{s_id}_top"
                    if "centerline" in s_data and len(s_data["centerline"]) >= 2:
                        p_bot = s_data["centerline"][0]
                        p_top = s_data["centerline"][-1]
                        anchors.setdefault(b_anch, {"pos": list(p_bot), "facing_deg": 180.0, "note": f"Bottom entry for {s_id}"})
                        anchors.setdefault(t_anch, {"pos": list(p_top), "facing_deg": 180.0, "note": f"Top exit for {s_id}"})
                asset["anchors"] = anchors
    m["assets"] = assets

    # 3. Add zone markers
    m["zone_markers"] = SCENE_ZONE_MARKERS

    return m

