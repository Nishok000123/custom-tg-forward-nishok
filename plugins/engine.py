"""
One forwarding engine shared by batch (/fwd), live (/live) and schedules.

Every forward ("task") carries its OWN filter snapshot (see build_task_filters).
The user's global /settings are only the defaults copied in when a task is created,
so changing /settings later never changes a forward that is already running.

Public API:
    build_task_filters(configs, overrides=None) -> dict
    should_forward(msg, task_filters, seen=None) -> (bool, reason)
    render_caption(msg, task_filters) -> Optional[str]
    summarize_filters(task_filters) -> str     (one-line human summary)
"""
import copy
import html
from typing import Optional, Tuple

MEDIA_TYPES = (
    "text", "photo", "video", "document", "audio",
    "voice", "animation", "sticker", "poll",
)

DEFAULT_TASK_FILTERS = {
    # message types: True = forward, False = skip
    "types": {t: True for t in MEDIA_TYPES},
    # INCLUDE list: if set, a message must contain one of these words
    # (checked in the file name, then caption/text). Case-insensitive.
    "keywords": [],
    # BLOCK list of file extensions, e.g. ["apk", "exe"]
    "extensions": [],
    # size filter in MB; size_mode: "gt" (only larger), "lt" (only smaller), None (off)
    "size_mb": 0,
    "size_mode": None,
    # skip files already sent by this task (by file_unique_id)
    "skip_duplicates": True,
    # transform
    "caption": None,        # template with {filename} {size} {caption}; None = keep original
    "forward_tag": False,   # True = forward with "Forwarded from" header
    "protect": False,
    "button": None,         # raw button text, parsed later
}


def _as_list(value):
    if not value:
        return []
    if isinstance(value, str):
        value = value.replace(",", " ").split()
    return [str(v).strip().lower().lstrip(".") for v in value if str(v).strip()]


def build_task_filters(configs: Optional[dict] = None, overrides: Optional[dict] = None) -> dict:
    """Snapshot the user's legacy /settings configs into a per-task filter dict."""
    f = copy.deepcopy(DEFAULT_TASK_FILTERS)
    c = configs or {}
    legacy_types = c.get("filters") or {}
    for t in MEDIA_TYPES:
        if t in legacy_types:
            f["types"][t] = bool(legacy_types[t])
    f["keywords"] = _as_list(c.get("keywords"))
    f["extensions"] = _as_list(c.get("extension"))
    try:
        f["size_mb"] = float(c.get("file_size") or 0)
    except (TypeError, ValueError):
        f["size_mb"] = 0
    limit = c.get("size_limit")
    f["size_mode"] = None if (limit is None or not f["size_mb"]) else ("gt" if limit else "lt")
    if "duplicate" in c:
        f["skip_duplicates"] = bool(c.get("duplicate"))
    f["caption"] = c.get("caption") or None
    f["forward_tag"] = bool(c.get("forward_tag"))
    f["protect"] = bool(c.get("protect"))
    f["button"] = c.get("button") or None
    if overrides:
        merge_filters(f, overrides)
    return f


def merge_filters(base: dict, overrides: dict) -> dict:
    """Shallow-merge overrides into base (types merged key by key). Returns base."""
    for k, v in (overrides or {}).items():
        if k == "types" and isinstance(v, dict):
            base.setdefault("types", {}).update({t: bool(x) for t, x in v.items() if t in MEDIA_TYPES})
        elif k in ("keywords", "extensions"):
            base[k] = _as_list(v)
        elif k in DEFAULT_TASK_FILTERS:
            base[k] = v
    return base


def normalize(task_filters: Optional[dict]) -> dict:
    """Fill missing keys so old stored tasks keep working."""
    f = copy.deepcopy(DEFAULT_TASK_FILTERS)
    if task_filters:
        merge_filters(f, task_filters)
    return f


# ── message inspection ───────────────────────────────────────────────────────

def message_type(msg) -> Optional[str]:
    if getattr(msg, "poll", None):
        return "poll"
    for t in ("photo", "video", "document", "audio", "voice", "animation", "sticker"):
        if getattr(msg, t, None):
            return t
    if getattr(msg, "text", None):
        return "text"
    return None


