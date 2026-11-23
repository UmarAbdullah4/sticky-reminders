"""Draw the app icon and write assets/icon.ico.

The picture is the same sticky note the tray icon shows, drawn large and
saved at every size Windows asks for (title bars, taskbar, Start menu, the
installer). Run it again only if the design changes; the result is committed.

    python tools/make_icon.py
"""
import os

from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "assets", "icon.ico")
SIZES = [16, 20, 24, 32, 40, 48, 64, 128, 256]


def draw(size):
    """The note at `size` pixels. Drawn at 4x and scaled down for clean edges."""
    big = size * 4
    unit = big / 64                               # the design is laid out on a 64-unit grid
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([6 * unit, 8 * unit, 58 * unit, 56 * unit], radius=9 * unit,
                        fill=(255, 201, 77))
    # the folded corner
    d.polygon([(58 * unit, 40 * unit), (58 * unit, 56 * unit), (42 * unit, 56 * unit)],
              fill=(224, 168, 0))
    # lines of writing; too fine to read below 24 px, so the smallest sizes go without
    if size >= 24:
        for y, end in ((22, 48), (32, 48), (42, 38)):
            d.line([(16 * unit, y * unit), (end * unit, y * unit)], fill=(26, 26, 26),
                   width=max(1, round(4 * unit)))
    return img.resize((size, size), Image.LANCZOS)


def main():
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    images = [draw(s) for s in SIZES]
    images[-1].save(OUT, format="ICO", sizes=[(s, s) for s in SIZES],
                    append_images=images[:-1])
    print(f"wrote {os.path.relpath(OUT, ROOT)} ({os.path.getsize(OUT)} bytes, "
          f"{len(SIZES)} sizes)")


if __name__ == "__main__":
    main()
