"""Plan Data Model for PainterSpace Shot Function Library.

Defines the structure for shot_plan.json, individual beats, start conditions,
event definitions, parameters, and structured validation error reporting.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Union


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
class ShotPlan:
    schema_version: str = "1.0"
    prompt: str = ""
    seed: int = 42
    setup: List[SetupAction] = field(default_factory=list)
    beats: List[Beat] = field(default_factory=list)
    camera: Optional[Dict[str, Any]] = None
    max_duration_s: Optional[float] = None

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
        }
        if self.camera:
            d["camera"] = self.camera
        if self.max_duration_s is not None:
            d["max_duration_s"] = self.max_duration_s
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
            camera=data.get("camera"),
            max_duration_s=data.get("max_duration_s"),
        )

    @classmethod
    def from_json(cls, json_str: str) -> ShotPlan:
        return cls.from_dict(json.loads(json_str))

