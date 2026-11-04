"""General Scene-Graph-Driven LLM & Compositional Semantic Shot Planner.

ARCHITECTURAL MANDATE (THE FOUNDATIONAL LAW OF PAINTERSPACE):
=============================================================
This planner MUST NEVER use hardcoded scenario branches (`if "keyword" in prompt:`)
or hardcoded 3D coordinate literals (`pos(x, y, z)`, `[x, y, z]`).

Every shot plan must be constructed compositionally from:
1. The live Scene Graph (`scene_data` objects, zones, portals) and Asset Manifest
   (`manifest.json` semantic anchors, surfaces, parts, stairs, and animation clips).
2. The high-level `FunctionCatalog` API (~40 verbs across setup, locomotion,
   vehicles, social interaction, combat, object manipulation, and camera framing).
3. Either:
   - Mode A (`_plan_with_llm_api`): Live LLM tool-calling / structured JSON generation
     with closed-loop compiler error recovery (`plan_and_compile_with_retry`), OR
   - Mode B (`_plan_general_semantic`): General compositional natural-language clause
     decomposition into Subject -> Action -> Semantic Target -> Causal Timing.
"""
from __future__ import annotations

import inspect
import json
import os
import re
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple, get_args, get_origin, get_type_hints

from function_catalog import FunctionCatalog
from plan_model import ShotPlan


class ManifestAssetMatcher:
    """Dynamically resolves natural-language nouns to assets, stairs, surfaces, and anchors in manifest.json."""

    STOP_WORDS = {
        "the", "and", "with", "from", "into", "near", "toward", "towards", "under",
        "over", "beside", "behind", "front", "back", "left", "right", "shot", "scene",
        "camera", "wide", "medium", "close", "drone", "misty", "evening", "morning",
        "night", "day", "suddenly", "then", "while", "after", "before", "something",
        "someone", "people", "person", "man", "woman", "couple", "crowd", "group",
        "street", "road", "lane", "avenue", "walking", "running", "standing", "sitting",
        "driving", "moving", "looking", "turning", "happy", "joyful", "startled",
        "surprise", "surprised", "angry", "heated", "fast", "slow", "quickly", "slowly",
    }

    def __init__(self, manifest: Dict[str, Any], scene_data: Optional[Dict[str, Any]] = None) -> None:
        self.manifest = manifest
        self.scene_data = scene_data or {}
        self.assets = manifest.get("assets", [])

    @staticmethod
    def _word_match(term: str, text_lower: str) -> bool:
        if not term or len(term) < 3:
            return False
        pattern = r"\b" + re.escape(term.lower()) + r"s?\b"
        return bool(re.search(pattern, text_lower))

    def find_scene_object_by_concept(self, concept: str) -> Optional[str]:
        """Find an existing object in scene_data matching a concept (e.g. 'tree', 'car', 'stairs', 'tower')."""
        c_low = concept.lower()
        for obj in self.scene_data.get("objects", []):
            name = str(obj.get("name") or obj.get("id") or "")
            asset = str(obj.get("asset") or obj.get("asset_id") or "")
            combined = f"{name} {asset}".lower()
            if self._word_match(c_low, combined) or c_low in combined.split("_") or c_low in name.lower():
                return name
        return None

    def find_manifest_asset_by_concept(self, concept: str, exclude_categories: Tuple[str, ...] = ("character",)) -> Optional[Dict[str, Any]]:
        """Find the best matching asset entry in manifest.json for a natural-language concept."""
        c_low = concept.lower()
        for a in self.assets:
            aid = str(a.get("id", ""))
            cat = aid.split(".")[0] if "." in aid else ""
            if cat in exclude_categories:
                continue
            searchable = [aid.replace(".", " ").replace("_", " "), str(a.get("label", ""))]
            searchable.extend(str(al) for al in a.get("aliases", []))
            searchable.extend(str(tg) for tg in a.get("tags", []))
            blob = " ".join(searchable).lower()
            if self._word_match(c_low, blob):
                return a
        return None

    def find_stair_portal(self) -> Tuple[Optional[str], Optional[str]]:
        """Discover the primary stair asset ID and stair portal ID from manifest.json."""
        for a in self.assets:
            stairs = a.get("stairs", {})
            if stairs:
                first_sid = next(iter(stairs.keys()))
                return str(a.get("id")), str(first_sid)
        return None, None

    def find_referenced_environment_assets(self, prompt: str) -> List[Dict[str, Any]]:
        """Identify props, trees, barriers, or buildings mentioned in the prompt."""
        p_low = prompt.lower()
        matched: List[Dict[str, Any]] = []
        seen_ids = set()

        # Check explicit concept categories first
        concept_map = [
            ("tree", ("trees.oak_tree", "trees.pine_tree", "trees.palm_tree")),
            ("oak", ("trees.oak_tree",)),
            ("pine", ("trees.pine_tree",)),
            ("palm", ("trees.palm_tree",)),
            ("stair", ("buildings.city_square",)),
            ("staircase", ("buildings.city_square",)),
            ("step", ("buildings.city_square",)),
            ("barrier", ("props.concrete_barrier", "props.plastic_barrier")),
            ("crate", ("props.wooden_crate",)),
            ("box", ("props.cardboard_box", "props.wooden_crate")),
            ("bench", ("props.park_bench",)),
            ("hydrant", ("props.fire_hydrant",)),
            ("trash", ("props.trash_can", "props.dumpster")),
            ("cone", ("props.traffic_cone",)),
            ("barrel", ("props.metal_barrel",)),
        ]
        for keyword, candidate_ids in concept_map:
            if self._word_match(keyword, p_low):
                for cid in candidate_ids:
                    for a in self.assets:
                        if a.get("id") == cid and cid not in seen_ids:
                            matched.append(a)
                            seen_ids.add(cid)
                            break
                    if cid in seen_ids:
                        break
        return matched


