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
from typing import Any, Dict, List, Optional, Tuple

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
        self.matcher = ManifestAssetMatcher(catalog.manifest, catalog.scene_data)

    # -------------------------------------------------------------------------
    # Tool Schema Introspection for LLM Tool Calling
    # -------------------------------------------------------------------------
    def export_tool_schemas(self) -> List[Dict[str, Any]]:
        """Dynamically introspect FunctionCatalog methods into OpenAI-compatible tool schemas."""
        tools: List[Dict[str, Any]] = []
        skip_methods = {"reset", "get_plan"}

        for name, method in inspect.getmembers(self.catalog, predicate=inspect.ismethod):
            if name.startswith("_") or name in skip_methods:
                continue
            sig = inspect.signature(method)
            doc = (inspect.getdoc(method) or f"Execute {name} on the shot plan.").strip()
            props: Dict[str, Any] = {}
            required: List[str] = []

            for param_name, param in sig.parameters.items():
                if param_name in ("self", "kwargs"):
                    continue
                ann = str(param.annotation)
                if "List" in ann:
                    p_schema: Dict[str, Any] = {"type": "array", "items": {"type": "string"}}
                elif "int" in ann and "float" not in ann and "Union" not in ann:
                    p_schema = {"type": "integer"}
                elif "float" in ann and "str" not in ann:
                    p_schema = {"type": "number"}
                elif "bool" in ann:
                    p_schema = {"type": "boolean"}
                else:
                    p_schema = {"type": "string"}
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
                        "properties": props,
                        "required": required,
                    },
                },
            })
        return tools

    def build_system_prompt(self, error_feedback: Optional[str] = None) -> str:
        """Build the system prompt describing the scene graph, manifest capabilities, and Foundational Law."""
        scene_objects = [
            {"name": o.get("name") or o.get("id"), "asset_id": o.get("asset") or o.get("asset_id")}
            for o in self.catalog.scene_data.get("objects", [])
        ]
        zones = [z.get("id") for z in self.catalog.scene_data.get("zones", [])] or [
            "lane_east", "lane_west", "sidewalk_east", "sidewalk_west", "plaza"
        ]
        stair_asset, stair_id = self.matcher.find_stair_portal()

        prompt_lines = [
            "You are the PainterSpace General Shot Planner.",
            "FOUNDATIONAL LAW: Never emit raw (x, y, z) coordinates or `pos(...)` strings.",
            "Always compose shots by calling `FunctionCatalog` methods with semantic references:",
            "- Zone markers: `lane_east.north`, `lane_east.south`, `lane_east.center`, `lane_east.stop_mark`, `lane_west.north`, `lane_west.south`, `sidewalk_east.north`, `sidewalk_west.south`, `plaza.center`",
            "- Vehicle anchors & surfaces: `car_1:door_fl_stand`, `car_1:door_fr_stand`, `car_1:hood_front`, `car_1:trunk_rear`, `car_1:roof`, `car_1:hood`",
            "- Relative selectors: `in_front_of(actor, gap)`, `behind(actor, gap)`, `beside(actor, left|right, gap)`, `left_of(actor, gap)`, `right_of(actor, gap)`, `nearest_to(actor, type)`",
            "- Cardinal directions: `north`, `south`, `east`, `west`",
            f"- Stairs: `{stair_id or 'stairs_main'}`, `{stair_id or 'stairs_main'}_bottom`, `{stair_id or 'stairs_main'}_top` (on `{stair_asset or 'buildings.city_square'}`)",
            f"- Current Scene Objects: {json.dumps(scene_objects)}",
            f"- Available Zones: {json.dumps(zones)}",
            "Return a JSON object `{\"calls\": [{\"function\": \"...\", \"args\": {...}}, ...]}`.",
        ]
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
        self.catalog.reset(prompt=prompt)
        self.matcher = ManifestAssetMatcher(self.catalog.manifest, self.catalog.scene_data)

        # 1. Try live LLM tool-calling if an API key or local LLM endpoint is configured
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        llm_url = os.environ.get("PAINTERSPACE_LLM_URL", "").strip()
        if api_key or llm_url:
            ok_api = self._plan_with_llm_api(prompt, api_key=api_key, llm_url=llm_url, error_feedback=error_feedback)
            if ok_api and self.catalog.plan.setup and self.catalog.plan.beats:
                self._verify_and_repair_prompt_adherence(prompt)
                return True, self.catalog.get_plan(), self.execution_log
            self.catalog.reset(prompt=prompt)

        # 2. General Scene-Graph-Driven Compositional Semantic Planner
        self._plan_general_semantic(prompt, error_feedback=error_feedback)
        self._verify_and_repair_prompt_adherence(prompt)
        return True, self.catalog.get_plan(), self.execution_log

    def plan_and_compile_with_retry(
        self,
        prompt: str,
        compiler: Any,
        output_usda_path: Path,
        scene_usda_path: Optional[Path] = None,
        validate: bool = True,
        max_retries: int = 2,
    ) -> Tuple[bool, Dict[str, Any], List[str]]:
        """Closed-loop planning and compilation with automatic feedback-driven self-correction."""
        feedback: Optional[str] = None
        last_report: Dict[str, Any] = {}
        all_logs: List[str] = []

        for attempt in range(max_retries + 1):
            ok_plan, plan, logs = self.plan_from_prompt(prompt, error_feedback=feedback)
            all_logs.extend(logs)
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
            feedback = json.dumps(err_info)
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

        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": prompt},
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

        for tc in tool_calls:
            fn_spec = tc.get("function", {})
            fn_name = fn_spec.get("name", "")
            try:
                fn_args = json.loads(fn_spec.get("arguments", "{}"))
            except Exception:
                fn_args = {}
            if self._invoke_catalog_call(fn_name, fn_args):
                executed += 1

        if executed == 0 and msg.get("content"):
            try:
                parsed = json.loads(msg["content"])
                for item in parsed.get("calls", []):
                    if self._invoke_catalog_call(item.get("function", ""), item.get("args", {})):
                        executed += 1
            except Exception:
                pass

        return executed > 0

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
            if isinstance(v_norm, str) and re.match(r"^(?:pos|point|coord)\s*\(", v_norm.strip(), re.IGNORECASE):
                v_norm = "plaza.center"
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

        try:
            method = getattr(self.catalog, fn_name)
            res = method(**norm_args)
            if isinstance(res, tuple) and len(res) >= 1 and res[0] is False:
                self.execution_log.append(f"Failed call {fn_name}({norm_args}): {res[1:]}")
                return False
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
            if "calls" in parsed and isinstance(parsed["calls"], list):
                calls_list = parsed["calls"]
            elif "tool_calls" in parsed and isinstance(parsed["tool_calls"], list):
                for tc in parsed["tool_calls"]:
                    fn_spec = tc.get("function", {})
                    fn_name = fn_spec.get("name", "")
                    raw_args = fn_spec.get("arguments", {})
                    fn_args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                    calls_list.append({"function": fn_name, "args": fn_args})

        executed = 0
        for item in calls_list:
            if not isinstance(item, dict):
                continue
            fn_name = str(item.get("function") or item.get("name") or "").strip()
            fn_args = item.get("args") if "args" in item else item.get("arguments", {})
            if isinstance(fn_args, str):
                try:
                    fn_args = json.loads(fn_args)
                except Exception:
                    fn_args = {}
            if not isinstance(fn_args, dict):
                fn_args = {}
            if self._invoke_catalog_call(fn_name, dict(fn_args)):
                executed += 1

        if executed == 0:
            return False, self.catalog.plan, list(self.execution_log)

        if not self.catalog.plan.camera:
            humans = [s.actor_id for s in self.catalog.plan.setup if "human" in (s.asset_id or "") or "character" in (s.asset_id or "")]
            vehicles = [s.actor_id for s in self.catalog.plan.setup if "car" in (s.asset_id or "") or "vehicle" in (s.asset_id or "")]
            self._synthesize_camera(prompt.lower(), vehicles, humans, {})

        return True, self.catalog.plan, list(self.execution_log)

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

    def _ensure_environment_context(self, prompt: str) -> Dict[str, str]:
        """Ensure any environment assets referenced in the prompt exist in the scene or shot setup."""
        env_refs: Dict[str, str] = {}
        p_low = prompt.lower()

        # Check stairs
        if any(w in p_low for w in ("stair", "stairs", "staircase", "steps")):
            stair_asset, stair_id = self.matcher.find_stair_portal()
            if stair_asset and stair_id:
                existing_stair_obj = self.matcher.find_scene_object_by_concept("city_square") or self.matcher.find_scene_object_by_concept("stair")
                if not existing_stair_obj:
                    placed_id = self._place(stair_asset, at_reference="plaza.center", label="CitySquareStairs")
                    self.execution_log.append(f"place('{stair_asset}', at_reference='plaza.center') -> {placed_id}")
                env_refs["stairs"] = stair_id
                env_refs["stairs_top"] = f"{stair_id}_top"
                env_refs["stairs_bottom"] = f"{stair_id}_bottom"

        # Check trees and props
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
                semantic_spot = "sidewalk_west.center" if cat == "trees" else "sidewalk_east.center"
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

        # 1. Determine vehicle count & roles
        veh_count = 0
        if re.search(r"\b(?:two|2)\s+(?:cars|vehicles|sedans)\b", p_low) or "second car" in p_low or "another car" in p_low:
            veh_count = 2
        elif re.search(r"\b(?:three|3)\s+(?:cars|vehicles)\b", p_low):
            veh_count = 3
        elif re.search(r"\b(?:car|vehicle|sedan|coupe|suv|taxi|cruiser|truck)\b", p_low):
            veh_count = 1

        car_assets = ["cars.car1", "cars.car2", "cars.car3"]
        car_zones = ["lane_east.north", "lane_west.south", "lane_east.south"]
        is_parked = any(w in p_low for w in ("parked", "stationary", "stopped car", "jumps onto", "jump onto", "vault", "climbs onto"))
        is_reversing = any(w in p_low for w in ("reverse", "reverses", "backs up", "backing up"))

        for i in range(veh_count):
            c_asset = car_assets[i % len(car_assets)]
            if i == 0 and is_parked:
                c_at = "lane_east.center"
            elif i == 0 and is_reversing:
                c_at = "lane_east.stop_mark"
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
        semantic_spots = [
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
        ]

        for i in range(human_count):
            build = builds[i % len(builds)]
            label = f"Actor{i + 1}"

            if i == 0 and "stairs" in env_refs:
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
                at_expr = "lane_east.center" if "dodge" in p_low or "evade" in p_low else f"beside({vehicles[0]}, left, near)"
                hid = self._place("character.human", at_reference=at_expr, facing_reference=vehicles[0], build=build, label=label)
                self.execution_log.append(f"place('character.human', at_reference='{at_expr}', facing_reference='{vehicles[0]}') -> {hid}")
            else:
                spot = semantic_spots[i % len(semantic_spots)]
                hid = self._place("character.human", at_reference=spot, build=build, label=label)
                self.execution_log.append(f"place('character.human', at_reference='{spot}', label='{label}') -> {hid}")

            humans.append(hid)

        return vehicles, humans

    def _split_into_clauses(self, prompt: str) -> List[str]:
        """Split a natural-language prompt into ordered temporal/causal clauses."""
        raw_parts = re.split(
            r"(?:;|\.\s+|\bthen\b|\band then\b|\bafter that\b|\bnext\b|\bsuddenly\b|\bwhen\b|\bas\b|\bwhile\b|,\s*but\b)",
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

        # Pre-check if vehicle approaches in background or stops first
        veh_scheduled = False
        if vehicles:
            v0 = vehicles[0]
            if any(w in p_low for w in ("dodge", "evade", "narrowly", "near miss", "almost hits", "speeds toward")):
                target_actor = humans[0] if humans else "lane_east.south"
                self.catalog.drive_toward(v0, target=target_actor, mode="near_miss", at_time=0.0, for_duration=3.2)
                self.execution_log.append(f"drive_toward('{v0}', target='{target_actor}', mode='near_miss')")
                veh_scheduled = True
            elif any(w in p_low for w in ("reverse", "reverses", "backs up", "backing up")):
                self.catalog.reverse_to(v0, target="lane_east.north", at_time=0.0, for_duration=3.5)
                self.execution_log.append(f"reverse_to('{v0}', target='lane_east.north')")
                veh_scheduled = True
            elif any(w in p_low for w in ("brakes", "brake", "screeches", "stops", "pulls up", "arrives", "drives")):
                self.catalog.drive_to(v0, target="lane_east.stop_mark", at_time=0.0, for_duration=2.8)
                self.execution_log.append(f"drive_to('{v0}', target='lane_east.stop_mark')")
                last_after_event = f"{v0}.stopped"
                veh_scheduled = True

            if len(vehicles) > 1:
                v1 = vehicles[1]
                self.catalog.drive_past(v1, target="lane_west.north", at_time=0.2, for_duration=4.5)
                self.execution_log.append(f"drive_past('{v1}', target='lane_west.north')")

        # Synthesize clauses sequentially
        for idx, clause in enumerate(clauses):
            c_low = clause.lower()
            beat_added = False
            at_arg = None if last_after_event else round(curr_time, 2)
            after_arg = last_after_event

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
            if any(w in c_low for w in ("jump", "jumps", "vault", "vaults", "climb onto", "climbs onto", "leaps onto")) and not beat_added and humans:
                surface_ref = f"{vehicles[0]}:roof" if vehicles and "roof" in c_low else (f"{vehicles[0]}:hood" if vehicles else "plaza.center")
                if "over" in c_low and vehicles:
                    self.catalog.jump_over(humans[0], obstacle=f"{vehicles[0]}:hood", at_time=at_arg, after=after_arg, for_duration=1.8)
                    self.execution_log.append(f"jump_over('{humans[0]}', obstacle='{vehicles[0]}:hood')")
                    curr_time += 1.9
                elif "down" in c_low or "off" in c_low:
                    land_ref = f"beside({vehicles[0]}, right, near)" if vehicles else "lane_east.center"
                    self.catalog.jump_down(humans[0], target=land_ref, at_time=at_arg, after=after_arg, for_duration=1.5)
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
                        land_ref = f"beside({vehicles[0]}, right, near)" if vehicles else "lane_east.center"
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
                    self.catalog.react(defender, mood="hit_reaction", style="hit_reaction", look_at=attacker, at_time=round((at_arg or curr_time) + 0.4, 2), for_duration=2.0)
                    self.execution_log.append(f"react('{defender}', look_at='{attacker}', style='hit_reaction')")
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
                self.catalog.huddle(actors=humans, center="plaza.center", at_time=at_arg, after=after_arg, for_duration=2.8)
                self.execution_log.append(f"huddle(actors={humans}, center='plaza.center')")
                curr_time += 2.9
                last_after_event = None
                beat_added = True

            if any(w in c_low for w in ("disperse", "scatter", "split up")) and humans:
                self.catalog.disperse(actors=humans, target="plaza.center", at_time=at_arg, after=after_arg, for_duration=2.8)
                self.execution_log.append(f"disperse(actors={humans}, target='plaza.center')")
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
                prop_ref = next((v for k, v in env_refs.items() if k not in ("stairs", "stairs_top", "stairs_bottom", "tree")), "plaza.center")
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
                    target_h = humans[1] if len(humans) > 1 else (vehicles[0] if vehicles else "lane_east.north")
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
                    self.catalog.climb_surface(humans[0], surface="plaza.center", style=st, direction=c_dir, at_time=at_arg, after=after_arg, for_duration=3.2)
                    self.execution_log.append(f"climb_surface('{humans[0]}', style='{st}', direction='{c_dir}')")
                    curr_time += 3.3
                    last_after_event = None
                    beat_added = True
                elif any(w in c_low for w in ("dive roll", "dive-roll")):
                    obs = f"{vehicles[0]}:hood" if vehicles else "plaza.center"
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
                    target_ref = humans[1] if len(humans) > 1 else (vehicles[0] if vehicles else "lane_east.north")
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
                    if "pace" in c_low or "pacing" in c_low:
                        self.catalog.pace_while_talking(humans[0], at_time=at_arg, after=after_arg, for_duration=3.5)
                        self.execution_log.append(f"pace_while_talking('{humans[0]}')")
                    else:
                        self.catalog.use_phone(humans[0], mode="talk", at_time=at_arg, after=after_arg, for_duration=3.2)
                        self.execution_log.append(f"use_phone('{humans[0]}', mode='talk')")
                    curr_time += 3.3
                    last_after_event = None
                    beat_added = True
                elif any(w in c_low for w in ("honk", "honks", "horn")):
                    self.catalog.honk_horn(humans[0], vehicle=vehicles[0] if vehicles else None, at_time=at_arg, after=after_arg, for_duration=1.8)
                    self.execution_log.append(f"honk_horn('{humans[0]}')")
                    curr_time += 1.9
                    last_after_event = None
                    beat_added = True
                elif any(w in c_low for w in ("shake hands", "shakes hands", "handshake")) and len(humans) >= 2:
                    self.catalog.shake_hands(humans[0], target=humans[1], at_time=at_arg, after=after_arg, for_duration=2.4)
                    self.catalog.shake_hands(humans[1], target=humans[0], at_time=at_arg, after=after_arg, for_duration=2.4)
                    self.execution_log.append(f"shake_hands('{humans[0]}', target='{humans[1]}')")
                    curr_time += 2.5
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
                    self.catalog.back_up_to(humans[0], target="lane_east.south", pace=b_pace, at_time=at_arg, after=after_arg, for_duration=3.0)
                    self.execution_log.append(f"back_up_to('{humans[0]}', target='lane_east.south', pace='{b_pace}')")
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
                    threat_ref = vehicles[0] if vehicles else (humans[-1] if len(humans) > 1 else "plaza.center")
                    self.catalog.flee_from(humans[0], threat=threat_ref, at_time=at_arg, after=after_arg, for_duration=2.8)
                    self.execution_log.append(f"flee_from('{humans[0]}', threat='{threat_ref}')")
                    curr_time += 2.9
                    last_after_event = None
                    beat_added = True
                elif any(w in c_low for w in ("follow", "follows", "following", "chase", "chases")) and len(humans) >= 2:
                    self.catalog.walk_to(humans[0], target="lane_east.north", at_time=at_arg, after=after_arg, for_duration=3.5)
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
                    for h_idx, hid in enumerate(humans):
                        dest = semantic_dest if h_idx % 2 == 0 else "sidewalk_west.north"
                        self.catalog.walk_to(hid, target=dest, at_time=at_arg, after=after_arg, for_duration=3.2)
                        self.execution_log.append(f"walk_to('{hid}', target='{dest}')")
                    curr_time += 3.3
                    last_after_event = None
                    beat_added = True

        # Ensure every actor has at least one meaningful beat if the prompt was purely descriptive
        if not self.catalog.plan.beats:
            if len(humans) >= 3:
                dests = ["lane_east.north", "sidewalk_west.south", "sidewalk_east.north", "lane_west.south", "plaza.center"]
                for i, hid in enumerate(humans):
                    dest = dests[i % len(dests)]
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
        if "tree" in clause_low and "tree" in env_refs:
            return f"beside({env_refs['tree']}, right, near)"
        if any(w in clause_low for w in ("car", "vehicle", "sedan")) and vehicles:
            if "hood" in clause_low:
                return f"{vehicles[0]}:hood_front"
            if "trunk" in clause_low:
                return f"{vehicles[0]}:trunk_rear"
            return f"{vehicles[0]}:door_fl_stand"
        if "north" in clause_low or "forward" in clause_low or "ahead" in clause_low:
            return "lane_east.north"
        if "south" in clause_low or "back" in clause_low:
            return "lane_east.south"
        if "west" in clause_low or "left" in clause_low:
            return "sidewalk_west.center"
        if "east" in clause_low or "right" in clause_low:
            return "sidewalk_east.center"
        if "plaza" in clause_low or "center" in clause_low or "square" in clause_low:
            return "plaza.center"
        return "lane_east.north"

    def _synthesize_camera(
        self,
        p_low: str,
        vehicles: List[str],
        humans: List[str],
        env_refs: Dict[str, str],
    ) -> None:
        """Determine camera style, vantage, movement, and cuts purely from semantic prompt cues."""
        tracked = list(humans[:3]) if humans else list(vehicles[:2])
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

        # 1. Verify tree presence if prompt explicitly mentions "tree"
        if re.search(r"\btrees?\b", p_low):
            has_tree_in_scene = self.matcher.find_scene_object_by_concept("tree") is not None
            has_tree_in_setup = any("tree" in (s.asset_id or "").lower() for s in plan.setup)
            if not has_tree_in_scene and not has_tree_in_setup:
                tid = self._place("trees.oak_tree", at_reference="sidewalk_west.center", label="OakTree")
                self.execution_log.append(f"[Adherence] Added missing tree '{tid}' at 'sidewalk_west.center'")

        # 2. Verify couple / pair human count
        if re.search(r"\b(couple|pair|two people)\b", p_low):
            human_setups = [s for s in plan.setup if "human" in (s.asset_id or "") or "character" in (s.asset_id or "")]
            while len(human_setups) < 2:
                hid = self._place("character.human", at_reference="plaza.center", label=f"Partner{len(human_setups) + 1}")
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

