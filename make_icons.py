"""Render the app icons: the dial ribbon, the app's own signature.

Three service bars of different spans with the brass NOW line crossing them --
the same idea as the hero, reduced until it still reads at 40px. A single bar
was tried first and looked like a generic progress meter.
"""
from playwright.sync_api import sync_playwright
from pathlib import Path

OUT = Path("/Users/whddnjs1124/Desktop/Projects/Dining")
SPANS = [(0.00, 0.72), (0.10, 1.00), (0.00, 0.55)]   # each bar's start/end
NOW = 0.46                                           # where the marker falls


def svg(inset, rounded):
    """inset: fraction of canvas kept clear at each edge, in viewBox units."""
    left, full = inset, 100 - inset * 2
    gap_ratio = 0.30
    h = (full * 0.62) / (len(SPANS) + (len(SPANS) - 1) * gap_ratio)
    gap = h * gap_ratio
    top = 50 - (len(SPANS) * h + (len(SPANS) - 1) * gap) / 2
    now_x = left + full * NOW

    parts = []
    for i, (a, b) in enumerate(SPANS):
        y, x, w = top + i * (h + gap), left + full * a, full * (b - a)
        parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{h*.2}" fill="#b9d9eb"/>')
        spent = max(0.0, min(now_x - x, w))          # the shadowed, spent part
        if spent > 0:
            parts.append(f'<rect x="{x}" y="{y}" width="{spent}" height="{h}" rx="{h*.2}" fill="#8ba9bd"/>')
    span = len(SPANS) * h + (len(SPANS) - 1) * gap
    parts.append(f'<rect x="{now_x-1.7}" y="{top-h*.55}" width="3.4" height="{span+h*1.1}" fill="#9a7b3f"/>')

    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
            f'<rect width="100" height="100" rx="{22 if rounded else 0}" fill="#fbfaf7"/>'
            f'{"".join(parts)}</svg>')


JOBS = [
    ("icon-180.png", 180, 20, True),            # apple-touch-icon
    ("icon-192.png", 192, 20, True),
    ("icon-512.png", 512, 20, True),
    ("icon-512-maskable.png", 512, 27, False),  # cropped to the middle 80%
]

with sync_playwright() as p:
    b = p.chromium.launch()
    for name, size, inset, rounded in JOBS:
        pg = b.new_page(viewport={"width": size, "height": size})
        pg.set_content(f'<body style="margin:0">{svg(inset, rounded)}</body>')
        pg.wait_for_timeout(120)
        pg.screenshot(path=str(OUT / name))
        pg.close()
        print(f"  {name}  {size}x{size}")
    b.close()