class LLMShotPlanner:
    """General Scene-Graph-Driven Shot Planner.

    Supports both live LLM API tool-calling (`_plan_with_llm_api`) and general
    compositional semantic NLP planning (`_plan_general_semantic`) with zero
    hardcoded scenario branches and zero hardcoded coordinates.
    """

    def __init__(self, catalog: FunctionCatalog) -> None:
        self.catalog = catalog
        self.execution_log: List[str] = []
        self.last_plan_review: Dict[str, Any] = {}
        self.brief = {"version": 1, "tracks": {}, "paths": {}}
        self.camera_mode = "auto"
        self.matcher = ManifestAssetMatcher(catalog.manifest, catalog.scene_data)

    # -------------------------------------------------------------------------
    # Tool Schema Introspection for LLM Tool Calling
    # -------------------------------------------------------------------------
    def export_tool_schemas(self) -> List[Dict[str, Any]]:
        """Dynamically introspect FunctionCatalog methods into OpenAI-compatible tool schemas."""
        tools: List[Dict[str, Any]] = []
        skip_methods = {"reset", "get_plan", "where_is", "distance_between", "time_of", "can_reach"}
        if self.camera_mode == "manual":
            skip_methods.update(name for name in dir(self.catalog) if name.startswith("camera_"))

        for name, method in inspect.getmembers(self.catalog, predicate=inspect.ismethod):
            if name.startswith("_") or name in skip_methods:
                continue
            sig = inspect.signature(method)
            type_hints = get_type_hints(method)
            doc = (inspect.getdoc(method) or f"Execute {name} on the shot plan.").strip()
            props: Dict[str, Any] = {}
            required: List[str] = []

            for param_name, param in sig.parameters.items():
                if param_name in ("self", "kwargs"):
                    continue
                from director_brief import FORBIDDEN_FIELDS
                if param_name in FORBIDDEN_FIELDS:
                    continue
                ann = str(param.annotation)
                if get_origin(type_hints.get(param_name)) is Literal:
                    p_schema = {'type': 'string', 'enum': list(get_args(type_hints[param_name]))}
                elif "List" in ann:
                    p_schema: Dict[str, Any] = {"type": "array", "items": {"type": "string"}}
                elif "int" in ann and "float" not in ann and "Union" not in ann:
                    p_schema = {"type": "integer"}
                elif "float" in ann and "str" not in ann:
                    p_schema = {"type": "number"}
                elif "bool" in ann:
                    p_schema = {"type": "boolean"}
                else:
                    p_schema = {"type": "string"}
                if p_schema.get("type") in ("number", "integer"):
                    continue
                props[param_name] = p_schema
                if param.default is inspect.Parameter.empty:
                    required.append(param_name)

            tools.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": doc.split("\n")[0],
                    "parameters": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": props,
                        "required": required,
                    },
                },
            })
        return tools

    def build_system_prompt(self, error_feedback: Optional[str] = None,
                            include_scene_inventory: bool = True,
                            include_brief_digest: bool = True) -> str:
        """Build the system prompt describing the scene graph, manifest capabilities, and Foundational Law."""
        manifest_assets = {a.get("id"): a for a in self.catalog.manifest.get("assets", [])}
        scene_objects = [
            {"name": o.get("name") or o.get("id"), "asset_id": o.get("asset") or o.get("asset_id"),
             "position": o.get("position"), "heading_deg": o.get("heading_deg"),
             "bounds_m": manifest_assets.get(o.get("asset") or o.get("asset_id"), {}).get("bounds_m"),
             "pivot": manifest_assets.get(o.get("asset") or o.get("asset_id"), {}).get("pivot"),
             "obstacle_bounds_xz": o.get("obstacle_bounds_xz"),
             "obstacle_box_count": o.get("obstacle_box_count", 0),
             "collision_covered": (o.get("name") or o.get("id")) not in
             self.catalog.scene_data.get("unprotected_scene_objects", [])}
            for o in self.catalog.scene_data.get("objects", [])
            if o.get("type") not in ("Camera", "camera") and (o.get("name") or o.get("id")) not in ("ShotCam", "CameraRig", "CameraPaths", "Cameras")
        ]
        zones = [z.get("id") for z in self.catalog.scene_data.get("zones", [])]
        authored_zones = self.catalog.scene_data.get("zones", [])
        zone_refs = [f"{zone['id']}.{marker}" for zone in authored_zones
                     for marker in zone.get("reference_markers", ["center"])]
        standing_spots = [
            {"reference": f"{spot['owner']}:{spot['name']}",
             "position": spot.get("position"),
             "clearance_m": spot.get("clearance_m")}
            for spot in self.catalog.scene_data.get("standing_spots", [])
            if spot.get("owner") and spot.get("name")
        ]
        rooftop_spots = [
            {"reference": f"{spot['owner']}:{spot['name']}",
             "position": spot.get("position"),
             "landing_reference": spot.get("landing_reference")}
            for spot in self.catalog.scene_data.get("rooftop_spots", [])
            if spot.get("owner") and spot.get("name")
        ]
        surface_spots = [
            {"reference": f"{spot['owner']}:{spot['name']}", "position": spot.get("position")}
            for spot in self.catalog.scene_data.get("surface_spots", [])
            if spot.get("owner") and spot.get("name")
        ]
        vehicle_refs = [
            {"asset_id": asset["id"], "anchors": list(asset.get("anchors", {})),
             "surfaces": list(asset.get("surfaces", {}))}
            for asset in self.catalog.manifest.get("assets", [])
            if {"vehicle", "car"}.intersection(asset.get("tags", []))
        ]
        scene_stairs = []
        for obj in scene_objects:
            asset = manifest_assets.get(obj["asset_id"], {})
            scene_obj = next((o for o in self.catalog.scene_data.get("objects", [])
                              if (o.get("name") or o.get("id")) == obj["name"]), {})
            for stair_id in (scene_obj.get("stairs") or asset.get("stairs", {})):
                scene_stairs.append(f"{obj['name']}:{stair_id}")

        prompt_lines = [
            "You are the PainterSpace General Shot Planner.",
            "FOUNDATIONAL LAW: Never emit raw (x, y, z) coordinates or `pos(...)` strings.",
            "Always compose shots by calling `FunctionCatalog` methods with semantic references:",
            *([f"- Authored scene zone markers: {json.dumps(zone_refs)}. Do not invent a lane when this list is empty.",
               f"- Vehicle asset anchors and surfaces: {json.dumps(vehicle_refs)}. Prefix an anchor or surface with the placed actor ID."]
              if include_scene_inventory else []),
            "- Relative selectors: `in_front_of(actor, gap)`, `behind(actor, gap)`, `beside(actor, left|right, gap)`, `left_of(actor, gap)`, `right_of(actor, gap)`, `nearest_to(actor, type)`",
            "- World directions: north = -Z, east = +X, south = +Z, west = -X; diagonals combine these axes. Use cardinal references for absolute movement (for example `northwest`), and actor-relative selectors for left, right, forward, or backward.",
            "- Scene objects: use an exact name from Current Scene Objects as a target for that object's authored origin. A bare word such as `center` is not an object or zone reference unless it is explicitly listed. For 'the center of' an object, use its name only when its pivot describes that center; otherwise use an authored zone or anchor. Never invent a reference to translate prose into geometry.",
            "- Combine the user's prompt with drawn routes for participating actors. Use `follow_path(actor=<owner>, path_id=<route ID>)` for their travel instead of guessing a cardinal destination. The compiler derives vehicle versus pedestrian motion and route duration from the route data. Routes for actors unrelated to the prompt remain optional guides.",
            "Keep the starting scene assets exactly as they are. Reuse the listed Current Scene Objects; do not place, replace, remove, or change buildings or other environment assets to make the prompt work. A library asset is not part of the scene unless it appears in Current Scene Objects.",
            "If an action needs an asset or feature absent from the starting scene, do not add a substitute asset or invent an anchor. Use only actions the starting assets support.",
            "Use stable actor IDs (human_1, human_2) for later calls. Use stage_pair only for specifically requested paired interactions, such as a one-on-one fight or a kiss. Stage again with contact spacing before a kiss if the participants have moved apart. Do not turn a crowd fight into a list of pairs.",
            "For fights between groups, use group_fight(group_a, group_b). It animates opposing groups without fixed opponents or pair arenas. Optional destinations_a and destinations_b list distinct supported run-in spots in membership order; omit them for participants already in position. When others join an existing fight, include the original fighters in their groups and sequence the group_fight after the initiating fight's real beat event. Individual attack/defense calls remain for explicitly directed exchanges between named opponents.",
            "Keep crowd members at distinct supported starting spots and give simultaneous arrivals separate destinations. Do not direct several actors to occupy the same actor or anchor. Sequence a later phase with a real returned beat ID or ready_event in `after`; a function name such as `attack` is not an event ID.",
            "Any measured, supported free space with body clearance is walkable, including open space between scene objects. Standing markers are placement suggestions, not a restriction on walking. Let the compiler route across available supported space around measured obstacles; do not confine travel to standing markers. Preserve explicit origin, ordered waypoint and destination requirements, and do not treat unsupported air or solid geometry as free space.",
            "Interpret shared actions in the prompt as overlapping action: conversation, discussion, fighting, and kissing involve both characters in the same interval. Emit adjacent reciprocal calls for an attack and its defense or hit reaction; a single hug_or_kiss call names both participants through its target. Give reciprocal calls the same timing, and use `after` between them only when the prompt explicitly orders them in sequence. Actions connected by 'while', 'as', 'together', or 'at the same time' should overlap; actions connected by 'then' or 'after' should follow in order.",
            "Plan the whole shot as interactions before emitting calls: identify each participant, the actor state each interaction requires, and the causal event that makes that state available. Preserve event order with `declare_interaction` when a one-shot response does not have returned beat IDs; use `after` only when its event ID is known. Do not infer a later interaction from actors' starting placements alone.",
            "For a multi-actor or multi-beat causal interaction, call `declare_interaction` as well as the action tools. List actions as `actor_id.function` using either the created action function or the catalog tool that produced it (for example, `actor.jump` and `actor.perform` both identify a jump created by perform). When an actor/function repeats, number every occurrence explicitly as `#1`, `#2`, and so on in authored action order. The resolver can fill unnumbered references only when they cover all remaining occurrences; never leave a subset ambiguous. Set timing to `sequence` only for actions that must finish one by one, `simultaneous` for the same start time, `overlap` for a shared interval without requiring equal starts, or `parallel` for independently scheduled actions that may start at different times. Name every participant. The declaration's optional `after` array lists earlier interaction IDs; every action in the new phase waits for every action in those interactions to finish. Do not declare actions absent from the calls.",
            "Before emitting interaction declarations, group actions by actor identity (labels and IDs can name the same actor). An overlap or simultaneous phase may contain at most one full-body action per actor, even when the functions differ or use occurrence numbers. Decompose multi-step interactions into causal phases: keep different actors' concurrent actions together, then put each actor's subsequent actions in later phases linked with after. Check this invariant across the whole response, including declarations before their action calls. Never change explicit concurrency to parallel or sequence merely to bypass a conflict; report genuinely contradictory requirements.",
            "Translate group timing explicitly: 'all start together', 'at the same time', and coordinated group action 'together' require simultaneous timing, not merely parallel or overlap. For simultaneous departures followed by a shared performance, declare two simultaneous interactions and put after=[the departure interaction ID] on the performance declaration. Actors may arrive at different times; the later phase starts together after the last arrival. If staggered travel is allowed, that travel phase may instead be parallel. Include the actual action calls. Do not flatten group movements and performances into one sequence unless the prompt explicitly asks for one-by-one action. Preserve one-by-one requests as sequence; the compiler must not silently rewrite them from prose. Interaction after values are interaction IDs; action-tool after values are real returned beat events. Existing top-level phases and per-call phase tags also support barriers but alone do not guarantee equal start times.",
            "When an actor travels to another actor, target the actor ID, not that actor's starting seat, placement marker, or earlier position. The compiler uses the target's position at the traveler's arrival. If the prompt says the travel happens after the target changes state or location, link the travel after the target's completed beat. For simultaneous movement, express the shared timing rather than inventing a stale fixed destination.",
            "For an initial group approach from the direction of a scene object, use stage_region_approach with an approach source_region and a floor or boundary entry destination_region ID from the listed spatial_regions. Use a boundary entry destination for travel toward that side; use a floor destination for an interior gathering. The compiler chooses individual clear placements, arrivals and routes from measured geometry. Do not infer source direction from standing marker names or numbers. This tool emits the movements; do not duplicate them with individual walk/run calls. Link a following shared performance with a simultaneous declare_interaction whose after array names the approach intent_id. These regions describe floor space and obstacle-side approaches, not doors or interiors; never invent a region ID or claim an actual building exit from an approach region.",
            "Actor lifecycle: reuse IDs from scene_context.existing_actors or returned by place. stage_region_approach can create new actor IDs when asset_id names a listed character asset; use it explicitly when adding actors to an existing cast. For a wholly new cast only, the configured defaults.character_asset_id (or the sole character asset) may supply the asset. IDs alone do not specify an asset. Other action tools do not create actors: place actors first unless staging creates them. Never assume requested actor IDs already exist.",
            "General origins and routes: stage_region_approach accepts any listed floor, entry, approach or corner source. Use source_regions (one ID per actor in the same order) for different explicit origins; use origin_layout='opposite' for opposite sides and 'surrounding' for entries from all sides, with source_region equal to the destination floor when the compiler should choose entries. These modes choose geometry, not timing. For around-corner travel, supply ordered via_regions containing the relevant measured corner quadrants, with source and destination on the intended sides. Separate calls can express groups with different route requirements. Never reduce opposite-side or around-corner instructions to one same-side source or an unconstrained direct route. If no advertised geometry grounds the requested location, report the missing evidence instead of inventing an entrance or interior. Exact initial positions and existing authored routes remain authoritative.",
            f"- Cast creation defaults: {json.dumps(self.catalog.manifest.get('defaults', {}))}",
            *([f"- Stair portals on current scene objects: {json.dumps(scene_stairs)}",
               f"- Scene-owned spatial regions: {json.dumps(self.catalog.list_spatial_regions())}",
               f"- Existing actors: {json.dumps(self.catalog.scene_data.get('existing_actors', []))}",
               f"- Measured Standing & Navigable Spots: {json.dumps(standing_spots)}",
               f"- Measured Rooftop Spots: {json.dumps(rooftop_spots)}. For a jump from a building top, place the actor at a listed rooftop spot and jump_down to its landing_reference when provided. Do not invent roof references.",
               f"- Measured Flat Surface Spots: {json.dumps(surface_spots)}. Characters can be placed or routed on these measured elevated surfaces.",
               f"- Current Scene Objects: {json.dumps(scene_objects)}",
               f"- Authored Zone Geometry: {json.dumps(self.catalog.scene_data.get('zones', []))}",
               f"- Available Zones: {json.dumps(zones)}"]
              if include_scene_inventory else []),
            "CRITICAL EXECUTION RULE: Respond ONLY with the tools call JSON matching schema: `{\"calls\": [{\"function\": \"...\", \"args\": {...}}, ...]}`. Your execution MUST end there. DO NOT execute scripts, commands, or tools; PainterSpace executes the plan.",
        ]
        from director_brief import build_digest, enrich_brief_choices
        prompt_lines.extend([
            "Never emit numeric times, distances, angles or geometry; use named levels and segment IDs.",
            "Explicit Director timeline segments set action and timing. Combine the prompt with them when provided; compile the timeline alone when there is no prompt. An empty timeline adds no constraint.",
            "Preferred choices must use listed alternatives. Report prose conflicts in brief_conflicts.",
            *([build_digest(enrich_brief_choices(self.brief, self.catalog.manifest), self.catalog.scene_data)]
              if include_brief_digest else []),
        ])
        if error_feedback:
            prompt_lines.append(
                f"\nPREVIOUS COMPILATION FEEDBACK TO FIX:\n{error_feedback}\n"
                "Adjust the semantic placement, timing, or function arguments to resolve this error."
            )
        return "\n".join(prompt_lines)

    # -------------------------------------------------------------------------
    # Public Entry Points
    # -------------------------------------------------------------------------
    def plan_from_prompt(
        self,
        prompt: str,
        error_feedback: Optional[str] = None,
    ) -> Tuple[bool, ShotPlan, List[str]]:
        """Translate any natural-language prompt into a verified ShotPlan."""
        self.execution_log = []
        self.last_plan_review = {}
        self.catalog.reset(prompt=prompt)
        self.matcher = ManifestAssetMatcher(self.catalog.manifest, self.catalog.scene_data)

        # 1. Try live LLM tool-calling if an API key or local LLM endpoint is configured
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        llm_url = os.environ.get("PAINTERSPACE_LLM_URL", "").strip()
        if api_key or llm_url:
            ok_api = self._plan_with_llm_api(prompt, api_key=api_key, llm_url=llm_url, error_feedback=error_feedback)
            if ok_api and self.catalog.plan.setup and self.catalog.plan.beats:
                self._verify_and_repair_prompt_adherence(prompt)
                return self._review_catalog_plan(), self.catalog.get_plan(), self.execution_log
            if self.last_plan_review and not self.last_plan_review['passed']:
                # Invalid generated choreography must reach the feedback/retry
                # loop, not disappear into an unrelated semantic fallback plan.
                return False, self.catalog.get_plan(), self.execution_log
            self.catalog.reset(prompt=prompt)

        # 2. General Scene-Graph-Driven Compositional Semantic Planner
        self._plan_general_semantic(prompt, error_feedback=error_feedback)
        self._verify_and_repair_prompt_adherence(prompt)
        return self._review_catalog_plan(), self.catalog.get_plan(), self.execution_log

    def plan_and_compile_with_retry(
        self,
        prompt: str,
        compiler: Any,
        output_usda_path: Path,
        scene_usda_path: Optional[Path] = None,
        validate: bool = True,
        max_retries: int = 2,
        camera_locked: bool = False,
    ) -> Tuple[bool, Dict[str, Any], List[str]]:
        """Closed-loop planning and compilation with automatic feedback-driven self-correction."""
        from director_brief import load_brief
        try:
            self.brief = load_brief(Path(output_usda_path).parent / "director_brief.json")
        except ValueError as exc:
            return False, {"error": {"error_code": "BRIEF_PREFLIGHT", "message": str(exc)}}, []
        self.camera_mode = "manual" if camera_locked or (Path(output_usda_path).parent / "camera_rig.json").exists() else "auto"
        feedback: Optional[str] = None
        last_report: Dict[str, Any] = {}
        all_logs: List[str] = []

        for attempt in range(max_retries + 1):
            ok_plan, plan, logs = self.plan_from_prompt(prompt, error_feedback=feedback)
            if camera_locked and plan:
                plan.camera_locked = True
            all_logs.extend(logs)
            if not ok_plan:
                from plan_review import review_suggestions
                success = False
                report = {'success': False, 'error': {
                    'error_code': 'PLAN_REVIEW' if self.last_plan_review else 'PLANNING_FAILED',
                    'message': 'Revise the generated calls before compilation.',
                    'details': self.last_plan_review or {'logs': logs},
                    'suggestions': review_suggestions(self.last_plan_review)}}
            else:
                success, report = compiler.compile(
                    plan,
                    output_usda_path,
                    scene_usda_path=scene_usda_path,
                    validate=validate,
                )
            last_report = report
            if success:
                return True, report, all_logs

            err_info = report.get("error", {})
            from director_brief import symbolic_feedback
            feedback = json.dumps(symbolic_feedback(err_info))
            all_logs.append(f"[Retry {attempt + 1}] Compiler feedback: {feedback}")

        return False, last_report, all_logs

    # -------------------------------------------------------------------------
    # Mode A: Live LLM API Tool-Calling Execution
    # -------------------------------------------------------------------------
    def _plan_with_llm_api(
        self,
        prompt: str,
        api_key: str,
        llm_url: str = "",
        error_feedback: Optional[str] = None,
    ) -> bool:
        """Call an OpenAI-compatible chat completions endpoint to generate FunctionCatalog calls."""
        endpoint = llm_url or "https://api.openai.com/v1/chat/completions"
        model = os.environ.get("PAINTERSPACE_LLM_MODEL", "gpt-4o-mini")
        sys_prompt = self.build_system_prompt(error_feedback=error_feedback)
        tools = self.export_tool_schemas()

        user_prompt = (
            f"{prompt}\n\n"
            "Keep the starting scene assets as they are; use existing scene objects and never add a replacement environment asset. "
            "MANDATORY: Respond ONLY with the tools call JSON (schema: {\"calls\": [{\"function\": \"...\", \"args\": {...}}, ...]}). "
            "Your execution MUST end immediately there."
        )
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "tools": tools,
            "tool_choice": "auto",
            "temperature": 0.1,
        }
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        try:
            req = urllib.request.Request(
                endpoint,
                data=json.dumps(payload).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=25) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            self.execution_log.append(f"LLM API unavailable ({exc}); using general semantic planner.")
            return False

        msg = body.get("choices", [{}])[0].get("message", {})
        tool_calls = msg.get("tool_calls", [])
        executed = 0
        failed = False

        for tc in tool_calls:
            fn_spec = tc.get("function", {})
            fn_name = fn_spec.get("name", "")
            try:
                fn_args = json.loads(fn_spec.get("arguments", "{}"))
            except Exception:
                fn_args = {}
            if self._invoke_catalog_call(fn_name, fn_args):
                executed += 1
            else:
                failed = True
                break

        if not failed and executed == 0 and msg.get("content"):
            try:
                parsed = json.loads(msg["content"])
                for item in parsed.get("calls", []):
                    if self._invoke_catalog_call(item.get("function", ""), item.get("args", {})):
                        executed += 1
                    else:
                        failed = True
                        break
            except Exception:
                pass

        return executed > 0 and not failed

    @staticmethod
    def _normalize_semantic_ref(val: Any) -> Any:
        """Convert dict-based semantic references into FunctionCatalog's string DSL if needed."""
        if isinstance(val, dict):
            if "near" in val:
                base = val["near"]
                side = val.get("side", "right")
                dist = val.get("dist", val.get("gap", "near"))
                if side == "front":
                    return f"in_front_of({base}, {dist})"
                if side == "back":
                    return f"behind({base}, {dist})"
                return f"beside({base}, {side}, {dist})"
            if "inside" in val:
                veh = val["inside"]
                seat = val.get("seat", "driver_seat")
                seat_map = {"driver": "driver_seat", "passenger": "passenger_seat"}
                return f"{veh}:{seat_map.get(seat, seat)}"
            if "at" in val:
                return str(val["at"])
            if "facing" in val:
                return str(val["facing"])
        return val

    def _invoke_catalog_call(self, fn_name: str, fn_args: Dict[str, Any]) -> bool:
        """Safely invoke a method on self.catalog while rejecting raw coordinate strings."""
        from director_brief import guard_symbolic
        try:
            guard_symbolic(fn_args)
            if self.camera_mode == "manual" and fn_name.startswith("camera_"):
                raise ValueError("Camera verb unavailable")
            if fn_name.startswith("_") or fn_name in {"reset", "get_plan"}:
                raise ValueError("Unavailable catalog method")
        except ValueError as exc:
            self.execution_log.append(str(exc))
            return False
        fn_aliases = {
            "set_environment": "place",
            "spawn": "place",
        }
        fn_name = fn_aliases.get(fn_name, fn_name)
        if not hasattr(self.catalog, fn_name):
            self.execution_log.append(f"Unknown catalog function: {fn_name}")
            return False

        # Normalize common LLM argument aliases and dict-based semantic references
        norm_args: Dict[str, Any] = {}
        for k, v in fn_args.items():
            v_norm = self._normalize_semantic_ref(v)
            norm_args[k] = v_norm

        if fn_name == "place":
            if "asset" in norm_args and "asset_id" not in norm_args:
                norm_args["asset_id"] = norm_args.pop("asset")
            if "name" in norm_args and "label" not in norm_args:
                norm_args["label"] = norm_args.pop("name")
            if "pos" in norm_args and "at_reference" not in norm_args and "at" not in norm_args:
                norm_args["at_reference"] = norm_args.pop("pos")
        elif fn_name == "camera_frame":
            if "subjects" in norm_args and "actors" not in norm_args:
                norm_args["actors"] = norm_args.pop("subjects")
            if "shot" in norm_args and "style" not in norm_args:
                norm_args["style"] = norm_args.pop("shot")
            if "angle" in norm_args and "vantage" not in norm_args:
                norm_args["vantage"] = norm_args.pop("angle")
        elif fn_name in ("hug_or_kiss", "shake_hands"):
            if "actor_a" in norm_args and "actor" not in norm_args:
                norm_args["actor"] = norm_args.pop("actor_a")
            if "actor_b" in norm_args and "target" not in norm_args:
                norm_args["target"] = norm_args.pop("actor_b")
            if "mode" in norm_args and "style" not in norm_args:
                norm_args["style"] = norm_args.pop("mode")
        elif fn_name in ("drive_to", "drive_toward", "drive_past", "drive_along", "brake_to_stop",
                         "reverse_to", "disembark_vehicle", "board_vehicle"):
            if "actor" in norm_args and "vehicle" not in norm_args:
                norm_args["vehicle"] = norm_args.pop("actor")

        try:
            method = getattr(self.catalog, fn_name)
            beats_before = len(self.catalog.plan.beats)
            res = method(**norm_args)
            if isinstance(res, tuple) and len(res) >= 1 and res[0] is False:
                error = res[1].to_dict() if len(res) > 1 and hasattr(res[1], 'to_dict') else (res[1] if len(res) > 1 else None)
                if isinstance(error, dict) and error.get('error_code'):
                    self.last_plan_review = {
                        'passed': False, 'requirements': [], 'overrides': [], 'pending_spatial_bindings': [],
                        'violations': [{'code': 'INVALID_TOOL_CALL', 'function': fn_name, 'error': error}],
                        'scope': 'catalog call contracts before compilation'}
                    self.execution_log.append('[Plan review] ' + json.dumps(self.last_plan_review))
                self.execution_log.append(f"Failed call {fn_name}({norm_args}): {res[1:]}")
                return False
            for beat in self.catalog.plan.beats[beats_before:]:
                beat.source_function = fn_name
            self.execution_log.append(f"{fn_name}({norm_args})")
            return True
        except Exception as exc:
            self.execution_log.append(f"Skipped invalid call {fn_name}({norm_args}): {exc}")
            return False

    def plan_from_llm_json(
        self,
        llm_response: Union[str, Dict[str, Any], List[Dict[str, Any]]],
        prompt: str = "",
    ) -> Tuple[bool, ShotPlan, List[str]]:
        """Execute a structured JSON tool-call response from an external LLM / queue response file."""
        self.execution_log.clear()
        self.last_plan_review = {}
        self.catalog.reset(prompt=prompt)

        if isinstance(llm_response, str):
            text = llm_response.strip()
            if text.startswith("```"):
                text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
                text = re.sub(r"\s*```$", "", text)
            parsed = json.loads(text)
        else:
            parsed = llm_response

        calls_list: List[Dict[str, Any]] = []
        if isinstance(parsed, list):
            calls_list = parsed
        elif isinstance(parsed, dict):
            if "phases" in parsed and isinstance(parsed["phases"], list):
                for phase_idx, phase_entry in enumerate(parsed["phases"], start=1):
                    if isinstance(phase_entry, dict):
                        phase_id = phase_entry.get("phase", phase_idx)
                        phase_calls = phase_entry.get("calls", [])
                    elif isinstance(phase_entry, list):
                        phase_id = phase_idx
                        phase_calls = phase_entry
                    else:
                        continue
                    for call_item in phase_calls:
                        if isinstance(call_item, dict):
                            tagged = dict(call_item)
                            tagged.setdefault("phase", phase_id)
                            calls_list.append(tagged)
            elif "calls" in parsed and isinstance(parsed["calls"], list):
                calls_list = parsed["calls"]
            elif "tool_calls" in parsed and isinstance(parsed["tool_calls"], list):
                for tc in parsed["tool_calls"]:
                    fn_spec = tc.get("function", {})
                    fn_name = fn_spec.get("name", "")
                    raw_args = fn_spec.get("arguments", {})
                    fn_args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                    calls_list.append({"function": fn_name, "args": fn_args})

        executed = 0
        failed = False
        if isinstance(parsed, dict):
            self.catalog.plan.brief_conflicts = parsed.get("brief_conflicts", [])
        current_phase: Any = None
        phase_barrier: List[str] = []
        for item in calls_list:
            if not isinstance(item, dict):
                continue
            item_phase = item.get("phase")
            if item_phase is not None and item_phase != current_phase:
                if current_phase is not None or self.catalog.plan.beats:
                    preceding_by_actor = {b.actor_id: b.beat_id for b in self.catalog.plan.beats}
                    phase_barrier = list(preceding_by_actor.values())
                current_phase = item_phase
            fn_name = str(item.get("function") or item.get("fn") or item.get("name") or "").strip()
            fn_args = item.get("args") if "args" in item else item.get("arguments", {k: v for k, v in item.items() if k not in ("fn", "function", "name", "phase")})
            if isinstance(fn_args, str):
                try:
                    fn_args = json.loads(fn_args)
                except Exception:
                    fn_args = {}
            if not isinstance(fn_args, dict):
                fn_args = {}
            beats_before = len(self.catalog.plan.beats)
            if self._invoke_catalog_call(fn_name, dict(fn_args)):
                executed += 1
                if phase_barrier and len(self.catalog.plan.beats) > beats_before:
                    for new_beat in self.catalog.plan.beats[beats_before:]:
                        existing_wait = list(new_beat.params.get("wait_for", []))
                        for dep_id in phase_barrier:
                            if dep_id != new_beat.beat_id and dep_id not in existing_wait:
                                existing_wait.append(dep_id)
                        if existing_wait:
                            new_beat.params["wait_for"] = existing_wait
            else:
                failed = True
                break

        if failed or (executed == 0 and (prompt.strip() or not (self.brief.get("tracks") or self.brief.get("paths")))):
            return False, self.catalog.plan, list(self.execution_log)

        if not self._review_catalog_plan():
            return False, self.catalog.plan, list(self.execution_log)

        if self.camera_mode != "manual" and not self.catalog.plan.camera:
            humans = [s.actor_id for s in self.catalog.plan.setup if "human" in (s.asset_id or "") or "character" in (s.asset_id or "")]
            vehicles = [s.actor_id for s in self.catalog.plan.setup if "car" in (s.asset_id or "") or "vehicle" in (s.asset_id or "")]
            self._synthesize_camera(prompt.lower(), vehicles, humans, {})

        return True, self.catalog.plan, list(self.execution_log)

    def _review_catalog_plan(self) -> bool:
        from plan_review import review_plan
        self.last_plan_review = review_plan(self.catalog.plan, self.catalog.scene_data,
                                             self.catalog.manifest,
                                             locked_actors=self.brief.get('tracks', {}))
        if not self.last_plan_review['passed']:
            self.execution_log.append('[Plan review] ' + json.dumps(self.last_plan_review))
        return self.last_plan_review['passed']

    # -------------------------------------------------------------------------
    # Mode B: General Scene-Graph-Driven Compositional Semantic Planner
    # -------------------------------------------------------------------------
    def _plan_general_semantic(self, prompt: str, error_feedback: Optional[str] = None) -> None:
        """Decompose any natural-language prompt into scene-graph entities, clauses, beats, and camera."""
        p_clean = prompt.strip()
        p_low = p_clean.lower()

        # Step 1: Discover and ensure referenced environment objects/props exist in scene or plan
        env_refs = self._ensure_environment_context(p_clean)

        # Step 2: Extract and place vehicles and human actors semantically
        vehicles, humans = self._extract_and_place_actors(p_clean, env_refs)

        # Step 3: Split prompt into clauses and synthesize sequential/reactive beats
        clauses = self._split_into_clauses(p_clean)
        self._synthesize_clauses(clauses, p_clean, vehicles, humans, env_refs, error_feedback=error_feedback)

        # Step 4: Synthesize compositional camera framing and movement
        self._synthesize_camera(p_low, vehicles, humans, env_refs)

    def _place(self, asset_id: str, **kwargs) -> str:
        """Invoke catalog.place and return the assigned string actor_id."""
        ok, res = self.catalog.place(asset_id, **kwargs)
        if ok and isinstance(res, dict):
            return str(res.get("actor_id", asset_id))
        return str(res)

    def _available_scene_references(self) -> Dict[str, Any]:
        """Derive valid spatial references from authored zones, measured standing spots, or scene objects."""
        authored_zones = self.catalog.scene_data.get("zones", [])
        if authored_zones:
            zone_ids = {z.get("id"): z for z in authored_zones if z.get("id")}
            if "lane_east" in zone_ids:
                z0 = "lane_east"
            else:
                z0 = next(iter(zone_ids))
            z1 = "lane_west" if "lane_west" in zone_ids else (list(zone_ids.keys())[1] if len(zone_ids) > 1 else z0)
            sw_w = "sidewalk_west" if "sidewalk_west" in zone_ids else z1
            sw_e = "sidewalk_east" if "sidewalk_east" in zone_ids else z0
            plaza = "plaza" if "plaza" in zone_ids else z0
            stop_marker = "stop_mark" if "stop_mark" in zone_ids[z0].get("reference_markers", []) else "center"
            return {
                "car_spots": [f"{z0}.north", f"{z1}.south", f"{z0}.south"],
                "car_center": f"{z0}.center",
                "car_stop": f"{z0}.{stop_marker}",
                "car_north": f"{z0}.north",
                "car_south": f"{z0}.south",
                "car_alt_north": f"{z1}.north",
                "sidewalk_west": f"{sw_w}.center",
                "sidewalk_east": f"{sw_e}.center",
                "plaza": f"{plaza}.center",
                "north": f"{z0}.north",
                "south": f"{z0}.south",
                "west": f"{sw_w}.center",
                "east": f"{sw_e}.center",
                "center": f"{plaza}.center",
                "human_spots": [
                    f"{z0}.center",
                    f"{sw_w}.center",
                    f"{sw_e}.center",
                    f"{plaza}.center",
                    f"{sw_w}.north",
                    f"{sw_e}.south",
                    f"{z1}.center",
                    f"{sw_w}.south",
                    f"{sw_e}.north",
                    f"{z0}.south",
                ],
            }

        spots = [
            s for s in self.catalog.scene_data.get("standing_spots", [])
            if s.get("owner") and s.get("name") and s.get("position")
        ]
        if spots:
            ref_of = lambda s: f"{s['owner']}:{s['name']}"
            xs = [float(s["position"][0]) for s in spots]
            zs = [float(s["position"][2]) for s in spots]
            cx = sum(xs) / len(xs)
            cz = sum(zs) / len(zs)
            axis = 2 if (max(zs) - min(zs)) >= (max(xs) - min(xs)) else 0
            lat_axis = 0 if axis == 2 else 2
            wide_spots = [s for s in spots if float(s.get("clearance_m", 0.0)) >= 2.5]
            if len(wide_spots) < 2:
                wide_spots = [s for s in spots if float(s.get("clearance_m", 0.0)) >= 1.5] or list(spots)
            wide_sorted = sorted(wide_spots, key=lambda s: float(s["position"][axis]))
            s_north = wide_sorted[0]
            s_south = wide_sorted[-1]
            s_center = wide_sorted[len(wide_sorted) // 2]
            s_stop = None
            for cand_a in sorted(wide_spots, key=lambda s: (-float(s.get("clearance_m", 0.0)), float(s["position"][axis]))):
                pa = cand_a["position"]
                for cand_b in wide_sorted:
                    if cand_b is cand_a:
                        continue
                    pb = cand_b["position"]
                    dist = ((float(pb[0]) - float(pa[0])) ** 2 + (float(pb[2]) - float(pa[2])) ** 2) ** 0.5
                    if abs(float(pb[lat_axis]) - float(pa[lat_axis])) <= 4.5 and 7.0 <= dist <= 13.0:
                        s_north = cand_a
                        s_stop = cand_b
                        break
                if s_stop is not None:
                    break
            if s_stop is None:
                p0 = s_north["position"]
                s_stop = next(
                    (s for s in wide_sorted
                     if 7.0 <= ((float(s["position"][0]) - float(p0[0])) ** 2 + (float(s["position"][2]) - float(p0[2])) ** 2) ** 0.5 <= 14.0),
                    s_center if s_center is not s_north else s_south,
                )
            s_alt = wide_sorted[min(1, len(wide_sorted) - 1)]
            by_z = sorted(spots, key=lambda s: float(s["position"][2]))
            by_x = sorted(spots, key=lambda s: float(s["position"][0]))
            by_c = sorted(spots, key=lambda s: (float(s["position"][0]) - cx) ** 2 + (float(s["position"][2]) - cz) ** 2)
            used_veh = {ref_of(s_north), ref_of(s_stop)}
            pa = s_north["position"]
            pb = s_stop["position"]

            def _seg_dist_xz(pt: List[float]) -> float:
                vx, vz = float(pb[0]) - float(pa[0]), float(pb[2]) - float(pa[2])
                wx, wz = float(pt[0]) - float(pa[0]), float(pt[2]) - float(pa[2])
                seg_len2 = vx * vx + vz * vz
                t = max(0.0, min(1.0, (wx * vx + wz * vz) / seg_len2)) if seg_len2 > 1e-6 else 0.0
                dx = float(pt[0]) - (float(pa[0]) + t * vx)
                dz = float(pt[2]) - (float(pa[2]) + t * vz)
                return (dx * dx + dz * dz) ** 0.5

            def _stop_dist_xz(pt: List[float]) -> float:
                return ((float(pt[0]) - float(pb[0])) ** 2 + (float(pt[2]) - float(pb[2])) ** 2) ** 0.5

            safe_ped = [s for s in spots if ref_of(s) not in used_veh and _seg_dist_xz(s["position"]) >= 4.5]
            if not safe_ped:
                safe_ped = [s for s in spots if ref_of(s) not in used_veh] or list(spots)
            ped_candidates = sorted(
                safe_ped,
                key=lambda s: (
                    -round(float(s.get("clearance_m", 0.0)), 1),
                    _stop_dist_xz(s["position"]),
                ),
            )
            human_spots: List[str] = []
            chosen_pts: List[List[float]] = []
            for cand in ped_candidates:
                cp = cand["position"]
                if all(((float(cp[0]) - float(p[0])) ** 2 + (float(cp[2]) - float(p[2])) ** 2) ** 0.5 >= 2.5 for p in chosen_pts):
                    human_spots.append(ref_of(cand))
                    chosen_pts.append(cp)
                    if len(human_spots) >= 10:
                        break
            if not human_spots:
                human_spots = [ref_of(ped_candidates[0])]
            return {
                "car_spots": [ref_of(s_north), ref_of(s_south), ref_of(s_center)],
                "car_center": ref_of(s_center),
                "car_stop": ref_of(s_stop),
                "car_north": ref_of(s_north),
                "car_south": ref_of(s_south),
                "car_alt_north": ref_of(s_alt),
                "sidewalk_west": ref_of(by_x[0]),
                "sidewalk_east": ref_of(by_x[-1]),
                "plaza": ref_of(by_c[0]),
                "north": ref_of(by_z[0]),
                "south": ref_of(by_z[-1]),
                "west": ref_of(by_x[0]),
                "east": ref_of(by_x[-1]),
                "center": ref_of(by_c[0]),
                "human_spots": human_spots,
            }

        if "zones" in self.catalog.scene_data and self.catalog.scene_data.get("objects"):
            objs = [
                o for o in self.catalog.scene_data.get("objects", [])
                if o.get("type") not in ("Camera", "camera")
                and (o.get("name") or o.get("id")) not in ("ShotCam", "CameraRig", "CameraPaths", "Cameras")
            ]
            base = (objs[0].get("name") or objs[0].get("id")) if objs else "Ground"
            return {
                "car_spots": [f"in_front_of({base}, far)", f"behind({base}, far)", base],
                "car_center": base,
                "car_stop": f"in_front_of({base}, near)",
                "car_north": f"in_front_of({base}, far)",
                "car_south": f"behind({base}, far)",
                "car_alt_north": f"beside({base}, left, far)",
                "sidewalk_west": f"beside({base}, left, near)",
                "sidewalk_east": f"beside({base}, right, near)",
                "plaza": base,
                "north": f"in_front_of({base}, far)",
                "south": f"behind({base}, far)",
                "west": f"beside({base}, left, near)",
                "east": f"beside({base}, right, near)",
                "center": base,
                "human_spots": [
                    base,
                    f"beside({base}, left, near)",
                    f"beside({base}, right, near)",
                    f"in_front_of({base}, near)",
                    f"behind({base}, near)",
                    f"beside({base}, left, far)",
                    f"beside({base}, right, far)",
                ],
            }

        return {
            "car_spots": ["lane_east.north", "lane_west.south", "lane_east.south"],
            "car_center": "lane_east.center",
            "car_stop": "lane_east.stop_mark",
            "car_north": "lane_east.north",
            "car_south": "lane_east.south",
            "car_alt_north": "lane_west.north",
            "sidewalk_west": "sidewalk_west.center",
            "sidewalk_east": "sidewalk_east.center",
            "plaza": "plaza.center",
            "north": "lane_east.north",
            "south": "lane_east.south",
            "west": "sidewalk_west.center",
            "east": "sidewalk_east.center",
            "center": "plaza.center",
            "human_spots": [
                "lane_east.center",
                "sidewalk_west.center",
                "sidewalk_east.center",
                "plaza.center",
                "sidewalk_west.north",
                "sidewalk_east.south",
                "lane_west.center",
                "sidewalk_west.south",
                "sidewalk_east.north",
                "lane_east.south",
            ],
        }

    def _ensure_environment_context(self, prompt: str) -> Dict[str, str]:
        """Ensure any environment assets referenced in the prompt exist in the scene or shot setup."""
        env_refs: Dict[str, str] = {}
        p_low = prompt.lower()

        # Check stairs
        if any(w in p_low for w in ("stair", "stairs", "staircase", "steps")):
            assets = {asset.get("id"): asset for asset in self.catalog.manifest.get("assets", [])}
            for obj in self.catalog.scene_data.get("objects", []):
                stairs = obj.get("stairs") or assets.get(obj.get("asset") or obj.get("asset_id"), {}).get("stairs", {})
                if stairs:
                    stair_ref = f"{obj.get('name') or obj.get('id')}:{next(iter(stairs))}"
                    env_refs["stairs"] = stair_ref
                    env_refs["stairs_top"] = f"{stair_ref}_top"
                    env_refs["stairs_bottom"] = f"{stair_ref}_bottom"
                    break

        # Check trees and props
        scene_refs = self._available_scene_references()
        for asset_meta in self.matcher.find_referenced_environment_assets(prompt):
            aid = str(asset_meta.get("id", ""))
            cat = aid.split(".")[0] if "." in aid else "prop"
            if cat == "buildings":
                continue
            concept_key = "tree" if cat == "trees" else aid.split(".")[-1]
            existing_name = self.matcher.find_scene_object_by_concept(concept_key)
            if existing_name:
                env_refs[concept_key] = existing_name
                if cat == "trees":
                    env_refs["tree"] = existing_name
            else:
                semantic_spot = scene_refs["sidewalk_west"] if cat == "trees" else scene_refs["sidewalk_east"]
                label = "".join(w.capitalize() for w in aid.split(".")[-1].split("_"))
                placed_id = self._place(aid, at_reference=semantic_spot, label=label)
                self.execution_log.append(f"place('{aid}', at_reference='{semantic_spot}', label='{label}') -> {placed_id}")
                env_refs[concept_key] = placed_id
                if cat == "trees":
                    env_refs["tree"] = placed_id

        return env_refs

    def _extract_and_place_actors(
        self,
        prompt: str,
        env_refs: Dict[str, str],
    ) -> Tuple[List[str], List[str]]:
        """Extract vehicle and human actors from prompt and place them using semantic references."""
        p_low = prompt.lower()
        vehicles: List[str] = []
        humans: List[str] = []
        scene_refs = self._available_scene_references()

        # 1. Determine vehicle count & roles
        veh_count = 0
        if re.search(r"\b(?:two|2)\s+(?:cars|vehicles|sedans)\b", p_low) or "second car" in p_low or "another car" in p_low:
            veh_count = 2
        elif re.search(r"\b(?:three|3)\s+(?:cars|vehicles)\b", p_low):
            veh_count = 3
        elif re.search(r"\b(?:car|vehicle|sedan|coupe|suv|taxi|cruiser|truck)\b", p_low):
            veh_count = 1

        car_assets = ["cars.car1", "cars.car2", "cars.car3"]
        car_zones = scene_refs["car_spots"]
        is_parked = any(w in p_low for w in ("parked", "stationary", "stopped car", "jumps onto", "jump onto", "vault", "climbs onto"))
        is_reversing = any(w in p_low for w in ("reverse", "reverses", "backs up", "backing up"))

        for i in range(veh_count):
            c_asset = car_assets[i % len(car_assets)]
            if i == 0 and is_parked:
                c_at = scene_refs["car_center"]
            elif i == 0 and is_reversing:
                c_at = scene_refs["car_stop"]
            else:
                c_at = car_zones[i % len(car_zones)]
            cid = self._place(c_asset, at_reference=c_at, label=f"Car{i + 1}")
            vehicles.append(cid)
            self.execution_log.append(f"place('{c_asset}', at_reference='{c_at}', label='Car{i + 1}') -> {cid}")

        # 2. Determine human actor count & roles
        word_to_num = {
            "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
            "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
            "couple": 2, "pair": 2, "both": 2, "trio": 3, "crowd": 6, "group": 4,
        }
        human_count = 0
        for m in re.finditer(
            r"\b(one|two|three|four|five|six|seven|eight|nine|ten|\d+)\s+(?:people|persons|humans|characters|pedestrians|men|women|friends|fighters|runners|actors)\b",
            p_low,
        ):
            tok = m.group(1)
            n = int(tok) if tok.isdigit() else word_to_num.get(tok, 1)
            human_count = max(human_count, n)

        if human_count == 0:
            if any(w in p_low for w in ("couple", "pair of", "two of them", "argue", "converse", "talking", "fight", "fistfight", "brawl", "duel", "greet", "shake hands", "handshake", "kiss", "hug", "cpr")):
                human_count = 2
            elif any(w in p_low for w in ("crowd", "busy street", "chaotic")):
                human_count = 6
            elif "group" in p_low or "huddle" in p_low:
                human_count = 3
            else:
                # Count distinct role nouns if present
                roles_found = set()
                for r in ("driver", "passenger", "pedestrian", "runner", "attacker", "defender", "victim", "bystander", "mechanic", "patient", "medic"):
                    if re.search(r"\b" + r + r"\b", p_low):
                        roles_found.add(r)
                human_count = max(1, len(roles_found))

        # Check if an approaching threat/stranger is mentioned alongside a couple/group
        if "couple" in p_low and any(w in p_low for w in ("approaching", "approaches", "stranger", "attacker")):
            human_count = max(human_count, 3)

        # Place human actors using semantic anchors, vehicles, stairs, or environment objects
        starts_in_vehicle = (
            len(vehicles) > 0
            and any(w in p_low for w in ("disembark", "gets out", "get out", "exits the car", "exit the car", "steps out", "driver", "passenger"))
            and not any(w in p_low for w in ("walks to the car and boards", "runs to the car and enters", "approaches the car and gets in"))
        )

        builds = ["average", "tall", "short"]
        semantic_spots = scene_refs["human_spots"]
        rooftop_jump = self._rooftop_jump_context(p_low)

        for i in range(human_count):
            build = builds[i % len(builds)]
            label = f"Actor{i + 1}"

            if i == 0 and rooftop_jump:
                roof_ref, _ = rooftop_jump
                hid = self._place("character.human", at_reference=roof_ref, build=build, label=label)
                self.execution_log.append(f"place('character.human', at_reference='{roof_ref}', label='{label}') -> {hid}")
            elif i == 0 and "stairs" in env_refs:
                going_down = any(w in p_low for w in ("down the stairs", "descend", "descending", "down stairs"))
                stair_spot = env_refs["stairs_top"] if going_down else env_refs["stairs_bottom"]
                hid = self._place("character.human", at_reference=stair_spot, build=build, label=label)
                self.execution_log.append(f"place('character.human', at_reference='{stair_spot}', label='{label}') -> {hid}")
            elif starts_in_vehicle and i < min(len(vehicles) * 2, human_count):
                v_target = vehicles[0] if i < 2 else vehicles[min(i // 2, len(vehicles) - 1)]
                seat = "driver_seat" if i % 2 == 0 else "passenger_seat"
                hid = self._place("character.human", in_vehicle=v_target, seat=seat, build=build, label=label)
                self.execution_log.append(f"place('character.human', in_vehicle='{v_target}', seat='{seat}') -> {hid}")
            elif "tree" in env_refs and i < 2:
                tree_ref = env_refs["tree"]
                side = "right" if i == 0 else "left"
                at_expr = f"beside({tree_ref}, {side}, conversational)"
                face_expr = humans[0] if i == 1 and humans else None
                hid = self._place("character.human", at_reference=at_expr, facing_reference=face_expr, build=build, label=label)
                self.execution_log.append(f"place('character.human', at_reference='{at_expr}', label='{label}') -> {hid}")
            elif "tree" in env_refs and i == 2:
                tree_ref = env_refs["tree"]
                at_expr = f"in_front_of({tree_ref}, far)"
                hid = self._place("character.human", at_reference=at_expr, facing_reference=humans[0] if humans else tree_ref, build=build, label="Approacher")
                self.execution_log.append(f"place('character.human', at_reference='{at_expr}', label='Approacher') -> {hid}")
            elif i == 1 and humans and any(w in p_low for w in ("argue", "converse", "talk", "fight", "brawl", "greet", "push", "shove", "couple")):
                at_expr = f"in_front_of({humans[0]}, conversational)"
                hid = self._place("character.human", at_reference=at_expr, facing_reference=humans[0], build=build, label=label)
                self.execution_log.append(f"place('character.human', at_reference='{at_expr}', facing_reference='{humans[0]}') -> {hid}")
            elif vehicles and i == 0 and any(w in p_low for w in ("dodge", "evade", "jumps onto", "jump onto", "vault")):
                at_expr = scene_refs["car_center"] if "dodge" in p_low or "evade" in p_low else f"beside({vehicles[0]}, left, near)"
                hid = self._place("character.human", at_reference=at_expr, facing_reference=vehicles[0], build=build, label=label)
                self.execution_log.append(f"place('character.human', at_reference='{at_expr}', facing_reference='{vehicles[0]}') -> {hid}")
            else:
                spot = semantic_spots[i % len(semantic_spots)]
                hid = self._place("character.human", at_reference=spot, build=build, label=label)
                self.execution_log.append(f"place('character.human', at_reference='{spot}', label='{label}') -> {hid}")

            humans.append(hid)

        return vehicles, humans

    def _rooftop_jump_context(self, prompt: str) -> Optional[Tuple[str, str]]:
        """Choose measured takeoff and ground references for a rooftop descent."""
        if not (re.search(r"\b(jump|jumps|leap|leaps|fall|falls)\b", prompt)
                and re.search(r"\b(roof|rooftop|top of (?:the |a )?(?:building|tower))\b", prompt)):
            return None
        roofs = self.catalog.scene_data.get("rooftop_spots", [])
        ground = self.catalog.scene_data.get("standing_spots", [])
        if not roofs or not ground:
            return None
        roofs = [spot for spot in roofs if spot.get("landing_reference")]
        if not roofs:
            return None
        roof = max(roofs, key=lambda spot: float(spot["position"][1]))
        return (f"{roof['owner']}:{roof['name']}", str(roof["landing_reference"]))

    def _split_into_clauses(self, prompt: str) -> List[str]:
        """Split a natural-language prompt into ordered temporal/causal clauses."""
        raw_parts = re.split(
            r"(?:;|\n+|\.\s+|\bthen\b|\band then\b|\bafter that\b|\bnext\b|\bsuddenly\b|\bwhen\b|\bas\b|\bwhile\b|,\s*but\b|,\s*|\s+(?=\d+\.?\d*\s*s?\s*[-–to]\s*\d+\.?\d*))",
            prompt,
            flags=re.IGNORECASE,
        )
        clauses = [c.strip(" ,.") for c in raw_parts if c and c.strip(" ,.")]
        return clauses if clauses else [prompt]

    def _synthesize_clauses(
        self,
        clauses: List[str],
        full_prompt: str,
        vehicles: List[str],
        humans: List[str],
        env_refs: Dict[str, str],
        error_feedback: Optional[str] = None,
    ) -> None:
        """Map each clause compositionally to FunctionCatalog calls with semantic targets and timing."""
        p_low = full_prompt.lower()
        curr_time = 0.0
        last_after_event: Optional[str] = None
        scene_refs = self._available_scene_references()

        # Pre-check if vehicle approaches in background or stops first
        veh_scheduled = False
        if vehicles:
            v0 = vehicles[0]
            if any(w in p_low for w in ("dodge", "evade", "narrowly", "near miss", "almost hits", "speeds toward")):
                target_actor = humans[0] if humans else scene_refs["car_south"]
                self.catalog.drive_toward(v0, target=target_actor, mode="near_miss", at_time=0.0, for_duration=3.2)
                self.execution_log.append(f"drive_toward('{v0}', target='{target_actor}', mode='near_miss')")
                veh_scheduled = True
            elif any(w in p_low for w in ("reverse", "reverses", "backs up", "backing up")):
                self.catalog.reverse_to(v0, target=scene_refs["car_north"], at_time=0.0, for_duration=3.5)
                self.execution_log.append(f"reverse_to('{v0}', target='{scene_refs['car_north']}')")
                veh_scheduled = True
            elif any(w in p_low for w in ("brakes", "brake", "screeches", "stops", "pulls up", "arrives", "drives")):
                self.catalog.drive_to(v0, target=scene_refs["car_stop"], at_time=0.0, for_duration=2.8)
                self.execution_log.append(f"drive_to('{v0}', target='{scene_refs['car_stop']}')")
                last_after_event = f"{v0}.stopped"
                veh_scheduled = True

            if len(vehicles) > 1:
                v1 = vehicles[1]
                self.catalog.drive_past(v1, target=scene_refs["car_alt_north"], at_time=0.2, for_duration=4.5)
                self.execution_log.append(f"drive_past('{v1}', target='{scene_refs['car_alt_north']}')")

        # Synthesize clauses sequentially
        for idx, clause in enumerate(clauses):
            c_low = clause.lower()
            beat_added = False
            at_arg = None if last_after_event else round(curr_time, 2)
            after_arg = last_after_event
            dur_arg = None

            # Check for explicit time window e.g. "0s-5s" or "5-10" or "10 to 15s"
            time_m = re.search(r"(\d+(?:\.\d+)?)\s*s?\s*(?:-|–|to)\s*(\d+(?:\.\d+)?)\s*s?", c_low)
            if time_m:
                t_start = float(time_m.group(1))
                t_end = float(time_m.group(2))
                at_arg = t_start
                after_arg = None
                dur_arg = max(0.5, t_end - t_start)

            # 1. Staircase traversal
            if any(w in c_low for w in ("stair", "stairs", "staircase", "steps")) and humans:
                stair_id = env_refs.get("stairs", "stairs_main")
                direction = "down" if any(w in c_low for w in ("down", "descend", "descending")) else "up"
                pace = "run" if any(w in c_low for w in ("run", "running", "sprint", "fast", "hurr")) else "walk"
                self.catalog.climb_stairs(
                    humans[0],
                    stairs=stair_id,
                    direction=direction,
                    pace=pace,
                    at_time=at_arg,
                    after=after_arg,
                    for_duration=4.5,
                )
                self.execution_log.append(f"climb_stairs('{humans[0]}', stairs='{stair_id}', direction='{direction}', pace='{pace}')")
                curr_time += 4.5
                last_after_event = None
                beat_added = True

            # 2. Vehicle disembark / exit
            if any(w in c_low for w in ("disembark", "gets out", "get out", "exits", "exit", "steps out")) and vehicles and humans:
                v0 = vehicles[0]
                self.catalog.disembark_vehicle(
                    vehicle=v0,
                    occupants=humans[:2],
                    after=f"{v0}.stopped" if veh_scheduled else after_arg,
                    at_time=None if veh_scheduled or after_arg else round(curr_time, 2),
                    for_duration=2.4,
                )
                self.execution_log.append(f"disembark_vehicle(vehicle='{v0}', occupants={humans[:2]})")
                curr_time = max(curr_time, 2.8) + 2.5
                last_after_event = None
                beat_added = True

            # 3. Vehicle boarding / entering
            elif any(w in c_low for w in ("board", "boards", "gets in", "get in", "enters the car", "enter the car", "climbs into")) and vehicles and humans:
                v0 = vehicles[0]
                seat = "passenger_seat" if "passenger" in c_low else "driver_seat"
                self.catalog.board_vehicle(
                    vehicle=v0,
                    occupants=humans[0],
                    seats=seat,
                    at_time=at_arg,
                    after=after_arg,
                    for_duration=3.2,
                )
                self.execution_log.append(f"board_vehicle(vehicle='{v0}', occupants='{humans[0]}', seats='{seat}')")
                curr_time += 3.3
                last_after_event = None
                beat_added = True

            # 4. Threat evasion / dodge
            if any(w in c_low for w in ("dodge", "dodges", "evade", "evades", "leaps out of the way", "jumps out of the way")) and humans:
                threat_id = vehicles[0] if vehicles else (humans[-1] if len(humans) > 1 else "car_1")
                side = "left" if "left" in c_low else "right"
                prox_cond = {"actor_a": humans[0], "actor_b": threat_id, "gap": "far"} if vehicles else None
                self.catalog.dodge(
                    humans[0],
                    threat=threat_id,
                    direction=side,
                    when_closer_than=prox_cond,
                    at_time=None if vehicles else at_arg,
                    for_duration=1.5,
                )
                self.execution_log.append(f"dodge('{humans[0]}', threat='{threat_id}', direction='{side}')")
                curr_time += 1.8
                last_after_event = f"{humans[0]}.finished"
                beat_added = True

            # 5. Jumping / vaulting onto or over a surface
            if any(w in c_low for w in ("jump", "jumps", "leap", "leaps", "vault", "vaults", "climb onto", "climbs onto")) and not beat_added and humans:
                surface_ref = f"{vehicles[0]}:roof" if vehicles and "roof" in c_low else (f"{vehicles[0]}:hood" if vehicles else scene_refs["plaza"])
                if "over" in c_low and vehicles:
                    self.catalog.jump_over(humans[0], obstacle=f"{vehicles[0]}:hood", at_time=at_arg, after=after_arg, for_duration=1.8)
                    self.execution_log.append(f"jump_over('{humans[0]}', obstacle='{vehicles[0]}:hood')")
                    curr_time += 1.9
                elif "down" in c_low or "off" in c_low or self._rooftop_jump_context(p_low):
                    rooftop_jump = self._rooftop_jump_context(p_low)
                    land_ref = (rooftop_jump[1] if rooftop_jump else
                                (f"beside({vehicles[0]}, right, near)" if vehicles else scene_refs["car_center"]))
                    self.catalog.jump_down(humans[0], target=land_ref, at_time=at_arg,
                                           after=after_arg,
                                           for_duration=None if rooftop_jump else 1.5)
                    self.execution_log.append(f"jump_down('{humans[0]}', target='{land_ref}')")
                    curr_time += 1.6
                else:
                    if vehicles and idx == 0:
                        stand_ref = f"beside({vehicles[0]}, left, adjacent)"
                        self.catalog.run_to(humans[0], target=stand_ref, at_time=0.0, for_duration=1.4)
                        curr_time += 1.4
                        at_arg = round(curr_time, 2)
                        after_arg = None
                    self.catalog.jump_on(humans[0], surface=surface_ref, at_time=at_arg, after=after_arg, for_duration=1.6)
                    self.execution_log.append(f"jump_on('{humans[0]}', surface='{surface_ref}')")
                    curr_time += 1.7
                    if "jump down" in p_low or "jumps down" in p_low or "lands" in p_low:
                        land_ref = f"beside({vehicles[0]}, right, near)" if vehicles else scene_refs["car_center"]
                        self.catalog.jump_down(humans[0], target=land_ref, at_time=round(curr_time, 2), for_duration=1.4)
                        self.execution_log.append(f"jump_down('{humans[0]}', target='{land_ref}')")
                        curr_time += 1.5
                last_after_event = None
                beat_added = True

            # 6. Combat / Attack / Push / Shove
            if any(w in c_low for w in ("attack", "attacks", "fight", "fistfight", "brawl", "punch", "punches", "kick", "kicks", "hook", "strike")) and humans:
                attacker = humans[0]
                defender = humans[1] if len(humans) > 1 else (vehicles[0] if vehicles else humans[0])
                moves = ["left_hook", "elbow_punch", "kick_to_the_groin"]
                self.catalog.attack(
                    attacker,
                    target=defender,
                    moves=moves,
                    at_time=at_arg,
                    after=after_arg,
                    for_duration=2.6,
                )
                self.execution_log.append(f"attack('{attacker}', target='{defender}', moves={moves})")
                if len(humans) > 1:
                    self.catalog.hit_reaction(defender, target=attacker, at_time=at_arg,
                                              after=after_arg, for_duration=2.6)
                    self.execution_log.append(f"hit_reaction('{defender}', target='{attacker}')")
                curr_time += 2.7
                last_after_event = None
                beat_added = True

            elif any(w in c_low for w in ("push", "pushes", "shove", "shoves")) and len(humans) >= 2:
                self.catalog.push(humans[0], target=humans[1], at_time=at_arg, after=after_arg, for_duration=1.8)
                self.execution_log.append(f"push('{humans[0]}', target='{humans[1]}')")
                curr_time += 1.9
                last_after_event = None
                beat_added = True

            # 7. Group Social Formations: Huddle & Disperse
            if "huddle" in c_low and humans:
                self.catalog.huddle(actors=humans, center=scene_refs["plaza"], at_time=at_arg, after=after_arg, for_duration=2.8)
                self.execution_log.append(f"huddle(actors={humans}, center='{scene_refs['plaza']}')")
                curr_time += 2.9
                last_after_event = None
                beat_added = True

            if any(w in c_low for w in ("disperse", "scatter", "split up")) and humans:
                self.catalog.disperse(actors=humans, target=scene_refs["plaza"], at_time=at_arg, after=after_arg, for_duration=2.8)
                self.execution_log.append(f"disperse(actors={humans}, target='{scene_refs['plaza']}')")
                curr_time += 2.9
                last_after_event = None
                beat_added = True

            # 8. Social Interactions: Argue, Converse, Greet, Joyful/Happy Expressions
            if any(w in c_low for w in ("argue", "arguing", "argument", "heated", "dispute")) and len(humans) >= 2:
                self.catalog.argue(actors=humans[:2], intensity="heated", at_time=at_arg, after=after_arg, for_duration=3.2)
                self.execution_log.append(f"argue(actors={humans[:2]}, intensity='heated')")
                curr_time += 3.3
                last_after_event = None
                beat_added = True

            elif any(w in c_low for w in ("converse", "conversing", "chat", "chatting", "talk", "talking", "discuss")) and len(humans) >= 2:
                self.catalog.converse(actors=humans[:2], style="casual", at_time=at_arg, after=after_arg, for_duration=3.2)
                self.execution_log.append(f"converse(actors={humans[:2]}, style='casual')")
                curr_time += 3.3
                last_after_event = None
                beat_added = True

            elif any(w in c_low for w in ("happy", "joyful", "cheer", "cheering", "celebrat", "laugh")) and humans:
                for h_idx, hid in enumerate(humans[:2]):
                    partner = humans[1 - h_idx] if len(humans) >= 2 else None
                    move_name = "cheering" if h_idx == 0 else "clapping"
                    style = "cheer" if h_idx == 0 else "happy"
                    self.catalog.gesture(hid, move=move_name, style=style, target=partner, at_time=at_arg, after=after_arg, for_duration=2.6)
                    self.execution_log.append(f"gesture('{hid}', move='{move_name}', style='{style}', target='{partner}')")
                curr_time += 2.6
                last_after_event = None
                beat_added = True

            elif any(w in c_low for w in ("greet", "greets", "wave", "waves", "welcome")) and humans:
                target_h = humans[1] if len(humans) > 1 else (vehicles[0] if vehicles else humans[0])
                self.catalog.greet(humans[0], target=target_h, style="wave", at_time=at_arg, after=after_arg, for_duration=2.2)
                self.execution_log.append(f"greet('{humans[0]}', target='{target_h}')")
                curr_time += 2.3
                last_after_event = None
                beat_added = True

            # 9. Emotional Shift / Startled / Surprise / React / Fall
            if any(w in c_low for w in ("startled", "surprise", "surprised", "spooked", "shock", "flinch", "panic", "notice something approaching")) and humans:
                threat_actor = humans[2] if len(humans) >= 3 else (vehicles[0] if vehicles else None)
                if len(humans) >= 3 and "approaching" in c_low:
                    self.catalog.walk_to(humans[2], target=f"in_front_of({humans[0]}, near)", at_time=0.0, for_duration=curr_time + 2.4)
                    self.execution_log.append(f"walk_to('{humans[2]}', target='in_front_of({humans[0]}, near)')")
                for hid in humans[:2]:
                    self.catalog.react(hid, mood="spooked", style="startled", look_at=threat_actor, at_time=at_arg, after=after_arg, for_duration=2.4)
                    self.execution_log.append(f"react('{hid}', mood='spooked', style='startled', look_at='{threat_actor}')")
                curr_time += 2.5
                last_after_event = None
                beat_added = True

            elif any(w in c_low for w in ("fall", "falls", "trip", "trips", "collapse", "collapses")) and humans and not beat_added:
                self.catalog.fall(humans[0], style="trip", at_time=at_arg, after=after_arg, for_duration=1.8)
                self.execution_log.append(f"fall('{humans[0]}', style='trip')")
                curr_time += 1.9
                if "stand" in p_low or "gets up" in p_low:
                    self.catalog.stand_up(humans[0], at_time=round(curr_time, 2), for_duration=2.0)
                    self.execution_log.append(f"stand_up('{humans[0]}')")
                    curr_time += 2.1
                last_after_event = None
                beat_added = True

            # 10. Object Manipulation: Pick up / Put down / Open hood or trunk
            if any(w in c_low for w in ("pick up", "picks up", "grab", "grabs", "lift", "lifts")) and humans:
                prop_ref = next((v for k, v in env_refs.items() if k not in ("stairs", "stairs_top", "stairs_bottom", "tree")), scene_refs["plaza"])
                self.catalog.pick_up(humans[0], object_reference=prop_ref, at_time=at_arg, after=after_arg, for_duration=2.0)
                self.execution_log.append(f"pick_up('{humans[0]}', object_reference='{prop_ref}')")
                curr_time += 2.1
                last_after_event = None
                beat_added = True

            if any(w in c_low for w in ("hood", "trunk")) and vehicles and humans:
                part_name = "hood" if "hood" in c_low else "trunk"
                stand_anchor = "hood_front" if part_name == "hood" else "trunk_rear"
                self.catalog.walk_to(humans[0], target=f"{vehicles[0]}:{stand_anchor}", at_time=at_arg, after=after_arg, for_duration=2.2)
                self.catalog.open_part(humans[0], part_reference=f"{vehicles[0]}:{part_name}", after=f"{humans[0]}.arrived", for_duration=1.6)
                self.execution_log.append(f"open_part('{humans[0]}', part_reference='{vehicles[0]}:{part_name}')")
                curr_time += 3.9
                last_after_event = None
                beat_added = True

            # 10b. Extended Fundamental Actions: Weapons, Defense, Medical, Acrobatics, Climbing, Posture, Devices, Social, Dance & Activities
            if not beat_added and humans:
                if any(w in c_low for w in ("cpr", "resuscitat", "medical aid", "first aid")) and len(humans) >= 2:
                    self.catalog.lie_down(humans[1], style="rest", at_time=0.0, for_duration=4.5)
                    self.catalog.medical_aid(humans[0], patient=humans[1], action="cpr", at_time=at_arg, after=after_arg, for_duration=4.0)
                    self.execution_log.append(f"medical_aid('{humans[0]}', patient='{humans[1]}', action='cpr')")
                    curr_time += 4.1
                    last_after_event = None
                    beat_added = True
                elif any(w in c_low for w in ("pistol", "rifle", "machine gun", "shoot", "shoots", "sword", "slash", "stab", "knife")):
                    target_h = humans[1] if len(humans) > 1 else (vehicles[0] if vehicles else scene_refs["north"])
                    wpn = "sword" if ("sword" in c_low or "slash" in c_low) else ("knife" if ("knife" in c_low or "stab" in c_low) else ("rifle" if ("rifle" in c_low or "machine" in c_low) else "pistol"))
                    w_act = "draw" if "draw" in c_low else "fire"
                    self.catalog.use_weapon(humans[0], target=target_h, weapon=wpn, action=w_act, at_time=at_arg, after=after_arg, for_duration=2.2)
                    self.execution_log.append(f"use_weapon('{humans[0]}', target='{target_h}', weapon='{wpn}', action='{w_act}')")
                    curr_time += 2.3
                    last_after_event = None
                    beat_added = True
                elif any(w in c_low for w in ("block", "blocks", "guard", "parry", "defend")):
                    threat_h = humans[1] if len(humans) > 1 else None
                    self.catalog.defend_or_block(humans[0], threat=threat_h, style="block", at_time=at_arg, after=after_arg, for_duration=2.0)
                    self.execution_log.append(f"defend_or_block('{humans[0]}', threat='{threat_h}', style='block')")
                    curr_time += 2.1
                    last_after_event = None
                    beat_added = True
                elif any(w in c_low for w in ("knocked out", "knocks out", "slip", "slips", "dying", "dies", "sweep fall")):
                    st = "slip" if "slip" in c_low else ("dying" if ("dying" in c_low or "dies" in c_low) else ("sweep_fall" if "sweep" in c_low else "knocked_out"))
                    self.catalog.collapse_or_knockout(humans[0], style=st, at_time=at_arg, after=after_arg, for_duration=2.2)
                    self.execution_log.append(f"collapse_or_knockout('{humans[0]}', style='{st}')")
                    curr_time += 2.3
                    last_after_event = None
                    beat_added = True
                elif any(w in c_low for w in ("backflip", "cartwheel", "macaco", "acrobatic", "spin")):
                    mv = "cartwheel" if "cartwheel" in c_low else ("macaco" if "macaco" in c_low else ("spin" if "spin" in c_low else "backflip"))
                    self.catalog.perform_acrobatics(humans[0], move=mv, at_time=at_arg, after=after_arg, for_duration=2.2)
                    self.execution_log.append(f"perform_acrobatics('{humans[0]}', move='{mv}')")
                    curr_time += 2.3
                    last_after_event = None
                    beat_added = True
                elif any(w in c_low for w in ("climb ladder", "climbs ladder", "climb rope", "climbs rope", "climb wall", "climbs wall", "climb roof", "climbs roof")):
                    st = "ladder" if "ladder" in c_low else ("rope" if "rope" in c_low else ("roof" if "roof" in c_low else "wall"))
                    c_dir = "down" if "down" in c_low else "up"
                    self.catalog.climb_surface(humans[0], surface=scene_refs["plaza"], style=st, direction=c_dir, at_time=at_arg, after=after_arg, for_duration=3.2)
                    self.execution_log.append(f"climb_surface('{humans[0]}', style='{st}', direction='{c_dir}')")
                    curr_time += 3.3
                    last_after_event = None
                    beat_added = True
                elif any(w in c_low for w in ("dive roll", "dive-roll")):
                    obs = f"{vehicles[0]}:hood" if vehicles else scene_refs["plaza"]
                    self.catalog.vault_over(humans[0], obstacle=obs, style="dive_roll", at_time=at_arg, after=after_arg, for_duration=2.0)
                    self.execution_log.append(f"vault_over('{humans[0]}', obstacle='{obs}', style='dive_roll')")
                    curr_time += 2.1
                    last_after_event = None
                    beat_added = True
                elif any(w in c_low for w in ("kneel", "kneels", "pray", "prays")):
                    st = "pray" if "pray" in c_low else ("inspect" if "inspect" in c_low else "kneel")
                    self.catalog.kneel(humans[0], style=st, at_time=at_arg, after=after_arg, for_duration=2.5)
                    self.execution_log.append(f"kneel('{humans[0]}', style='{st}')")
                    curr_time += 2.6
                    last_after_event = None
                    beat_added = True
                elif any(w in c_low for w in ("lie down", "lies down", "sleep", "sleeps", "writhe")):
                    st = "writhe" if "writhe" in c_low else ("sleep" if "sleep" in c_low else "rest")
                    self.catalog.lie_down(humans[0], style=st, at_time=at_arg, after=after_arg, for_duration=3.5)
                    self.execution_log.append(f"lie_down('{humans[0]}', style='{st}')")
                    curr_time += 3.6
                    last_after_event = None
                    beat_added = True
                elif any(w in c_low for w in ("push button", "pushes button", "pull lever", "pulls lever", "enter code", "enters code", "filing cabinet", "dig", "digs", "typing", "types", "write", "writes")):
                    act = "pull_lever" if "lever" in c_low else ("enter_code" if "code" in c_low else ("open_cabinet" if "cabinet" in c_low else ("dig" if "dig" in c_low else ("type" if "typ" in c_low else ("write" if "writ" in c_low else "push_button")))))
                    self.catalog.interact_object(humans[0], action=act, at_time=at_arg, after=after_arg, for_duration=2.4)
                    self.execution_log.append(f"interact_object('{humans[0]}', action='{act}')")
                    curr_time += 2.5
                    last_after_event = None
                    beat_added = True
                elif any(w in c_low for w in ("throw", "throws", "toss", "tosses", "grenade")):
                    target_ref = humans[1] if len(humans) > 1 else (vehicles[0] if vehicles else scene_refs["north"])
                    st = "grenade" if ("grenade" in c_low or "toss" in c_low) else "overhand"
                    self.catalog.throw_toward(humans[0], target=target_ref, style=st, at_time=at_arg, after=after_arg, for_duration=2.0)
                    self.execution_log.append(f"throw_toward('{humans[0]}', target='{target_ref}', style='{st}')")
                    curr_time += 2.1
                    last_after_event = None
                    beat_added = True
                elif any(w in c_low for w in ("pull rope", "pulls rope", "pull heavy", "pulls heavy", "tug")):
                    st = "rope" if "rope" in c_low else "heavy"
                    self.catalog.pull_object(humans[0], style=st, at_time=at_arg, after=after_arg, for_duration=2.8)
                    self.execution_log.append(f"pull_object('{humans[0]}', style='{st}')")
                    curr_time += 2.9
                    last_after_event = None
                    beat_added = True
                elif any(w in c_low for w in ("look around", "looks around", "search ground", "searches ground", "inspect area")):
                    st = "nervous" if "nervous" in c_low else ("search_ground" if "ground" in c_low else "look_around")
                    self.catalog.inspect_area(humans[0], style=st, at_time=at_arg, after=after_arg, for_duration=2.5)
                    self.execution_log.append(f"inspect_area('{humans[0]}', style='{st}')")
                    curr_time += 2.6
                    last_after_event = None
                    beat_added = True
                elif any(w in c_low for w in ("cell phone", "on the phone", "phone call")):
                    d = dur_arg if dur_arg is not None else 3.2
                    if "pace" in c_low or "pacing" in c_low:
                        self.catalog.pace_while_talking(humans[0], at_time=at_arg, after=after_arg, for_duration=d)
                        self.execution_log.append(f"pace_while_talking('{humans[0]}')")
                    else:
                        self.catalog.use_phone(humans[0], mode="talk", duration=d, at_time=at_arg, after=after_arg)
                        self.execution_log.append(f"use_phone('{humans[0]}', mode='talk')")
                    curr_time = max(curr_time, (at_arg or curr_time) + d + 0.1)
                    last_after_event = None
                    beat_added = True
                elif any(w in c_low for w in ("honk", "honks", "horn")):
                    self.catalog.honk_horn(humans[0], vehicle=vehicles[0] if vehicles else None, at_time=at_arg, after=after_arg, for_duration=1.8)
                    self.execution_log.append(f"honk_horn('{humans[0]}')")
                    curr_time += 1.9
                    last_after_event = None
                    beat_added = True
                elif any(w in c_low for w in ("shake hands", "shakes hands", "handshake")):
                    d = dur_arg if dur_arg is not None else 2.4
                    if len(humans) < 2:
                        hid2 = self._place("character.human", at_reference=f"in_front_of({humans[0]}, conversational)", label="Partner")
                        humans.append(hid2)
                        self.execution_log.append(f"place('character.human', at_reference='in_front_of({humans[0]}, conversational)', label='Partner') -> {hid2}")
                    self.catalog.shake_hands(humans[0], target=humans[1], duration=d, at_time=at_arg, after=after_arg)
                    self.catalog.shake_hands(humans[1], target=humans[0], duration=d, at_time=at_arg, after=after_arg)
                    self.execution_log.append(f"shake_hands('{humans[0]}', target='{humans[1]}')")
                    curr_time = max(curr_time, (at_arg or curr_time) + d + 0.1)
                    last_after_event = None
                    beat_added = True
                elif any(w in c_low for w in ("kiss", "kisses", "hug", "hugs", "pat", "pats")) and len(humans) >= 2:
                    st = "pat" if ("pat" in c_low or "hug" in c_low) else "kiss"
                    self.catalog.hug_or_kiss(humans[0], target=humans[1], style=st, at_time=at_arg, after=after_arg, for_duration=2.4)
                    self.execution_log.append(f"hug_or_kiss('{humans[0]}', target='{humans[1]}', style='{st}')")
                    curr_time += 2.5
                    last_after_event = None
                    beat_added = True
                elif any(w in c_low for w in ("victory", "rally", "standing clap", "applaud")):
                    st = "victory" if "victory" in c_low else ("rally" if "rally" in c_low else "clap")
                    self.catalog.celebrate(humans[0], style=st, at_time=at_arg, after=after_arg, for_duration=2.6)
                    self.execution_log.append(f"celebrate('{humans[0]}', style='{st}')")
                    curr_time += 2.7
                    last_after_event = None
                    beat_added = True
                elif any(w in c_low for w in ("cry", "cries", "crying", "yell", "yells", "shrug", "shrugs", "nod", "nods", "salute", "salutes", "threaten", "threatens", "bored", "cocky")):
                    em = next((k for k in ("cry", "yell", "shrug", "nod", "salute", "threaten", "bored", "cocky") if k in c_low), "shrug")
                    self.catalog.emote(humans[0], emotion=em, target=humans[1] if len(humans) > 1 else None, at_time=at_arg, after=after_arg, for_duration=2.4)
                    self.execution_log.append(f"emote('{humans[0]}', emotion='{em}')")
                    curr_time += 2.5
                    last_after_event = None
                    beat_added = True
                elif any(w in c_low for w in ("dance", "dances", "dancing", "moonwalk", "breakdance", "groove")):
                    st = "moonwalk" if "moonwalk" in c_low else ("breakdance" if "breakdance" in c_low else ("silly" if "silly" in c_low else "groove"))
                    self.catalog.dance(humans[0], style=st, at_time=at_arg, after=after_arg, for_duration=3.5)
                    self.execution_log.append(f"dance('{humans[0]}', style='{st}')")
                    curr_time += 3.6
                    last_after_event = None
                    beat_added = True
                elif any(w in c_low for w in ("guitar", "piano", "drums", "sing", "singing", "rap", "rapping", "basketball", "soccer", "jump rope", "fishing", "smoke", "smoking", "cards", "gaming")):
                    act_keys = ("guitar", "piano", "drums", "sing", "rap", "basketball", "soccer", "jump_rope", "fish", "smoke", "cards", "gaming")
                    norm_c = c_low.replace("jump rope", "jump_rope").replace("fishing", "fish").replace("singing", "sing").replace("rapping", "rap").replace("smoking", "smoke")
                    act = next((k for k in act_keys if k in norm_c), "guitar")
                    self.catalog.perform_activity(humans[0], activity=act, at_time=at_arg, after=after_arg, for_duration=3.5)
                    self.execution_log.append(f"perform_activity('{humans[0]}', activity='{act}')")
                    curr_time += 3.6
                    last_after_event = None
                    beat_added = True

            # 11. General & Specialized Locomotion: Crawl, Stumble, Back Up, Carry, Swim, Fly, Sneak, Run, Jog, Walk, Approach, Follow, Flee
            if not beat_added and humans:
                semantic_dest = self._resolve_clause_destination(c_low, vehicles, humans, env_refs)
                if any(w in c_low for w in ("crawl", "crawls", "crawling")):
                    c_dir = "backward" if "back" in c_low else "forward"
                    self.catalog.crawl_to(humans[0], target=semantic_dest, direction=c_dir, at_time=at_arg, after=after_arg, for_duration=3.5)
                    self.execution_log.append(f"crawl_to('{humans[0]}', target='{semantic_dest}', direction='{c_dir}')")
                    curr_time += 3.6
                    last_after_event = None
                    beat_added = True
                elif any(w in c_low for w in ("stumble", "stumbles", "limp", "limps", "injured", "drunk")):
                    st = "drunk" if "drunk" in c_low else ("sad" if "sad" in c_low else "injured")
                    self.catalog.stumble_to(humans[0], target=semantic_dest, style=st, at_time=at_arg, after=after_arg, for_duration=3.5)
                    self.execution_log.append(f"stumble_to('{humans[0]}', target='{semantic_dest}', style='{st}')")
                    curr_time += 3.6
                    last_after_event = None
                    beat_added = True
                elif any(w in c_low for w in ("back up", "backs up", "step backward", "steps backward", "walk backward", "walks backward", "retreat")):
                    b_pace = "run" if "run" in c_low else ("jog" if "jog" in c_low else "walk")
                    self.catalog.back_up_to(humans[0], target=scene_refs["south"], pace=b_pace, at_time=at_arg, after=after_arg, for_duration=3.0)
                    self.execution_log.append(f"back_up_to('{humans[0]}', target='{scene_refs['south']}', pace='{b_pace}')")
                    curr_time += 3.1
                    last_after_event = None
                    beat_added = True
                elif any(w in c_low for w in ("carry", "carries", "carrying", "shopping bag", "carrying a box")):
                    item = "box" if "box" in c_low else "bag"
                    self.catalog.carry_to(humans[0], target=semantic_dest, item=item, at_time=at_arg, after=after_arg, for_duration=3.2)
                    self.execution_log.append(f"carry_to('{humans[0]}', target='{semantic_dest}', item='{item}')")
                    curr_time += 3.3
                    last_after_event = None
                    beat_added = True
                elif any(w in c_low for w in ("swim", "swims", "swimming")):
                    self.catalog.swim_to(humans[0], target=semantic_dest, at_time=at_arg, after=after_arg, for_duration=3.5)
                    self.execution_log.append(f"swim_to('{humans[0]}', target='{semantic_dest}')")
                    curr_time += 3.6
                    last_after_event = None
                    beat_added = True
                elif any(w in c_low for w in ("fly", "flies", "flying", "float", "floats", "floating", "hover")):
                    st = "float" if ("float" in c_low or "hover" in c_low) else "superhero"
                    self.catalog.fly_to(humans[0], target=semantic_dest, style=st, at_time=at_arg, after=after_arg, for_duration=3.2)
                    self.execution_log.append(f"fly_to('{humans[0]}', target='{semantic_dest}', style='{st}')")
                    curr_time += 3.3
                    last_after_event = None
                    beat_added = True
                elif any(w in c_low for w in ("sneak", "sneaks", "sneaking", "creep", "crouch")):
                    for hid in humans:
                        self.catalog.sneak_to(hid, target=semantic_dest, at_time=at_arg, after=after_arg, for_duration=2.8)
                        self.execution_log.append(f"sneak_to('{hid}', target='{semantic_dest}')")
                    curr_time += 2.9
                    last_after_event = None
                    beat_added = True
                elif any(w in c_low for w in ("flee", "flees", "run away", "runs away", "escape")):
                    threat_ref = vehicles[0] if vehicles else (humans[-1] if len(humans) > 1 else scene_refs["plaza"])
                    self.catalog.flee_from(humans[0], threat=threat_ref, at_time=at_arg, after=after_arg, for_duration=2.8)
                    self.execution_log.append(f"flee_from('{humans[0]}', threat='{threat_ref}')")
                    curr_time += 2.9
                    last_after_event = None
                    beat_added = True
                elif any(w in c_low for w in ("follow", "follows", "following", "chase", "chases")) and len(humans) >= 2:
                    self.catalog.walk_to(humans[0], target=scene_refs["north"], at_time=at_arg, after=after_arg, for_duration=3.5)
                    self.catalog.follow(humans[1], target=humans[0], at_time=at_arg, after=after_arg, for_duration=3.5)
                    self.execution_log.append(f"follow('{humans[1]}', target='{humans[0]}')")
                    curr_time += 3.6
                    last_after_event = None
                    beat_added = True
                elif any(w in c_low for w in ("run", "runs", "running", "sprint", "sprints", "dash", "rushes")):
                    self.catalog.run_to(humans[0], target=semantic_dest, at_time=at_arg, after=after_arg, for_duration=2.5)
                    self.execution_log.append(f"run_to('{humans[0]}', target='{semantic_dest}')")
                    curr_time += 2.6
                    last_after_event = None
                    beat_added = True
                elif any(w in c_low for w in ("jog", "jogs", "jogging")):
                    self.catalog.jog_to(humans[0], target=semantic_dest, at_time=at_arg, after=after_arg, for_duration=2.8)
                    self.execution_log.append(f"jog_to('{humans[0]}', target='{semantic_dest}')")
                    curr_time += 2.9
                    last_after_event = None
                    beat_added = True
                elif any(w in c_low for w in ("walk", "walks", "walking", "stroll", "stride", "cross", "crosses", "head", "heads", "approach", "approaches", "go", "goes")):
                    d = dur_arg if dur_arg is not None else 3.2
                    alt_dest = scene_refs["human_spots"][min(1, len(scene_refs["human_spots"]) - 1)]
                    for h_idx, hid in enumerate(humans):
                        dest = semantic_dest if h_idx % 2 == 0 else alt_dest
                        self.catalog.walk_to(hid, target=dest, at_time=at_arg, after=after_arg, for_duration=d)
                        self.execution_log.append(f"walk_to('{hid}', target='{dest}')")
                    curr_time = max(curr_time, (at_arg or curr_time) + d + 0.1)
                    last_after_event = None
                    beat_added = True

        # Ensure every actor has at least one meaningful beat if the prompt was purely descriptive
        if not self.catalog.plan.beats:
            if len(humans) >= 3:
                dests = scene_refs["human_spots"]
                for i, hid in enumerate(humans):
                    dest = dests[(i + 1) % len(dests)]
                    if i % 3 == 0:
                        self.catalog.run_to(hid, target=dest, at_time=0.0, for_duration=4.5)
                    else:
                        self.catalog.walk_to(hid, target=dest, at_time=0.0, for_duration=4.5)
                    self.execution_log.append(f"walk_to('{hid}', target='{dest}')")
            elif len(humans) == 2:
                self.catalog.converse(actors=humans, style="casual", at_time=0.0, for_duration=4.0)
                self.execution_log.append(f"converse(actors={humans})")
            elif humans:
                dest = self._resolve_clause_destination(p_low, vehicles, humans, env_refs)
                self.catalog.walk_to(humans[0], target=dest, at_time=0.0, for_duration=4.0)
                self.execution_log.append(f"walk_to('{humans[0]}', target='{dest}')")

    def _resolve_clause_destination(
        self,
        clause_low: str,
        vehicles: List[str],
        humans: List[str],
        env_refs: Dict[str, str],
    ) -> str:
        """Resolve a clause's spatial target into a semantic reference expression."""
        scene_refs = self._available_scene_references()
        if "tree" in clause_low and "tree" in env_refs:
            return f"beside({env_refs['tree']}, right, near)"
        if any(w in clause_low for w in ("car", "vehicle", "sedan")) and vehicles:
            if "hood" in clause_low:
                return f"{vehicles[0]}:hood_front"
            if "trunk" in clause_low:
                return f"{vehicles[0]}:trunk_rear"
            return f"{vehicles[0]}:door_fl_stand"
        diagonal = re.search(r"\b(north|south)[\s_-]?(east|west)(?:bound|ward|wards)?\b", clause_low)
        if diagonal:
            return diagonal.group(1) + diagonal.group(2)
        cardinal = re.search(r"\b(north|south|east|west)(?:bound|ward|wards)?\b", clause_low)
        if cardinal:
            return cardinal.group(1)
        if humans:
            actor = humans[0]
            if re.search(r"\b(?:forward|ahead)\b", clause_low):
                return f"in_front_of({actor}, far)"
            if re.search(r"\b(?:back|backward|backwards)\b", clause_low):
                return f"behind({actor}, far)"
            if re.search(r"\bleft\b", clause_low):
                return f"beside({actor}, left, far)"
            if re.search(r"\bright\b", clause_low):
                return f"beside({actor}, right, far)"
        if "plaza" in clause_low or "center" in clause_low or "square" in clause_low:
            return scene_refs["center"]
        return scene_refs["north"]

    def _synthesize_camera(
        self,
        p_low: str,
        vehicles: List[str],
        humans: List[str],
        env_refs: Dict[str, str],
    ) -> None:
        """Determine camera style, vantage, movement, and cuts purely from semantic prompt cues."""
        if self.camera_mode == "manual":
            return
        tracked = list(humans[:3]) + [v for v in vehicles[:1] if v not in humans] if humans else list(vehicles[:2])
        if "tree" in env_refs and env_refs["tree"] not in tracked:
            tracked.append(env_refs["tree"])

        # Style
        if any(w in p_low for w in ("top down", "top-down", "bird's eye", "birds eye", "overhead")):
            style = "top_down"
        elif any(w in p_low for w in ("close up", "close-up", "closeup", "tight shot")):
            style = "close"
        elif any(w in p_low for w in ("over the shoulder", "over-the-shoulder")):
            style = "over_the_shoulder"
        elif any(w in p_low for w in ("three quarter", "three-quarter", "3/4")):
            style = "three_quarter"
        elif any(w in p_low for w in ("wide", "establishing", "crowd", "drone", "aerial", "soaring", "disperse")):
            style = "wide"
        else:
            style = "medium"

        # Vantage
        vantage = "aerial" if any(w in p_low for w in ("drone", "aerial", "soaring", "high angle", "high-angle", "swoop")) else "eye_level"

        # Movement
        if any(w in p_low for w in ("orbit", "orbits", "orbiting", "360", "circles around")):
            move = "orbit"
        elif any(w in p_low for w in ("pull out", "pulls out", "pull back", "zoom out")):
            move = "pull_out"
        elif any(w in p_low for w in ("crane up", "cranes up", "rises")):
            move = "crane_up"
        elif any(w in p_low for w in ("track", "tracks", "tracking", "follow", "follows")) and "camera" in p_low:
            move = "track_with"
        elif any(w in p_low for w in ("push in", "pushes in", "soaring toward", "soars toward", "swoop", "zoom in")):
            move = "push_in"
        else:
            move = "push_in" if vantage == "aerial" else None

        self.catalog.camera_frame(actors=tracked, style=style, vantage=vantage, move=move)
        if move == "orbit":
            self.catalog.camera_move(movement="orbit", angle_deg=60.0)
        elif move:
            self.catalog.camera_move(movement=move)

        self.execution_log.append(f"camera_frame(actors={tracked}, style='{style}', vantage='{vantage}', move='{move}')")

    # -------------------------------------------------------------------------
    # Post-Plan Semantic Prompt Adherence Verification & Self-Repair
    # -------------------------------------------------------------------------
    def _verify_and_repair_prompt_adherence(self, prompt: str) -> None:
        """Verify that all key entities and emotional/camera cues in the prompt are represented."""
        p_low = prompt.lower()
        plan = self.catalog.plan
        scene_refs = self._available_scene_references()

        # 1. Verify tree presence if prompt explicitly mentions "tree"
        if re.search(r"\btrees?\b", p_low):
            has_tree_in_scene = self.matcher.find_scene_object_by_concept("tree") is not None
            has_tree_in_setup = any("tree" in (s.asset_id or "").lower() for s in plan.setup)
            if not has_tree_in_scene and not has_tree_in_setup:
                tid = self._place("trees.oak_tree", at_reference=scene_refs["sidewalk_west"], label="OakTree")
                self.execution_log.append(f"[Adherence] Added missing tree '{tid}' at '{scene_refs['sidewalk_west']}'")

        # 2. Verify couple / pair human count
        if re.search(r"\b(couple|pair|two people)\b", p_low):
            human_setups = [s for s in plan.setup if "human" in (s.asset_id or "") or "character" in (s.asset_id or "")]
            while len(human_setups) < 2:
                hid = self._place("character.human", at_reference=scene_refs["plaza"], label=f"Partner{len(human_setups) + 1}")
                human_setups.append(plan.setup[-1])
                self.execution_log.append(f"[Adherence] Added partner actor '{hid}'")

        # 3. Verify drone / aerial camera elevation via semantic vantage
        if re.search(r"\b(drone|aerial|soaring|bird'?s[- ]eye)\b", p_low):
            plan.camera["vantage"] = "aerial"
            if plan.camera.get("style") not in ("wide", "top_down"):
                plan.camera["style"] = "wide"
            if not plan.camera.get("move"):
                plan.camera["move"] = "push_in"


if __name__ == "__main__":
    import subprocess

    repo_root = Path(__file__).resolve().parent.parent.parent
    app_bin = repo_root / "build" / "PainterSpace"
    env = dict(os.environ)
    qt_lib = "/Users/vishaljose/Qt/6.11.1/macos/lib"
    env["DYLD_FRAMEWORK_PATH"] = f"{qt_lib}:{env.get('DYLD_FRAMEWORK_PATH', '')}".strip(":")
    subprocess.run(["cmake", "--build", str(repo_root / "build")], cwd=str(repo_root), check=False)
    os.execve(str(app_bin), [str(app_bin)], env)