def _media_obj(msg):
    t = message_type(msg)
    if t in (None, "text", "poll"):
        return None
    return getattr(msg, t, None)


def _file_name(msg) -> str:
    m = _media_obj(msg)
    return (getattr(m, "file_name", None) or "") if m else ""


def _file_size(msg) -> int:
    m = _media_obj(msg)
    return int(getattr(m, "file_size", 0) or 0) if m else 0


def _plain_text(msg) -> str:
    return str(getattr(msg, "caption", None) or getattr(msg, "text", None) or "")


def dedupe_key(msg) -> Optional[str]:
    m = _media_obj(msg)
    if m is not None:
        return getattr(m, "file_unique_id", None)
    return None


def should_forward(msg, task_filters: dict, seen: Optional[set] = None) -> Tuple[bool, str]:
    """
    Decide whether this task forwards `msg`.
    Returns (True, "ok") or (False, reason) where reason is one of:
    empty, type, keyword, extension, size, duplicate.
    `seen` is a per-task set of file_unique_ids; it is updated when True is returned.
    """
    if msg is None or getattr(msg, "empty", False) or getattr(msg, "service", False):
        return False, "empty"
    f = task_filters
    t = message_type(msg)
    if t is None:
        return False, "empty"
    if not f["types"].get(t, True):
        return False, "type"

    if f["keywords"]:
        hay = (_file_name(msg) + " " + _plain_text(msg)).lower()
        if not any(k in hay for k in f["keywords"]):
            return False, "keyword"

    name = _file_name(msg)
    if f["extensions"] and name and "." in name:
        if name.rsplit(".", 1)[-1].lower() in f["extensions"]:
            return False, "extension"

    if f["size_mode"] and f["size_mb"] and _media_obj(msg) is not None:
        size_mb = _file_size(msg) / (1024 * 1024)
        if f["size_mode"] == "gt" and size_mb <= f["size_mb"]:
            return False, "size"
        if f["size_mode"] == "lt" and size_mb >= f["size_mb"]:
            return False, "size"

    if f["skip_duplicates"] and seen is not None:
        key = dedupe_key(msg)
        if key:
            if key in seen:
                return False, "duplicate"
            seen.add(key)
    return True, "ok"


# ── transform ────────────────────────────────────────────────────────────────

def human_size(size) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(size or 0)
    i = 0
    while size >= 1024.0 and i < len(units) - 1:
        size /= 1024.0
        i += 1
    return "%.2f %s" % (size, units[i])


def render_caption(msg, task_filters: dict) -> Optional[str]:
    """
    Safe caption templating: only {filename}, {size}, {caption} are replaced
    (plain str.replace — no str.format, so user templates cannot touch objects).
    Returns None to keep the original caption.
    """
    template = task_filters.get("caption")
    if not template or _media_obj(msg) is None:
        return None
    original = getattr(msg, "caption", None)
    if original is not None:
        original = getattr(original, "html", None) or str(original)
    else:
        original = ""
    out = template
    out = out.replace("{filename}", html.escape(_file_name(msg)))
    out = out.replace("{size}", human_size(_file_size(msg)))
    out = out.replace("{caption}", original)
    return out


def summarize_filters(task_filters: dict) -> str:
    f = normalize(task_filters)
    off = [t for t, on in f["types"].items() if not on]
    parts = []
    parts.append("types: all" if not off else "skip: " + ", ".join(off))
    if f["keywords"]:
        parts.append("keywords: " + ", ".join(f["keywords"]))
    if f["extensions"]:
        parts.append("block ext: " + ", ".join(f["extensions"]))
    if f["size_mode"]:
        parts.append(("size > " if f["size_mode"] == "gt" else "size < ") + f"{f['size_mb']:g} MB")
    parts.append("dupes: skip" if f["skip_duplicates"] else "dupes: allow")
    if f["caption"]:
        parts.append("custom caption")
    if f["forward_tag"]:
        parts.append("forward tag")
    return " · ".join(parts)
