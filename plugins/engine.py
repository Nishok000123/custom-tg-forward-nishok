"""
One forwarding engine shared by batch (/fwd), live (/live) and schedules.

Every forward ("task") carries its OWN filter snapshot (see build_task_filters).
The user's global /settings are only the defaults copied in when a task is created,
so changing /settings later never changes a forward that is already running.

Public API:
    build_task_filters(configs, overrides=None) -> dict
    should_forward(msg, task_filters, seen=None) -> (bool, reason)
    build_output(msg, task_filters) -> Optional[str]   (final caption / text HTML)
    render_caption(msg, task_filters) -> Optional[str]
    preview_text(task_filters) -> str                  (sample output for the UI)
    summarize_filters(task_filters) -> str             (one-line human summary)
"""
import copy
import html
import re
from typing import Optional, Tuple

MEDIA_TYPES = (
    "text", "photo", "video", "document", "audio",
    "voice", "animation", "sticker", "poll",
)

MAX_RULES = 20

DEFAULT_TASK_FILTERS = {
    # message types: True = forward, False = skip
    "types": {t: True for t in MEDIA_TYPES},
    # INCLUDE list: if set, a message must contain one of these words
    # (checked in the file name, then caption/text). Case-insensitive.
    "keywords": [],
    # BLOCK list: skip a message if any of these words/phrases appear.
    "blocked_words": [],
    # BLOCK list of file extensions, e.g. ["apk", "exe"]
    "extensions": [],
    # size filter in MB; size_mode: "gt" (only larger), "lt" (only smaller), None (off)
    "size_mb": 0,
    "size_mode": None,
    # skip files already sent by this task (by file_unique_id)
    "skip_duplicates": True,
    # transform
    "caption": None,        # template with {filename} {size} {caption}; None = keep original
    "replace": [],          # [[find, replace], ...] on caption/text, case-insensitive
    "strip_links": False,   # remove URLs / t.me links
    "strip_mentions": False,  # remove @usernames
    "header": None,         # HTML added above caption/text ({filename} {size} allowed)
    "footer": None,         # HTML added below
    "forward_tag": False,   # True = forward with "Forwarded from" header (no text changes)
    "protect": False,
    "button": None,         # raw button text, parsed later
}


def _as_list(value):
    """Single words: split on commas/spaces, lowercase, strip leading dots."""
    if not value:
        return []
    if isinstance(value, str):
        value = value.replace(",", " ").split()
    return [str(v).strip().lower().lstrip(".") for v in value if str(v).strip()]


def _as_phrases(value):
    """Phrases: split on commas/new lines only, so 'promo code' stays one item."""
    if not value:
        return []
    if isinstance(value, str):
        value = re.split(r"[,\n]", value)
    out = []
    for v in value:
        v = str(v).strip().lower()
        if v and v not in out:
            out.append(v)
    return out[:50]


def _as_rules(value):
    rules = []
    for r in value or []:
        if isinstance(r, (list, tuple)) and len(r) == 2 and str(r[0]).strip():
            rules.append([str(r[0]), str(r[1] or "")])
    return rules[:MAX_RULES]


def parse_rules(text: str) -> list:
    """'old => new' per line ('old =>' removes the word). Lines without => are ignored."""
    rules = []
    for line in str(text or "").splitlines():
        sep = "=>" if "=>" in line else ("->" if "->" in line else None)
        if not sep:
            continue
        old, new = line.split(sep, 1)
        old = old.strip()
        if old:
            rules.append([old, new.strip()])
    return rules[:MAX_RULES]


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
        elif k == "blocked_words":
            base[k] = _as_phrases(v)
        elif k == "replace":
            base[k] = _as_rules(v)
        elif k in ("strip_links", "strip_mentions", "skip_duplicates", "forward_tag", "protect"):
            base[k] = bool(v)
        elif k in ("header", "footer", "caption"):
            base[k] = str(v) if v else None
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


def _as_html(value) -> str:
    """pyrogram Str has .html; plain strings are escaped."""
    if value is None:
        return ""
    rich = getattr(value, "html", None)
    return rich if rich is not None else html.escape(str(value))


def dedupe_key(msg) -> Optional[str]:
    m = _media_obj(msg)
    if m is not None:
        return getattr(m, "file_unique_id", None)
    return None


