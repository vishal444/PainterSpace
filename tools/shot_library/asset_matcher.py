"""Manifest-driven Semantic Asset Matcher for PainterSpace.

Provides generic, data-driven tokenization and asset matching against manifest.json
without hardcoded asset names, special cases, or regex traps.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Set, Tuple


def tokenize(text: str) -> Set[str]:
    """Tokenize a string by splitting CamelCase, underscores, dots, hyphens, and whitespace.

    Example:
      'StreetAvenue' -> {'street', 'avenue'}
      'OakTree' -> {'oak', 'tree'}
      'trees.oak_tree' -> {'trees', 'tree', 'oak'}
      'furnitures.circle_table' -> {'furnitures', 'furniture', 'circle', 'table'}
    """
    if not text:
        return set()
    # Split CamelCase (e.g. StreetAvenue -> Street Avenue, OakTree -> Oak Tree)
    expanded = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(text))
    # Extract alphanumeric tokens
    tokens = set(re.findall(r"[a-zA-Z0-9]+", expanded.lower()))
    # Simple morphological stemming (singular/plural)
    stems = set(tokens)
    for tok in tokens:
        if tok.endswith("ies") and len(tok) > 4:
            stems.add(tok[:-3] + "y")
        elif tok.endswith("es") and len(tok) > 3:
            stems.add(tok[:-2])
        elif tok.endswith("s") and len(tok) > 3:
            stems.add(tok[:-1])
        elif not tok.endswith("s"):
            stems.add(tok + "s")
    return stems


class ManifestAssetMatcher:
    """Generic manifest-driven matcher for props, actors, and scene objects."""

    STOP_WORDS = {
        "a", "an", "the", "in", "on", "at", "to", "for", "of", "with", "by", "near",
        "beside", "and", "or", "but", "is", "are", "was", "were", "be", "been", "being",
        "have", "has", "had", "do", "does", "did", "from", "into", "onto", "over", "under",
        "shot", "scene", "frame", "view", "camera", "angle", "lighting", "look", "toward",
        "towards", "something", "their", "them", "they", "its", "it", "standing", "soaring",
        "happy", "suddenly", "turn", "notice", "approaching", "evening", "misty", "slowly",
        "quickly", "fast", "both", "all", "some", "more", "most", "other", "another",
    }

    def __init__(self, manifest: Dict[str, Any]) -> None:
        self.manifest = manifest
        self.assets = manifest.get("assets", [])
        self._index: List[Dict[str, Any]] = []

        for a in self.assets:
            aid = a.get("id", "")
            tags = a.get("tags", [])
            dprim = a.get("default_prim", "")
            is_env = (
                ("environment" in tags or aid.startswith("environment."))
                and "prop" not in tags
                and "object" not in tags
            )

            tokens = set()
            tokens.update(tokenize(aid))
            tokens.update(tokenize(dprim))
            for t in tags:
                tokens.update(tokenize(t))

            self._index.append({
                "asset": a,
                "asset_id": aid,
                "default_prim": dprim,
                "is_env": is_env,
                "tokens": tokens,
            })

    def match_prim(self, prim_name: str, asset_id: str = "") -> List[Dict[str, Any]]:
        """Find manifest assets that match a prim's name and/or asset_id."""
        prim_tokens = tokenize(prim_name) | tokenize(asset_id)
        scored = []
        for entry in self._index:
            overlap = prim_tokens & entry["tokens"]
            if overlap:
                score = len(overlap)
                if asset_id and entry["asset_id"] == asset_id:
                    score += 10
                scored.append((score, entry["asset"]))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [s[1] for s in scored]

    def prim_has_concept(self, prim_name: str, asset_id: str, concept: str) -> bool:
        """Check if a prim represents a given concept (e.g. 'tree', 'car', 'table')."""
        prim_tokens = tokenize(prim_name) | tokenize(asset_id)
        concept_tokens = tokenize(concept)
        if prim_tokens & concept_tokens:
            return True
        for a in self.match_prim(prim_name, asset_id):
            a_tokens = tokenize(a.get("id", "")) | set(a.get("tags", []))
            if a_tokens & concept_tokens:
                return True
        return False

    def is_concept_in_scene(self, concept: str, scene_data: Dict[str, Any]) -> bool:
        """Check if any static object in scene_data represents the concept."""
        for obj in scene_data.get("objects", []):
            if self.prim_has_concept(obj.get("name", "") or obj.get("id", ""), obj.get("asset", ""), concept):
                return True
        return False

    def is_concept_in_actors(self, concept: str, actors: List[Dict[str, Any]]) -> bool:
        """Check if any registered actor matches the concept."""
        for a in actors:
            aid = a.get("asset_id", "")
            lbl = a.get("label", "")
            if self.prim_has_concept(lbl, aid, concept):
                return True
        return False

    def find_prompt_props(self, prompt: str) -> List[Dict[str, Any]]:
        """Identify discrete placeable props mentioned in user prompt from manifest assets."""
        p_tokens = tokenize(prompt) - self.STOP_WORDS
        matched_assets = []
        seen_concepts = set()

        for entry in self._index:
            if entry["is_env"]:
                continue
            aid = entry["asset_id"]
            if aid in ("character.human", "character.male", "character.female") or "character" in entry["asset"].get("tags", []):
                continue  # Characters handled separately
            overlap = p_tokens & entry["tokens"]
            if overlap:
                score = len(overlap)
                # Primary concept token
                concept_key = sorted(overlap)[0]
                matched_assets.append({
                    "score": score,
                    "asset": entry["asset"],
                    "concept": concept_key,
                    "overlap": overlap,
                })

        matched_assets.sort(key=lambda x: x["score"], reverse=True)

        chosen = []
        for m in matched_assets:
            c = m["concept"]
            if c not in seen_concepts:
                seen_concepts.add(c)
                asset_copy = dict(m["asset"])
                asset_copy["concept"] = c
                chosen.append(asset_copy)
        return chosen
