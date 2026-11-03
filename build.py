#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Rebuild this folder from GitHub's Primer packages on npm.

Downloads @primer/primitives and @primer/octicons, verifies their sha512, and
writes tokens.json, dark.css, light.css, theme.css, base16/, preview/, icons/
and the licences. Only CSS, SVG and text are read from the packages.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import re
import shutil
import sys
import tarfile
import urllib.request
from html import escape
from pathlib import Path

REGISTRY = "https://registry.npmjs.org"
HERE = Path(__file__).resolve().parent
THEMES = {"dark": "Dark default", "light": "Light default"}
DECLARATION = re.compile(r"--([\w-]+):\s*([^;]+);(?:\s*/\*\*\s*(.*?)\s*\*/)?")
REFERENCE = re.compile(r"var\(--([\w-]+)\)")

# base16 slot -> (dark token, light token). base00-07: background to foreground.
# base08-0F: red, orange, yellow, green, cyan, blue, purple, brown, mostly from
# the ANSI colours. Light ANSI yellow is brown, so yellow uses fgColor-attention.
BASE16 = {
    "base00": ("bgColor-default", "bgColor-default"),
    "base01": ("bgColor-muted", "bgColor-muted"),
    "base02": ("borderColor-default", "borderColor-default"),
    "base03": ("borderColor-emphasis", "borderColor-emphasis"),
    "base04": ("fgColor-muted", "fgColor-muted"),
    "base05": ("fgColor-default", "fgColor-default"),
    "base06": ("fgColor-default", "fgColor-default"),
    "base07": ("ansi-whiteBright", "fgColor-default"),
    "base08": ("ansi-red", "ansi-red"),
    "base09": ("prettylights-syntax-variable", "prettylights-syntax-variable"),
    "base0A": ("fgColor-attention", "fgColor-attention"),
    "base0B": ("ansi-green", "ansi-green"),
    "base0C": ("ansi-cyan", "ansi-cyan"),
    "base0D": ("ansi-blue", "ansi-blue"),
    "base0E": ("ansi-magenta", "ansi-magenta"),
    "base0F": ("display-brown-fgColor", "display-brown-fgColor"),
}


def fetch_package(name: str, version: str) -> tuple[str, tarfile.TarFile]:
    with urllib.request.urlopen(f"{REGISTRY}/{name}/{version}", timeout=30) as response:
        meta = json.load(response)
    algorithm, _, expected = meta["dist"]["integrity"].partition("-")
    if algorithm != "sha512":
        raise SystemExit(f"{name}@{version}: unsupported integrity algorithm {algorithm}")
    with urllib.request.urlopen(meta["dist"]["tarball"], timeout=60) as response:
        data = response.read()
    actual = base64.b64encode(hashlib.sha512(data).digest()).decode()
    if actual != expected:
        raise SystemExit(f"{name}@{version}: tarball does not match the registry's sha512")
    return meta["version"], tarfile.open(fileobj=io.BytesIO(data), mode="r:gz")


def read_text(archive: tarfile.TarFile, member: str) -> str:
    handle = archive.extractfile(member)
    if handle is None:
        raise SystemExit(f"{member} is not a regular file")
    return handle.read().decode("utf-8")


def theme_block(css: str) -> str:
    """The theme's declarations. The file repeats them under @media; check they match."""
    start = css.index("{") + 1
    end = css.index("}", start)
    media = css.find("@media")
    if media != -1:
        inner = css.index("{", css.index("{", media) + 1) + 1
        auto = {m[0]: m[1] for m in DECLARATION.findall(css[inner : css.index("}", inner)])}
        main = {m[0]: m[1] for m in DECLARATION.findall(css[start:end])}
        if auto != main:
            raise SystemExit("prefers-color-scheme block differs from the main theme block")
    return css[start:end]


def resolve(tokens: dict[str, str], fallback: dict[str, str]) -> dict[str, str]:
    def value(name: str, seen: tuple[str, ...]) -> str:
        if name in seen:
            raise SystemExit(f"reference cycle: {' -> '.join(seen + (name,))}")
        raw = tokens.get(name, fallback.get(name))
        if raw is None:
            raise SystemExit(f"unresolved token --{name}")
        return REFERENCE.sub(lambda m: value(m.group(1), seen + (name,)), raw.strip())

    return {name: value(name, ()) for name in sorted(tokens)}


def css_block(selector: str, scheme: str, tokens: dict[str, str], notes: dict[str, str]) -> str:
    lines = [f"{selector} {{", f"  color-scheme: {scheme};"]
    for name, val in tokens.items():
        note = f" /* {notes[name]} */" if notes.get(name) else ""
        lines.append(f"  --{name}: {val};{note}")
    lines.append("}")
    return "\n".join(lines)


def indent(text: str) -> str:
    return "\n".join(f"  {line}" if line else line for line in text.splitlines())


