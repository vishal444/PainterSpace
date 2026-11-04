"""Plan Data Model for PainterSpace Shot Function Library.

Defines the structure for shot_plan.json, individual beats, start conditions,
event definitions, parameters, and structured validation error reporting.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Literal, Optional, Union, get_args


InteractionTiming = Literal['sequence', 'overlap', 'parallel', 'simultaneous']
INTERACTION_TIMINGS = get_args(InteractionTiming)


@dataclass
class StructuredError:
    error_code: str
    message: str
    beat_id: Optional[str] = None
    actor_id: Optional[str] = None
    details: Dict[str, Any] = field(default_factory=dict)
    suggestions: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class TimingCondition:
    type: str  # "at", "after", "with", "when_closer_than", "when_stopped", "until", "for_duration"
    value: Union[float, str, Dict[str, Any]]
    actor_a: Optional[str] = None
    actor_b: Optional[str] = None
    gap: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        d = {"type": self.type, "value": self.value}
        if self.actor_a:
            d["actor_a"] = self.actor_a
        if self.actor_b:
            d["actor_b"] = self.actor_b
        if self.gap:
            d["gap"] = self.gap
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> TimingCondition:
        return cls(
            type=str(data.get("type", "after")),
            value=data.get("value", 0.0),
            actor_a=data.get("actor_a"),
            actor_b=data.get("actor_b"),
            gap=data.get("gap"),
        )


@dataclass
class Beat:
    beat_id: str
    function: str
    actor_id: str
    params: Dict[str, Any] = field(default_factory=dict)
    start_condition: Optional[TimingCondition] = None
    duration_condition: Optional[TimingCondition] = None
    source_function: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "beat_id": self.beat_id,
            "function": self.function,
            "actor_id": self.actor_id,
            "params": dict(self.params),
        }
        if self.start_condition:
            d["start_condition"] = self.start_condition.to_dict()
        if self.duration_condition:
            d["duration_condition"] = self.duration_condition.to_dict()
        if self.source_function is not None:
            d['source_function'] = self.source_function
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Beat:
        sc = data.get("start_condition")
        dc = data.get("duration_condition")
        return cls(
            beat_id=str(data.get("beat_id", "")),
            function=str(data.get("function", "")),
            actor_id=str(data.get("actor_id", "")),
            params=dict(data.get("params", {})),
            start_condition=TimingCondition.from_dict(sc) if sc else None,
            duration_condition=TimingCondition.from_dict(dc) if dc else None,
            source_function=data.get('source_function'),
        )


@dataclass
class SetupAction:
    action_type: str  # "place", "set_appearance", "set_variant", "remove"
    actor_id: str
    asset_id: Optional[str] = None
    label: Optional[str] = None
    at_reference: Optional[str] = None
    facing_reference: Optional[str] = None
    appearance: Dict[str, Any] = field(default_factory=dict)
    variant: Dict[str, Any] = field(default_factory=dict)
    position: Optional[List[float]] = None
    facing_deg: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None and v != {} and v != []}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> SetupAction:
        return cls(
            action_type=str(data.get("action_type", "place")),
            actor_id=str(data.get("actor_id", "")),
            asset_id=data.get("asset_id"),
            label=data.get("label"),
            at_reference=data.get("at_reference"),
            facing_reference=data.get("facing_reference"),
            appearance=dict(data.get("appearance", {})),
            variant=dict(data.get("variant", {})),
            position=data.get("position"),
            facing_deg=data.get("facing_deg"),
        )


@dataclass
class InteractionPlan:
    interaction_id: str
    participants: List[str]
    actions: List[str]
    timing: str = "sequence"
    intent: str = ""
    after: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> InteractionPlan:
        after = data.get('after', [])
        if not isinstance(after, list) or any(not isinstance(parent, str) or not parent for parent in after):
            raise ValueError('Interaction after must be a list of nonempty interaction IDs.')
        return cls(
            interaction_id=str(data.get("interaction_id", "")),
            participants=list(data.get("participants", [])),
            actions=list(data.get("actions", [])),
            timing=str(data.get("timing", "sequence")),
            intent=str(data.get("intent", "")),
            after=list(after),
        )


@dataclass
class SpatialIntent:
    intent_id: str
    actors: List[str]
    movements: Dict[str, str]
    source_region: str
    destination_region: str
    departure_timing: str = 'simultaneous'
    gait: str = 'walk'
    bindings: Dict[str, Any] = field(default_factory=dict)
    source_regions: List[str] = field(default_factory=list)
    via_regions: List[str] = field(default_factory=list)
    origin_layout: str = 'specified'

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, data):
        return cls(intent_id=str(data['intent_id']), actors=list(data['actors']),
                   movements=dict(data['movements']), source_region=str(data['source_region']),
                   destination_region=str(data['destination_region']),
                   departure_timing=str(data.get('departure_timing', 'simultaneous')),
                   gait=str(data.get('gait', 'walk')), bindings=dict(data.get('bindings', {})),
                   source_regions=list(data.get('source_regions', [])),
                   via_regions=list(data.get('via_regions', [])),
                   origin_layout=str(data.get('origin_layout', 'specified')))


@dataclass
class ShotPlan:
    schema_version: str = "1.0"
    prompt: str = ""
    seed: int = 42
    setup: List[SetupAction] = field(default_factory=list)
    beats: List[Beat] = field(default_factory=list)
    interactions: List[InteractionPlan] = field(default_factory=list)
    camera: Optional[Dict[str, Any]] = None
    max_duration_s: Optional[float] = None
    camera_locked: bool = False
    brief_conflicts: List[Any] = field(default_factory=list)
    spatial_intents: List[SpatialIntent] = field(default_factory=list)

    def add_setup(self, action: SetupAction) -> None:
        self.setup.append(action)

    def add_beat(self, beat: Beat) -> None:
        self.beats.append(beat)

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "schema_version": self.schema_version,
            "prompt": self.prompt,
            "seed": self.seed,
            "setup": [s.to_dict() for s in self.setup],
            "beats": [b.to_dict() for b in self.beats],
            "interactions": [i.to_dict() for i in self.interactions],
            "brief_conflicts": self.brief_conflicts,
            "spatial_intents": [intent.to_dict() for intent in self.spatial_intents],
        }
        if self.camera:
            d["camera"] = self.camera
        if self.max_duration_s is not None:
            d["max_duration_s"] = self.max_duration_s
        if self.camera_locked:
            d["camera_locked"] = self.camera_locked
        return d

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> ShotPlan:
        return cls(
            schema_version=str(data.get("schema_version", "1.0")),
            prompt=str(data.get("prompt", "")),
            seed=int(data.get("seed", 42)),
            setup=[SetupAction.from_dict(s) for s in data.get("setup", [])],
            beats=[Beat.from_dict(b) for b in data.get("beats", [])],
            interactions=[InteractionPlan.from_dict(i) for i in data.get("interactions", [])],
            camera=data.get("camera"),
            max_duration_s=data.get("max_duration_s"),
            camera_locked=bool(data.get("camera_locked", False)),
            brief_conflicts=list(data.get("brief_conflicts", [])),
            spatial_intents=[SpatialIntent.from_dict(item) for item in data.get('spatial_intents', [])],
        )

    @classmethod
    def from_json(cls, json_str: str) -> ShotPlan:
        return cls.from_dict(json.loads(json_str))

