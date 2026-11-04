"""Resolve common combat move names to clips present in the active manifest."""


def resolve_attack_moves(moves, clips):
    aliases = {"punch": "punching", "block": "boxing_block", "counterattack": "combo_punch"}
    moves = moves or ["left_hook", "elbow_punch"]
    if isinstance(moves, str):
        moves = [moves]
    resolved = [move if move in clips else aliases.get(move, move) for move in moves]
    missing = [move for move in resolved if move not in clips]
    if missing:
        raise ValueError("Unknown attack clips: " + ", ".join(missing))
    return resolved
