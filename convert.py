#!/usr/bin/env python3
"""
Convert markdown notes into a minimal e-reader-friendly static site.

Usage:
    python convert.py

Reads files from ./notes/ and writes the site to ./docs/.
Folder names inside notes/ become categories on the index page.
"""

import os
import shutil
from pathlib import Path

import markdown

NOTES_DIR = Path("notes")
OUTPUT_DIR = Path("docs")

PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<link rel="stylesheet" href="{css_path}">
</head>
<body>
<p><a href="{home_path}">&larr; Home</a></p>
<hr>
<h1>{title}</h1>
{content}
</body>
</html>
"""

INDEX_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Notes</title>
<link rel="stylesheet" href="style.css">
</head>
<body>
<h1>Notes</h1>
{content}
</body>
</html>
"""

# Deliberately plain CSS. No web fonts, no flex/grid tricks, no JS.
# High contrast, generous line-height, serif body for reading.
CSS = """body {
  font-family: Georgia, "Times New Roman", serif;
  max-width: 40em;
  margin: 2em auto;
  padding: 0 1em;
  color: #000;
  background: #fff;
  font-size: 18px;
  line-height: 1.55;
}
h1, h2, h3, h4 {
  font-family: Helvetica, Arial, sans-serif;
  line-height: 1.25;
}
h1 { font-size: 1.6em; }
h2 { font-size: 1.3em; margin-top: 1.5em; }
h3 { font-size: 1.1em; }
a { color: #000; }
hr { border: 0; border-top: 1px solid #999; margin: 1em 0; }
code {
  font-family: "Courier New", monospace;
  background: #eee;
  padding: 0.1em 0.3em;
}
pre {
  font-family: "Courier New", monospace;
  background: #eee;
  padding: 0.6em 0.8em;
  overflow-x: auto;
  font-size: 0.9em;
}
pre code { background: transparent; padding: 0; }
blockquote {
  border-left: 3px solid #999;
  margin-left: 0;
  padding-left: 1em;
  color: #333;
}
table { border-collapse: collapse; }
th, td { border: 1px solid #999; padding: 0.3em 0.6em; }
img { max-width: 100%; }
ul.notes-list { list-style: none; padding-left: 0; }
ul.notes-list li { margin: 0.4em 0; font-size: 1.05em; }
"""


def title_from_filename(name: str) -> str:
    """Turn 'my-note_v2' into 'my note v2'."""
    return name.replace("-", " ").replace("_", " ").strip()


def title_from_markdown(md_text: str, fallback: str) -> str:
    """Prefer the first H1 in the file if there is one; else fall back."""
    for line in md_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
        if stripped:
            # First non-empty line isn't a heading -> just use fallback
            break
    return fallback


def relative_prefix(out_path: Path) -> str:
    """How many '../' do we need to climb back to docs/ from this file?"""
    depth = len(out_path.relative_to(OUTPUT_DIR).parts) - 1
    return "../" * depth


def main() -> None:
    if not NOTES_DIR.exists():
        raise SystemExit(
            f"Couldn't find {NOTES_DIR}/ folder. Put your .md files in there "
            "(optionally inside category subfolders) and run again."
        )

    if OUTPUT_DIR.exists():
        shutil.rmtree(OUTPUT_DIR)
    OUTPUT_DIR.mkdir()

    (OUTPUT_DIR / "style.css").write_text(CSS, encoding="utf-8")

    md = markdown.Markdown(extensions=["fenced_code", "tables", "sane_lists"])

    categories: dict[str, list[tuple[str, str]]] = {}

    md_files = sorted(NOTES_DIR.rglob("*.md"))
    if not md_files:
        print(f"No .md files found under {NOTES_DIR}/. Nothing to build.")

    for md_path in md_files:
        rel = md_path.relative_to(NOTES_DIR)
        category = rel.parts[0] if len(rel.parts) > 1 else "Uncategorized"

        raw = md_path.read_text(encoding="utf-8")
        title = title_from_markdown(raw, title_from_filename(rel.stem))

        md.reset()
        body_html = md.convert(raw)

        # If the file starts with an H1 matching the title, strip it so we
        # don't render the title twice (the template already adds an H1).
        body_html = _strip_leading_h1(body_html, title)

        out_path = OUTPUT_DIR / rel.with_suffix(".html")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        prefix = relative_prefix(out_path)

        page = PAGE_TEMPLATE.format(
            title=_escape(title),
            content=body_html,
            css_path=prefix + "style.css",
            home_path=prefix + "index.html",
        )
        out_path.write_text(page, encoding="utf-8")

        link = str(out_path.relative_to(OUTPUT_DIR)).replace(os.sep, "/")
        categories.setdefault(category, []).append((title, link))

    sections = []
    for category in sorted(categories.keys(), key=str.lower):
        items = sorted(categories[category], key=lambda t: t[0].lower())
        lis = "\n".join(
            f'    <li><a href="{link}">{_escape(title)}</a></li>'
            for title, link in items
        )
        sections.append(
            f"<h2>{_escape(category)}</h2>\n"
            f'  <ul class="notes-list">\n{lis}\n  </ul>'
        )

    index_html = INDEX_TEMPLATE.format(
        content="\n".join(sections) if sections else "<p>No notes yet.</p>"
    )
    (OUTPUT_DIR / "index.html").write_text(index_html, encoding="utf-8")

    total = sum(len(v) for v in categories.values())
    print(f"Built {total} page(s) across {len(categories)} categor(ies) -> {OUTPUT_DIR}/")


def _escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
    )


def _strip_leading_h1(html: str, title: str) -> str:
    """Remove a leading <h1>...</h1> from generated body if it matches the title."""
    html_lstripped = html.lstrip()
    if html_lstripped.startswith("<h1"):
        end = html_lstripped.find("</h1>")
        if end != -1:
            inner = html_lstripped[html_lstripped.find(">") + 1:end]
            if inner.strip().lower() == title.strip().lower():
                return html_lstripped[end + len("</h1>"):].lstrip()
    return html


if __name__ == "__main__":
    main()
