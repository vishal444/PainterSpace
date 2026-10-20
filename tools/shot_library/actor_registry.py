"""Actor Registry and Identity system for PainterSpace Shot Function Library.

Guarantees unique actor identification, human-friendly labels, appearance variants
for identical assets (tinting, height scaling, descriptive tags), stage serialization
via USD prim customData, and round-trip reconstruction from any composed stage.
"""
from __future__ import annotations

import copy
import json
import math
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from manifest_schema import CHARACTER_APPEARANCE_VARIANTS


@dataclass
class ActorAppearance:
    color_variant: str = "blue"
    tint_rgb: List[float] = field(default_factory=lambda: [0.22, 0.45, 0.85])
    height_scale: float = 1.0
    descriptive_tags: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> ActorAppearance:
        return cls(
            color_variant=str(data.get("color_variant", "blue")),
            tint_rgb=list(data.get("tint_rgb", [0.22, 0.45, 0.85])),
            height_scale=float(data.get("height_scale", 1.0)),
            descriptive_tags=list(data.get("descriptive_tags", [])),
        )


@dataclass
class ActorRecord:
    actor_id: str
    label: str
    asset_id: str
    role_tags: List[str] = field(default_factory=list)
    appearance: ActorAppearance = field(default_factory=ActorAppearance)
    prim_path: str = ""

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["appearance"] = self.appearance.to_dict()
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> ActorRecord:
        app_data = data.get("appearance", {})
        if isinstance(app_data, dict):
            app = ActorAppearance.from_dict(app_data)
        else:
            app = ActorAppearance()
        return cls(
            actor_id=str(data.get("actor_id", "")),
            label=str(data.get("label", "")),
            asset_id=str(data.get("asset_id", "")),
            role_tags=list(data.get("role_tags", [])),
            appearance=app,
            prim_path=str(data.get("prim_path", "")),
        )


