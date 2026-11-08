"""Parsers for Ropewiki (MediaWiki) wikitext.

Pure functions, no network or geo dependencies, so they can be tested with fixtures.
Values are returned as written on the wiki; unit conversion lives in the converter.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass

_HEADING = re.compile(r"^==+\s*(?P<title>[^=].*?)\s*==+\s*$", re.MULTILINE)
_EXT_LINK = re.compile(r"\[(?P<url>https?://[^\s\]]+)(?:\s+(?P<label>[^\]]*))?\]")
_RAPPEL_LINE = re.compile(
    r"^'''\s*(?P<label>R\d+[a-z]?|rap\.?\s*\d+[a-z]?)\s*'''\s*:?\s*(?P<text>.*)$",
    re.IGNORECASE | re.MULTILINE,
)
_FEET = re.compile(r"(\d+(?:\.\d+)?)\s*(?:ft\b|feet\b|')", re.IGNORECASE)
_METERS = re.compile(r"(\d+(?:\.\d+)?)\s*m\b", re.IGNORECASE)
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_REF = re.compile(r"<ref\b[^>]*?(?:/>|>.*?</ref>)", re.DOTALL | re.IGNORECASE)
_FILE_LINK = re.compile(r"\[\[(?:File|Image):[^\[\]]*\]\]", re.IGNORECASE)
_INNER_TEMPLATE = re.compile(r"\{\{[^{}]*\}\}")
_WIKILINK = re.compile(r"\[\[([^\[\]|]*)(?:\|([^\[\]]*))?\]\]")
_TAG = re.compile(r"</?[a-zA-Z][^>]*>")
_QUOTES = re.compile(r"'{2,}")
_TABLE_LINE = re.compile(r"^(?:\{\||\|\}|\|-).*$", re.MULTILINE)
_LIST_MARK = re.compile(r"^[*#:;]+\s*", re.MULTILINE)


@dataclass(frozen=True)
class Rappel:
    label: str
    height_m: float | None
    text: str


def find_template(text: str, name: str) -> dict[str, str] | None:
    """Return the parameters of the first ``{{name ...}}`` call, or None.

    Nested ``{{...}}`` and ``[[...]]`` may contain ``|`` and ``=``; only top-level
    separators split parameters. Keys are stripped; the first occurrence of a
    duplicate key wins, matching MediaWiki's behaviour.
    """
    match = re.search(r"\{\{\s*" + re.escape(name) + r"\s*(?=[|}\n])", text, re.IGNORECASE)
    if match is None:
        return None
    body = _template_body(text, match.end())
    if body is None:
        return None
    params: dict[str, str] = {}
    for part in _split_top_level(body):
        if "=" not in part:
            continue
        key, _, value = part.partition("=")
        key = key.strip()
        if key and key not in params:
            params[key] = value.strip()
    return params


def _template_body(text: str, start: int) -> str | None:
    depth = 0
    j = start
    while j < len(text):
        if text.startswith("{{", j):
            depth += 1
            j += 2
        elif text.startswith("}}", j):
            if depth == 0:
                return text[start:j]
            depth -= 1
            j += 2
        else:
            j += 1
    return None


def _split_top_level(body: str) -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    brace = bracket = 0
    i = 0
    while i < len(body):
        two = body[i : i + 2]
        if two == "{{":
            brace += 1
            current.append(two)
            i += 2
            continue
        if two == "}}":
            brace -= 1
            current.append(two)
            i += 2
            continue
        if two == "[[":
            bracket += 1
            current.append(two)
            i += 2
            continue
        if two == "]]":
            bracket -= 1
            current.append(two)
            i += 2
            continue
        ch = body[i]
        if ch == "|" and brace == 0 and bracket == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
        i += 1
    parts.append("".join(current))
    return parts


def strip_template(text: str, name: str) -> str:
    """Remove the first ``{{name ...}}`` call from ``text``."""
    match = re.search(r"\{\{\s*" + re.escape(name) + r"\s*(?=[|}\n])", text, re.IGNORECASE)
    if match is None:
        return text
    body = _template_body(text, match.end())
    if body is None:
        return text
    end = match.end() + len(body) + 2
    return text[: match.start()] + text[end:]


def split_sections(text: str) -> list[tuple[str, str]]:
    """Split page text into ``(heading, body)`` pairs. Lead text uses heading ``Lead``."""
    sections: list[tuple[str, str]] = []
    matches = list(_HEADING.finditer(text))
    lead = text[: matches[0].start()].strip() if matches else text.strip()
    if lead:
        sections.append(("Lead", lead))
    for idx, m in enumerate(matches):
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        body = text[m.end() : end].strip()
        if body:
            sections.append((m.group("title").strip(), body))
    return sections


def external_links(text: str) -> list[tuple[str, str]]:
    """``(url, label)`` for each bracketed external link. Label may be empty."""
    return [(m.group("url"), (m.group("label") or "").strip()) for m in _EXT_LINK.finditer(text)]


def height_m(text: str) -> float | None:
    """First height in ``text`` as meters. Feet are converted; plain meters are kept."""
    m = _FEET.search(text)
    if m:
        return round(float(m.group(1)) * 0.3048, 1)
    m = _METERS.search(text)
    if m:
        return float(m.group(1))
    return None


def rappels(section_body: str) -> list[Rappel]:
    """Rappel lines such as ``'''R3''': 85ft Option 1: ...`` (labels R1, R2a, rap 3)."""
    out: list[Rappel] = []
    for m in _RAPPEL_LINE.finditer(section_body):
        label = normalize_label(m.group("label"))
        text = m.group("text").strip()
        out.append(Rappel(label=label, height_m=height_m(text), text=text))
    return out


def first_number(text: str | None) -> float | None:
    if not text:
        return None
    m = _NUMBER.search(text)
    return float(m.group(0)) if m else None


def first_int(text: str | None) -> int | None:
    value = first_number(text)
    return int(value) if value is not None else None


_LABEL_TOKEN = r"(?:R\d+[a-z]?|[Rr]ap\.?\s*\d+[a-z]?)"
_LABELS_PREFIX = re.compile(
    rf"^\s*(?P<labels>{_LABEL_TOKEN}(?:\s*(?:&|and|,)\s*{_LABEL_TOKEN})*)\s*:\s*(?P<text>.*)$"
)
_CODED = re.compile(r"^\s*(?P<code>\d+)\s*-\s*(?P<label>[^()]+?)\s*(?:\((?P<note>.*)\))?\s*$")


def normalize_label(raw: str) -> str:
    """``rap 3`` / ``Rap.3`` / ``R3'`` -> ``R3``. Letter suffixes stay lower case."""
    text = raw.replace("'", "").strip()
    m = re.match(r"(?i)^(?:r|rap\.?)\s*(\d+)([a-z]?)$", text)
    if m:
        return f"R{m.group(1)}{m.group(2).lower()}"
    return re.sub(r"\s+", " ", text)


