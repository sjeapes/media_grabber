"""The darktable options you can change, with checks so a bad value can never reach darktable.

Everything here only affects photos developed from now on: an existing JPEG is never
replaced, so changing a setting never rewrites your earlier results.
"""
from __future__ import annotations

import re

# Look presets. The value is darktable's own "auto-apply pixel workflow defaults" choice, which
# decides which modules darktable applies by itself to a raw photo (the "automatic look").
PRESETS = {
    "standard": {"label": "Standard (scene-referred, filmic)", "workflow": "scene-referred (filmic)",
                 "help": "darktable's modern default: exposure lift, colour calibration and the "
                         "filmic tone curve. A good all-round starting point."},
    "sigmoid": {"label": "Sigmoid (scene-referred)", "workflow": "scene-referred (sigmoid)",
                "help": "Like Standard, but with the newer sigmoid tone curve: simpler, with "
                        "gentler highlights and more contrast in the midtones."},
    "classic": {"label": "Classic (display-referred)", "workflow": "display-referred (legacy)",
                "help": "The older darktable look: base curve and white balance. Closer to a "
                        "camera JPEG, less highlight recovery."},
    "minimal": {"label": "Minimal (no automatic look)", "workflow": "none",
                "help": "Applies nothing automatic — flat and dull, a neutral starting point "
                        "if you supply your own XMP template."},
}
COLOURS = {"SRGB": "sRGB (best for screens and Google Photos)", "ADOBERGB": "Adobe RGB (wider, for printing)",
           "DISPLAY_P3": "Display P3 (wide, for modern phones and screens)"}
INTENTS = {"PERCEPTUAL": "Perceptual", "RELATIVE_COLORIMETRIC": "Relative colorimetric"}

DEFAULT_DT = {"preset": "standard", "quality": None, "max_size": 0, "hq": True,
              "colour": "SRGB", "intent": "PERCEPTUAL", "xmp_template": ""}
XMP_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._()-]{0,100}\.xmp$")


class DtError(ValueError):
    """A setting that can't be used; the message is shown to the user."""


def clean(new: dict, base: dict | None = None) -> dict:
    """Validated settings: `base` (or the defaults) with the changes in `new` applied."""
    out = dict(DEFAULT_DT)
    out.update(base or {})
    if not isinstance(new, dict):
        raise DtError("Invalid settings")
    for key, val in new.items():
        if key not in DEFAULT_DT:
            raise DtError(f"Unknown setting “{key}”")
        if key == "preset":
            if val not in PRESETS:
                raise DtError("Choose one of the looks in the list")
        elif key == "quality":
            if val is not None and (isinstance(val, bool) or not isinstance(val, int) or not 50 <= val <= 100):
                raise DtError("JPEG quality must be a whole number from 50 to 100")
        elif key == "max_size":
            if isinstance(val, bool) or not isinstance(val, int) or not (val == 0 or 400 <= val <= 20000):
                raise DtError("Longest side must be 0 (full size) or between 400 and 20000 pixels")
        elif key == "hq":
            if not isinstance(val, bool):
                raise DtError("Invalid setting")
        elif key == "colour":
            if val not in COLOURS:
                raise DtError("Choose a colour space from the list")
        elif key == "intent":
            if val not in INTENTS:
                raise DtError("Choose a rendering intent from the list")
        elif key == "xmp_template":
            if not isinstance(val, str) or (val and not XMP_NAME.match(val)):
                raise DtError("That isn't a valid template file name")
        out[key] = val
    return out


def load(saved) -> dict:
    """Settings read back from disk; anything invalid quietly falls back to the default."""
    out = dict(DEFAULT_DT)
    if isinstance(saved, dict):
        for key, val in saved.items():
            try:
                out = clean({key: val}, out)
            except DtError:
                pass
    return out


def describe() -> dict:
    return {"presets": [{"id": k, "label": v["label"], "help": v["help"]} for k, v in PRESETS.items()],
            "colours": [{"id": k, "label": v} for k, v in COLOURS.items()],
            "intents": [{"id": k, "label": v} for k, v in INTENTS.items()],
            "defaults": DEFAULT_DT}