def luminance(color: str) -> float:
    channels = [int(color[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def base16_palette(theme: str, tokens: dict[str, str]) -> dict[str, tuple[str, str]]:
    """slot -> (token, colour), checked to be opaque and correctly ordered."""
    column = 0 if theme == "dark" else 1
    palette = {slot: (pair[column], tokens[pair[column]]) for slot, pair in BASE16.items()}
    for slot, (token, color) in palette.items():
        if not re.fullmatch(r"#[0-9a-f]{6}", color):
            raise SystemExit(f"base16 {slot}: --{token} is {color}, not an opaque #rrggbb colour")
    ramp = [luminance(palette[f"base0{i}"][1]) for i in range(8)]
    ordered = sorted(ramp) if theme == "dark" else sorted(ramp, reverse=True)
    if ramp != ordered:
        raise SystemExit(f"base16 {theme}: base00-07 do not run from background to foreground")
    return palette


def base16_scheme(theme: str, tokens: dict[str, str]) -> str:
    palette = base16_palette(theme, tokens)
    lines = [
        'system: "base16"',
        f'name: "GitHub {THEMES[theme]}"',
        'author: "GitHub Primer (https://primer.style)"',
        f'variant: "{theme}"',
        "palette:",
    ]
    lines += [f'  {slot}: "{color}" # {token}' for slot, (token, color) in palette.items()]
    return "\n".join(lines) + "\n"


# Colours shown in the preview images.
PREVIEW_TOKENS = (
    "bgColor-inset", "bgColor-default", "bgColor-muted", "borderColor-default",
    "borderColor-emphasis", "fgColor-muted", "fgColor-default", "fgColor-accent",
    "bgColor-accent-emphasis", "fgColor-success", "bgColor-success-emphasis",
    "fgColor-attention", "fgColor-severe", "fgColor-danger", "fgColor-done",
    "fgColor-sponsors",
)
SANS = "-apple-system, BlinkMacSystemFont, 'Segoe UI', 'Noto Sans', Helvetica, Arial, sans-serif"
MONO = "ui-monospace, SFMono-Regular, 'SF Mono', Menlo, Consolas, 'Liberation Mono', monospace"
WIDTH = 846


def contrast(a: str, b: str) -> float:
    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def chip(x: float, y: float, w: float, h: float, color: str) -> str:
    """A paint-chip card: colour on top, a white strip with the hex code below."""
    block, r = h - 30, 4
    top = (
        f"M{x:.1f} {y + block:.1f}V{y + r}A{r} {r} 0 0 1 {x + r:.1f} {y}"
        f"H{x + w - r:.1f}A{r} {r} 0 0 1 {x + w:.1f} {y + r}V{y + block:.1f}Z"
    )
    return (
        f'<rect x="{x:.1f}" y="{y}" width="{w:.1f}" height="{h}" rx="{r}" class="paper" />'
        f'<path d="{top}" fill="{color}" />'
        f'<line x1="{x:.1f}" y1="{y + block:.1f}" x2="{x + w:.1f}" y2="{y + block:.1f}" class="edge" />'
        f'<text x="{x + 8:.1f}" y="{y + block + 19:.1f}" class="hex">{color.upper()}</text>'
    )


def preview_svg(theme: str, tokens: dict[str, str]) -> str:
    """Primer and base16 colours as paint-chip cards, two rows of eight each."""
    pad, gap, columns, card_h = 20, 10, 8, 128
    card_w = (WIDTH - 2 * pad - (columns - 1) * gap) / columns
    fg, bg, border = tokens["fgColor-default"], tokens["bgColor-default"], tokens["borderColor-default"]
    rows = [
        ("Primer", [tokens[name] for name in PREVIEW_TOKENS]),
        ("base16", [color for _, color in base16_palette(theme, tokens).values()]),
    ]
    parts = [f'<text class="title" x="{pad + 4}" y="{pad + 24}">GitHub {escape(THEMES[theme])}</text>']
    y = pad + 24
    for heading, chips in rows:
        parts.append(f'<text class="heading" x="{pad + 4}" y="{y + 34}">{heading}</text>')
        top = y + 46
        for index, color in enumerate(chips):
            row, column = divmod(index, columns)
            parts.append(chip(pad + column * (card_w + gap), top + row * (card_h + gap), card_w, card_h, color))
        y = top + 2 * card_h + gap
    height = y + pad
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{height}" viewBox="0 0 {WIDTH} {height}" role="img" aria-label="GitHub {escape(THEMES[theme])} palette">
<defs><filter id="lift" x="-10%" y="-10%" width="120%" height="125%"><feDropShadow dx="0" dy="1" stdDeviation="1.5" flood-color="#1f2328" flood-opacity="0.18" /></filter></defs>
<style>
  text {{ font-family: {SANS}; }}
  .title {{ font-size: 20px; font-weight: 600; fill: {fg}; }}
  .heading {{ font-size: 14px; font-weight: 600; fill: {tokens["fgColor-muted"]}; }}
  .paper {{ fill: #ffffff; stroke: #1f23281f; filter: url(#lift); }}
  .edge {{ stroke: #1f232814; }}
  .hex {{ font-size: 11px; font-weight: 700; fill: #1f2328; letter-spacing: 0.02em; }}
</style>
<rect x="0.5" y="0.5" width="{WIDTH - 1}" height="{height - 1}" rx="6" fill="{bg}" stroke="{border}" />
{chr(10).join(parts)}
</svg>
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--primitives", default="11.10.0", help="@primer/primitives version")
    parser.add_argument("--octicons", default="19.42.0", help="@primer/octicons version")
    parser.add_argument("--out", type=Path, default=HERE, help="output directory (default: this folder)")
    args = parser.parse_args(argv)

    primitives_version, primitives = fetch_package("@primer/primitives", args.primitives)
    octicons_version, octicons = fetch_package("@primer/octicons", args.octicons)

    # Size tokens that a few colour tokens reference.
    fallback: dict[str, str] = {}
    for member in primitives.getnames():
        if re.match(r"package/dist/css/(base|functional)/size/[\w-]+\.css$", member):
            fallback.update({m[0]: m[1] for m in DECLARATION.findall(read_text(primitives, member))})

    resolved: dict[str, dict[str, str]] = {}
    notes: dict[str, str] = {}
    for theme in THEMES:
        css = read_text(primitives, f"package/dist/css/functional/themes/{theme}.css")
        found = DECLARATION.findall(theme_block(css))
        resolved[theme] = resolve({m[0]: m[1] for m in found}, fallback)
        notes.update({m[0]: m[2] for m in found if m[2]})
    if resolved["dark"].keys() != resolved["light"].keys():
        raise SystemExit("dark and light themes define different tokens")

    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    source = {
        "package": "@primer/primitives",
        "version": primitives_version,
        "files": {theme: f"dist/css/functional/themes/{theme}.css" for theme in THEMES},
        "names": THEMES,
        "license": "MIT, see LICENSE-primer-primitives",
    }
    (out / "tokens.json").write_text(
        json.dumps({"source": source, **resolved}, indent=2) + "\n", encoding="utf-8"
    )

    header = (
        f"/* GitHub {{name}}, from @primer/primitives {primitives_version} (MIT).\n"
        "   Generated by build.py. */\n"
    )
    for theme, name in THEMES.items():
        (out / f"{theme}.css").write_text(
            header.format(name=f'"{name}"') + css_block(":root", theme, resolved[theme], notes) + "\n",
            encoding="utf-8",
        )
    switching = "\n\n".join(
        [
            header.format(name='"Light default" and "Dark default"'),
            (
                "/* Light by default, dark if the system prefers it. data-theme=\"light\"\n"
                "   or \"dark\" pins an element and its children. */"
            ),
            css_block(':root,\n[data-theme="light"]', "light", resolved["light"], {}),
            "@media (prefers-color-scheme: dark) {\n"
            + indent(css_block(':root:not([data-theme="light"])', "dark", resolved["dark"], {}))
            + "\n}",
            css_block('[data-theme="dark"]', "dark", resolved["dark"], {}),
        ]
    )
    (out / "theme.css").write_text(switching + "\n", encoding="utf-8")

    schemes = out / "base16"
    schemes.mkdir(exist_ok=True)
    for theme in THEMES:
        (schemes / f"github-{theme}-default.yaml").write_text(
            base16_scheme(theme, resolved[theme]), encoding="utf-8"
        )

    previews = out / "preview"
    previews.mkdir(exist_ok=True)
    for theme in THEMES:
        (previews / f"{theme}.svg").write_text(preview_svg(theme, resolved[theme]), encoding="utf-8")

    icons = out / "icons"
    if icons.exists():
        shutil.rmtree(icons)
    icons.mkdir()
    count = 0
    for member in octicons.getmembers():
        match = re.fullmatch(r"package/build/svg/([\w-]+\.svg)", member.name)
        if not (member.isfile() and match):
            continue
        svg = read_text(octicons, member.name)
        if re.search(r"<script|\son\w+=|href=", svg, re.IGNORECASE):
            raise SystemExit(f"{match.group(1)} contains script or links")
        (icons / match.group(1)).write_text(svg, encoding="utf-8")
        count += 1

    (out / "LICENSE-primer-primitives").write_text(read_text(primitives, "package/LICENSE"), encoding="utf-8")
    (out / "LICENSE-primer-octicons").write_text(read_text(octicons, "package/LICENSE"), encoding="utf-8")

    print(
        f"@primer/primitives {primitives_version}: {len(resolved['dark'])} tokens per theme; "
        f"@primer/octicons {octicons_version}: {count} icons -> {out}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