def parse_coded(value: str | None) -> tuple[int | None, str | None, str | None]:
    """``"2 - Thin wetsuit (3mm-4mm)"`` -> ``(2, "Thin wetsuit", "3mm-4mm")``."""
    if not value or not value.strip():
        return None, None, None
    m = _CODED.match(value)
    if m is None:
        return None, value.strip(), None
    return int(m.group("code")), m.group("label").strip(), (m.group("note") or None)


def parse_file_list(value: str | None) -> list[str]:
    """Comma-separated ``FileList`` value, empties dropped."""
    if not value:
        return []
    return [name.strip() for name in value.split(",") if name.strip()]


def parse_comments(wiki: str) -> tuple[str, list[tuple[list[str], str]]]:
    """Split a condition ``Comments`` value into general wikitext and per-rappel notes.

    A line starting with a rappel label (``R1:``, ``R4 & R5:``, ``rap 3:``) starts a note.
    Following non-blank lines continue it; a blank line ends it. Everything else is general.
    Returns ``(general_wiki, [(labels, note_wiki), ...])``.
    """
    general: list[str] = []
    notes: list[tuple[list[str], str]] = []
    current: tuple[list[str], list[str]] | None = None

    def flush() -> None:
        nonlocal current
        if current is not None:
            labels, lines = current
            notes.append((labels, "\n".join(lines).strip()))
            current = None

    for raw in wiki.splitlines():
        line = raw.strip()
        m = _LABELS_PREFIX.match(line)
        if m:
            flush()
            labels = [normalize_label(t) for t in re.findall(_LABEL_TOKEN, m.group("labels"))]
            current = (labels, [m.group("text").strip()])
        elif not line:
            flush()
            general.append("")
        elif current is not None:
            current[1].append(line)
        else:
            general.append(line)
    flush()
    return "\n".join(general).strip(), notes


def wiki_to_text(wiki: str) -> str:
    """Plain text from wikitext: drops templates, comments, refs, file links and markup.

    ``[[Target|label]]`` keeps the label, ``[http://x label]`` keeps the label (or the URL),
    list markers become ``- ``, and HTML entities are decoded. Paragraphs are kept.
    """
    text = _COMMENT.sub("", wiki)
    text = _REF.sub("", text)
    text = _FILE_LINK.sub("", text)
    for _ in range(20):  # templates can nest; strip innermost first until none remain
        new = _INNER_TEMPLATE.sub("", text)
        if new == text:
            break
        text = new
    text = _WIKILINK.sub(lambda m: m.group(2) if m.group(2) is not None else m.group(1), text)
    text = _EXT_LINK.sub(lambda m: (m.group("label") or m.group("url")).strip(), text)
    text = _TAG.sub("", text)
    text = _QUOTES.sub("", text)
    text = _TABLE_LINE.sub("", text)
    text = _HEADING.sub(lambda m: m.group("title"), text)
    text = _LIST_MARK.sub("- ", text)
    text = html.unescape(text).replace("\xa0", " ")
    text = "\n".join(line.rstrip() for line in text.splitlines())
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()
