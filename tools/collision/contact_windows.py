"""Inclusive contact windows expressed in unrounded simulation seconds."""
import math

# Frame/second conversion and accumulated action durations may differ by a few
# representable floats. This is an arithmetic allowance, not a frame-sized or
# millisecond extension of permission to collide.
CONTACT_TIME_ROUNDOFF_ULPS = 8


def window_error(rule):
    try:
        for key in ('t0', 't1'):
            if key in rule and not math.isfinite(float(rule[key])):
                return f'{key} must be finite simulation seconds'
        if float(rule.get('t0', -math.inf)) > float(rule.get('t1', math.inf)):
            return 't0 must not be later than t1'
    except (TypeError, ValueError, OverflowError):
        return 'contact times must be numeric simulation seconds'
    return None


def contains_time(rule, seconds):
    """Include exact endpoints and floating-point roundoff, never adjacent samples."""
    seconds = float(seconds)
    if not math.isfinite(seconds) or window_error(rule):
        return False
    for key, lower in (('t0', True), ('t1', False)):
        if key not in rule:
            continue
        boundary = float(rule[key])
        tolerance = CONTACT_TIME_ROUNDOFF_ULPS * max(math.ulp(seconds), math.ulp(boundary))
        if lower and seconds < boundary - tolerance:
            return False
        if not lower and seconds > boundary + tolerance:
            return False
    return True
