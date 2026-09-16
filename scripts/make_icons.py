"""Build every icon the site serves from the two brand masters.

There was no script for this. The first set was made by hand in September
and the only record of how was a comment in index.html, so the second
rebrand started by working out what the first one had done. This exists so
the third one does not.

Two masters, both 512x512 with transparent corners, in brand/:

  mark-light.png   green ring on paper, circular. Every favicon size and
                   the .ico come from this one.
  mark-dark.png    paper ring on a dark rounded square. Shipped as-is for
                   the square slots other people's products crop for us:
                   a LinkedIn page avatar, a GitHub organisation, anywhere
                   a circle mask or a rounded-rect mask gets applied to
                   whatever we upload.

Both marks are the same ring at the same radii, measured: the stroke is
7.2% of the canvas in each, and only the colours and the outer shape
differ. That is why one master can feed the light set and the other needs
no resizing at all.

Every size is resized once from the 512 master rather than stepped down
through the larger ones, since a chain of resamples softens a 1px ring
long before it reaches 16px.

favicon.svg wraps the 512 PNG as a base64 <image> rather than redrawing
the mark as two <circle> elements. A redraw would be smaller and sharper,
and it is deliberately not what happens here: commit 4de4326 ("Use the
actual provided logo/favicon assets, not a redraw") replaced exactly that
kind of approximation with the designer's own file. An SVG master would
change this; a hand-traced one would not.

Usage:
    python scripts/make_icons.py
    python scripts/make_icons.py --check    # verify without writing
"""

import argparse
import base64
import io
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
BRAND = ROOT / "brand"
OUT = ROOT / "frontend"

# 180 is apple-touch-icon, which iOS demands as PNG and will not take as
# SVG. 512 is the largest anything asks for and doubles as the master the
# SVG wraps. 32 and 16 are the two a browser tab actually picks between.
PNG_SIZES = (512, 180, 32, 16)

# The old favicon.ico held one 16x16 image, so anything wanting a crisper
# icon (a Windows shortcut, a browser that prefers .ico over SVG) had
# nothing better to choose. These are the four sizes .ico is normally
# asked for.
ICO_SIZES = [(16, 16), (32, 32), (48, 48), (64, 64)]


def build(check: bool = False) -> int:
    light = BRAND / "mark-light.png"
    dark = BRAND / "mark-dark.png"
    for p in (light, dark):
        if not p.exists():
            print(f"missing master: {p}", file=sys.stderr)
            return 1

    src = Image.open(light).convert("RGBA")
    if src.size != (512, 512):
        print(f"expected a 512x512 master, got {src.size}", file=sys.stderr)
        return 1

    written = []
    for size in PNG_SIZES:
        im = src.resize((size, size), Image.LANCZOS)
        target = OUT / f"favicon-{size}.png"
        if not check:
            im.save(target)
        written.append(target)

    # Pillow builds every entry in ICO_SIZES from the image it is handed,
    # so hand it one comfortably larger than the biggest entry.
    ico = OUT / "favicon.ico"
    if not check:
        src.resize((256, 256), Image.LANCZOS).save(ico, sizes=ICO_SIZES)
    written.append(ico)

    buf = io.BytesIO()
    src.save(buf, format="PNG")
    data = base64.b64encode(buf.getvalue()).decode("ascii")
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">\n'
        f'  <image href="data:image/png;base64,{data}" width="512" height="512"/>\n'
        "</svg>\n"
    )
    svg_path = OUT / "favicon.svg"
    if not check:
        svg_path.write_text(svg, encoding="utf-8")
    written.append(svg_path)

    # The dark master ships unresized. Every platform that takes a square
    # avatar wants at least 400px and scales down itself, so 512 is the
    # one file that serves all of them.
    square = OUT / "logo-square.png"
    if not check:
        Image.open(dark).convert("RGBA").save(square)
    written.append(square)

    for p in written:
        rel = p.relative_to(ROOT).as_posix()
        if check:
            # No byte count on this path, deliberately. The file sitting
            # there is still the previous build, and printing its size
            # beside "would write" reads as a prediction of the new one.
            print(f"  would write {rel}")
        else:
            print(f"  wrote {rel:<28} {p.stat().st_size:>7} bytes")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true",
                    help="report what would be written without writing it")
    args = ap.parse_args()
    return build(check=args.check)


if __name__ == "__main__":
    raise SystemExit(main())
