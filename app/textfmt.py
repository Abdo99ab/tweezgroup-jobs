"""Turn a pasted job description back into organized sections.

Recruiters paste LinkedIn-style ads: an intro, then emoji-marked headings
("📝 What You'll Own", "🎯 Ideal Candidate"…) each followed by short bullet
lines. HTML collapses the line breaks, which is why the public page showed one
wall of text. This module parses the stored text and renders proper structure:

  <div class="jd">
    <div class="jd-intro"><p>…</p></div>
    <section class="jd-sec"><h3>📝 What You'll Own</h3><ul><li>…</li></ul></section>
    …
  </div>

Everything is escaped — no HTML from the description ever reaches the page raw.
If a description has no recognizable structure it degrades gracefully to
paragraphs, so plain descriptions look exactly as before.
"""
import re

from markupsafe import Markup, escape

EMOJI = re.compile(r"[☀-➿⬀-⯿️\U0001F000-\U0001FAFF]")
LEAD_EMOJI = re.compile(r"^\s*(?:[☀-➿⬀-⯿️\U0001F000-\U0001FAFF]\s*)+")
BULLET = re.compile(r"^\s*[-•*▪◦·–]\s+")


def _is_heading(line):
    stripped = LEAD_EMOJI.sub("", line).strip(" :")
    if not stripped or len(stripped) > 70:
        return False
    if LEAD_EMOJI.match(line):
        return True
    if line.rstrip().endswith(":"):
        return True
    letters = [c for c in stripped if c.isalpha()]
    if letters and sum(c.isupper() for c in letters) / len(letters) > 0.8 and len(stripped.split()) <= 8:
        return True  # ALL-CAPS short line
    return False


def _parse(text):
    """[('intro', [lines]) or ('sec', heading, [lines])] from the raw description."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if "\n" not in text.strip() and len(EMOJI.findall(text)) >= 3:
        # single-line paste: every emoji starts a new line so headings can be found
        text = EMOJI.sub(lambda m: "\n" + m.group(0), text)
    lines = [l.strip() for l in text.split("\n")]
    out, current = [], ("intro", None, [])
    for line in lines:
        if not line:
            current[2].append("")  # paragraph break
            continue
        if _is_heading(line):
            out.append(current)
            current = ("sec", line.rstrip(":"), [])
        else:
            current[2].append(line)
    out.append(current)
    return [b for b in out if b[1] is not None or any(l for l in b[2])]


def _body_html(body_lines):
    """Bullets when the lines look like a list, paragraphs otherwise."""
    items = [l for l in body_lines if l]
    if not items:
        return ""
    explicit = sum(1 for l in items if BULLET.match(l))
    listish = len(items) >= 2 and (explicit >= 2 or sum(1 for l in items if len(l) <= 110) / len(items) >= 0.8)
    if listish:
        lis = "".join(f"<li>{escape(BULLET.sub('', l))}</li>" for l in items)
        return f"<ul>{lis}</ul>"
    paras, cur = [], []
    for l in body_lines + [""]:
        if l:
            cur.append(l)
        elif cur:
            paras.append("<p>" + " ".join(str(escape(x)) for x in cur) + "</p>")
            cur = []
    return "".join(paras)


def description_html(text, part="all"):
    """Jinja filter: organized, fully escaped HTML for a role description.
    part: "all" (default), "intro" (text before the first heading), or
    "sections" (everything from the first heading on) — the jobs listing shows
    the intro and folds the sections behind a 'full description' toggle."""
    if not text or not text.strip():
        return ""
    blocks = _parse(text)
    # A leading "🚀 WE'RE HIRING …" banner duplicates the role title: use its text as the
    # intro paragraph instead of keeping it as a section.
    if blocks and blocks[0][0] == "sec" and (len(blocks) == 1 or blocks[1][0] == "sec"):
        _k, _h, body0 = blocks[0]
        blocks = [("intro", None, body0)] + blocks[1:]
    intro, secs = [], []
    for kind, heading, body in blocks:
        if kind == "intro":
            intro.append(f'<div class="jd-intro">{_body_html(body)}</div>')
        else:
            secs.append(f'<section class="jd-sec"><h3>{escape(heading)}</h3>{_body_html(body)}</section>')
    parts = []
    if part != "sections":
        parts += intro
    if part != "intro" and secs:
        parts.append(f'<div class="jd-sections">{"".join(secs)}</div>')
    if not parts:
        return ""
    return Markup(f'<div class="jd">{"".join(parts)}</div>')


def has_sections(text):
    return bool(text) and any(k == "sec" for k, _h, _b in _parse(text))