def should_forward(msg, task_filters: dict, seen: Optional[set] = None) -> Tuple[bool, str]:
    """
    Decide whether this task forwards `msg`.
    Returns (True, "ok") or (False, reason) where reason is one of:
    empty, type, keyword, blocked, extension, size, duplicate.
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

    hay = None
    if f["keywords"] or f.get("blocked_words"):
        hay = (_file_name(msg) + " " + _plain_text(msg)).lower()
    if f["keywords"] and not any(k in hay for k in f["keywords"]):
        return False, "keyword"
    if f.get("blocked_words") and any(w in hay for w in f["blocked_words"]):
        return False, "blocked"

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

_URL_RE = re.compile(r"(?i)\b(?:https?://|www\.|(?:t|telegram)\.me/)[^\s<]+")
_ANCHOR_RE = re.compile(r"(?is)<a\s[^>]*>(.*?)</a>")
_MENTION_RE = re.compile(r"(?<![\w/@.])@[A-Za-z][A-Za-z0-9_]{3,31}\b")


def human_size(size) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(size or 0)
    i = 0
    while size >= 1024.0 and i < len(units) - 1:
        size /= 1024.0
        i += 1
    return "%.2f %s" % (size, units[i])


def _fill(template: str, filename: str, size: int, caption_html: Optional[str] = None) -> str:
    """Plain str.replace of the known tokens — never str.format (see audit H3)."""
    out = template.replace("{filename}", html.escape(filename or ""))
    out = out.replace("{size}", human_size(size) if size else "")
    if caption_html is not None:
        out = out.replace("{caption}", caption_html)
    return out


def text_tools_active(tf: dict) -> bool:
    return bool(
        tf.get("replace") or tf.get("strip_links") or tf.get("strip_mentions")
        or tf.get("header") or tf.get("footer")
    )


def apply_text_tools(body: str, tf: dict, filename: str = "", size: int = 0) -> str:
    """Find/replace, strip links/@mentions, then add header/footer. Works on HTML."""
    out = body or ""
    for old, new in tf.get("replace") or []:
        pat = re.compile(re.escape(html.escape(old, quote=False)), re.I)
        rep = html.escape(new, quote=False)
        out = pat.sub(lambda _m: rep, out)
    if tf.get("strip_links"):
        def _unwrap(m):
            label = m.group(1)
            return "" if (not label.strip() or _URL_RE.search(label)) else label
        out = _ANCHOR_RE.sub(_unwrap, out)
        out = _URL_RE.sub("", out)
    if tf.get("strip_mentions"):
        out = _MENTION_RE.sub("", out)
    if tf.get("strip_links") or tf.get("strip_mentions") or tf.get("replace"):
        out = re.sub(r"[ \t]{2,}", " ", out)
        out = re.sub(r"[ \t]+\n", "\n", out)
        out = re.sub(r"\n{3,}", "\n\n", out)
    out = out.strip()
    header = _fill(tf["header"], filename, size).strip() if tf.get("header") else ""
    footer = _fill(tf["footer"], filename, size).strip() if tf.get("footer") else ""
    return "\n\n".join(p for p in (header, out, footer) if p)


def render_caption(msg, task_filters: dict) -> Optional[str]:
    """
    Caption template only: {filename}, {size}, {caption} are replaced.
    Returns None to keep the original caption.
    """
    template = task_filters.get("caption")
    if not template or _media_obj(msg) is None:
        return None
    return _fill(template, _file_name(msg), _file_size(msg), _as_html(getattr(msg, "caption", None)))


def build_output(msg, task_filters: dict) -> Optional[str]:
    """
    Final caption (media) or text (text messages) after template + text tools.
    None = send the original unchanged. "" for a text message = nothing left to send.
    """
    tf = task_filters
    t = message_type(msg)
    is_media = _media_obj(msg) is not None
    if t != "text" and not is_media:
        return None
    has_template = is_media and bool(tf.get("caption"))
    if not has_template and not text_tools_active(tf):
        return None
    if has_template:
        body = render_caption(msg, tf) or ""
    elif is_media:
        body = _as_html(getattr(msg, "caption", None))
    else:
        body = _as_html(getattr(msg, "text", None))
    return apply_text_tools(body, tf, _file_name(msg), _file_size(msg))


SAMPLE_TEXT = "Episode 12 is out 🎬\nJoin @mychannel → https://t.me/mychannel"
SAMPLE_FILE = "Show.S01E12.1080p.mkv"
SAMPLE_SIZE = 734003200


def preview_text(task_filters: dict) -> str:
    """What a sample media caption turns into with these filters (HTML)."""
    tf = task_filters
    body = html.escape(SAMPLE_TEXT)
    if tf.get("caption"):
        body = _fill(tf["caption"], SAMPLE_FILE, SAMPLE_SIZE, body)
    out = apply_text_tools(body, tf, SAMPLE_FILE, SAMPLE_SIZE)
    return out or "<i>(empty caption)</i>"


def summarize_filters(task_filters: dict) -> str:
    f = normalize(task_filters)
    off = [t for t, on in f["types"].items() if not on]
    parts = []
    parts.append("types: all" if not off else "skip: " + ", ".join(off))
    if f["keywords"]:
        parts.append("include: " + ", ".join(f["keywords"]))
    if f["blocked_words"]:
        parts.append("block: " + ", ".join(f["blocked_words"]))
    if f["extensions"]:
        parts.append("block ext: " + ", ".join(f["extensions"]))
    if f["size_mode"]:
        parts.append(("size > " if f["size_mode"] == "gt" else "size < ") + f"{f['size_mb']:g} MB")
    parts.append("dupes: skip" if f["skip_duplicates"] else "dupes: allow")
    if f["caption"]:
        parts.append("custom caption")
    if f["replace"]:
        parts.append(f"{len(f['replace'])} replace rule(s)")
    if f["strip_links"]:
        parts.append("no links")
    if f["strip_mentions"]:
        parts.append("no @mentions")
    if f["header"]:
        parts.append("header")
    if f["footer"]:
        parts.append("footer")
    if f["forward_tag"]:
        parts.append("forward tag")
    if f["protect"]:
        parts.append("protected")
    return " · ".join(parts)