class ActorRegistry:
    def __init__(self) -> None:
        self._actors: Dict[str, ActorRecord] = {}  # keyed by actor_id
        self._label_map: Dict[str, str] = {}      # label.lower() -> actor_id
        self._counter: Dict[str, int] = {}        # prefix -> next integer

    def register(
        self,
        asset_id: str,
        label: Optional[str] = None,
        role_tags: Optional[List[str]] = None,
        appearance: Optional[ActorAppearance] = None,
        actor_id: Optional[str] = None,
        prim_path: Optional[str] = None,
    ) -> ActorRecord:
        """Register a new actor, assigning a unique ID and distinct appearance variant if needed."""
        # Determine prefix (human, car, prop, etc.)
        if "human" in asset_id or "character" in asset_id:
            prefix = "human"
        elif "car" in asset_id or "vehicle" in asset_id:
            prefix = "car"
        else:
            prefix = asset_id.split(".")[-1].lower()

        # Assign unique actor_id if not provided
        if not actor_id:
            idx = self._counter.get(prefix, 1)
            while f"{prefix}_{idx}" in self._actors:
                idx += 1
            actor_id = f"{prefix}_{idx}"
            self._counter[prefix] = idx + 1
        else:
            # Update counter
            if "_" in actor_id:
                parts = actor_id.rsplit("_", 1)
                if parts[1].isdigit():
                    self._counter[parts[0]] = max(self._counter.get(parts[0], 1), int(parts[1]) + 1)

        # Unique label
        if not label:
            label = f"{prefix.capitalize()}_{actor_id.split('_')[-1]}"
        base_label = label
        c = 2
        while label.lower() in self._label_map and self._label_map[label.lower()] != actor_id:
            label = f"{base_label}_{c}"
            c += 1

        # Auto-assign appearance variant if not provided
        if appearance is None:
            appearance = self._auto_assign_appearance(asset_id)

        # Prim path fallback
        if not prim_path:
            prim_path = f"/World/{label.replace(' ', '_')}"

        record = ActorRecord(
            actor_id=actor_id,
            label=label,
            asset_id=asset_id,
            role_tags=list(role_tags or []),
            appearance=appearance,
            prim_path=prim_path,
        )

        self._actors[actor_id] = record
        self._label_map[label.lower()] = actor_id
        return record

    def _auto_assign_appearance(self, asset_id: str) -> ActorAppearance:
        """Pick the next distinct appearance variant for characters."""
        if "human" not in asset_id and "character" not in asset_id:
            return ActorAppearance(color_variant="default", tint_rgb=[1.0, 1.0, 1.0], height_scale=1.0)

        # Count existing characters
        existing_chars = [a for a in self._actors.values() if "human" in a.asset_id or "character" in a.asset_id]
        var_idx = len(existing_chars) % len(CHARACTER_APPEARANCE_VARIANTS)
        preset = CHARACTER_APPEARANCE_VARIANTS[var_idx]
        return ActorAppearance(
            color_variant=preset["color_variant"],
            tint_rgb=list(preset["tint_rgb"]),
            height_scale=float(preset["height_scale"]),
            descriptive_tags=list(preset["descriptive_tags"]),
        )

    def get(self, ref: str) -> Optional[ActorRecord]:
        """Resolve an actor reference by actor_id or label."""
        if not ref:
            return None
        if ref in self._actors:
            return self._actors[ref]
        ref_lower = ref.lower()
        if ref_lower in self._label_map:
            return self._actors[self._label_map[ref_lower]]
        # Also try case-insensitive actor_id
        for aid, rec in self._actors.items():
            if aid.lower() == ref_lower:
                return rec
        return None

    def rename_actor(self, ref: str, new_label: str) -> Tuple[bool, str]:
        """Rename an actor's label while keeping its actor_id permanent."""
        rec = self.get(ref)
        if not rec:
            return False, f"Actor '{ref}' not found"
        if not new_label or not new_label.strip():
            return False, "Label cannot be empty"

        clean_label = new_label.strip()
        if clean_label.lower() in self._label_map and self._label_map[clean_label.lower()] != rec.actor_id:
            return False, f"Label '{clean_label}' is already in use by actor {self._label_map[clean_label.lower()]}"

        old_label = rec.label
        if old_label.lower() in self._label_map:
            del self._label_map[old_label.lower()]

        rec.label = clean_label
        self._label_map[clean_label.lower()] = rec.actor_id
        return True, ""

    def remove_actor(self, ref: str) -> Optional[ActorRecord]:
        rec = self.get(ref)
        if not rec:
            return None
        del self._actors[rec.actor_id]
        if rec.label.lower() in self._label_map:
            del self._label_map[rec.label.lower()]
        return rec

    def find_actor(self, query: str) -> List[ActorRecord]:
        """Match descriptive tags, role, or asset (e.g. 'the tall one', 'red jacket', 'car')."""
        q = query.lower()
        matches = []
        for rec in self._actors.values():
            score = 0
            if q in rec.actor_id.lower() or q in rec.label.lower() or q in rec.asset_id.lower():
                score += 5
            for tag in rec.role_tags:
                if q in tag.lower() or tag.lower() in q:
                    score += 3
            for dtag in rec.appearance.descriptive_tags:
                if q in dtag.lower() or dtag.lower() in q:
                    score += 4
            if rec.appearance.color_variant in q:
                score += 3
            if "tall" in q and rec.appearance.height_scale > 1.02:
                score += 3
            if "short" in q and rec.appearance.height_scale < 0.98:
                score += 3
            if score > 0:
                matches.append((score, rec))

        matches.sort(key=lambda x: x[0], reverse=True)
        return [m[1] for m in matches]

    def list_actors(self) -> List[Dict[str, Any]]:
        return [rec.to_dict() for rec in self._actors.values()]

    def to_stage_custom_data(self, actor_id: str) -> Dict[str, Any]:
        """Return dict ready to be written to prim customData."""
        rec = self._actors.get(actor_id)
        if not rec:
            return {}
        return {
            "actor_id": rec.actor_id,
            "label": rec.label,
            "asset_id": rec.asset_id,
            "role_tags": rec.role_tags,
            "appearance_variant": rec.appearance.color_variant,
            "height_scale": rec.appearance.height_scale,
            "tint_rgb": rec.appearance.tint_rgb,
            "descriptive_tags": rec.appearance.descriptive_tags,
        }

    @classmethod
    def from_stage(cls, stage: Any) -> ActorRegistry:
        """Reconstruct registry from customData authored on stage prims."""
        registry = cls()
        for prim in stage.Traverse():
            if not prim.IsValid():
                continue
            actor_id = prim.GetCustomDataByKey("actor_id")
            if not actor_id:
                # Also check child meshes or references
                continue
            actor_id = str(actor_id)
            label = str(prim.GetCustomDataByKey("label") or prim.GetName())
            asset_id = str(prim.GetCustomDataByKey("asset_id") or "")
            role_tags = list(prim.GetCustomDataByKey("role_tags") or [])
            color_variant = str(prim.GetCustomDataByKey("appearance_variant") or "default")
            height_scale = float(prim.GetCustomDataByKey("height_scale") or 1.0)
            tint_val = prim.GetCustomDataByKey("tint_rgb")
            tint_rgb = list(tint_val) if tint_val else [1.0, 1.0, 1.0]
            descriptive_tags = list(prim.GetCustomDataByKey("descriptive_tags") or [])

            app = ActorAppearance(
                color_variant=color_variant,
                tint_rgb=tint_rgb,
                height_scale=height_scale,
                descriptive_tags=descriptive_tags,
            )
            registry.register(
                asset_id=asset_id,
                label=label,
                role_tags=role_tags,
                appearance=app,
                actor_id=actor_id,
                prim_path=str(prim.GetPath()),
            )
        return registry

