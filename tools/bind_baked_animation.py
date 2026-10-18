#!/usr/bin/env python3
"""Bind a baked animation to the skeleton below a referenced character prim.

This is the final deterministic step after ``bake_animation.py``.  It locates
the skeleton in the *composed* shot, authors a BakedAnim reference in the shot
layer, and points ``skel:animationSource`` at it.  No skeleton path is supplied
by an LLM or copied into an action list.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from pxr import Sdf, Usd, UsdSkel


def bind(shot: Path, character_path: str, animation_file: Path, name: str = "BakedAnim") -> tuple[str, str]:
    """Author the portable animation reference and return animation/skeleton paths."""
    shot = shot.resolve()
    animation_file = animation_file.resolve()
    if not animation_file.is_file():
        raise ValueError(f"animation does not exist: {animation_file}")
    stage = Usd.Stage.Open(str(shot))
    if not stage:
        raise ValueError(f"Cannot open shot: {shot}")
    character = stage.GetPrimAtPath(character_path)
    if not character:
        raise ValueError(f"Character prim does not exist: {character_path}")
    skeletons = [prim for prim in Usd.PrimRange(character) if prim.IsA(UsdSkel.Skeleton)]
    if len(skeletons) != 1:
        raise ValueError(f"Expected one Skeleton below {character_path}; found {len(skeletons)}")
    relative = os.path.relpath(animation_file, shot.parent).replace(os.sep, "/")
    stage.SetEditTarget(stage.GetRootLayer())
    animation_path = character.GetPath().AppendChild(name)
    animation = UsdSkel.Animation.Define(stage, animation_path)
    animation.GetPrim().GetReferences().ClearReferences()
    animation.GetPrim().GetReferences().AddReference(str(relative))
    UsdSkel.BindingAPI(skeletons[0]).CreateAnimationSourceRel().SetTargets([animation_path])
    stage.GetRootLayer().Save()
    return str(animation_path), str(skeletons[0].GetPath())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shot", required=True, type=Path)
    parser.add_argument("--character-path", required=True,
                        help="Prim path of the referenced character, e.g. /World/Hero")
    parser.add_argument("--animation", required=True, type=Path)
    parser.add_argument("--name", default="BakedAnim")
    args = parser.parse_args()
    try:
        animation_path, skeleton_path = bind(args.shot, args.character_path, args.animation, args.name)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"bound {animation_path} to {skeleton_path} in {args.shot.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
