"""Draws assets/icon.ico (the .exe / taskbar icon, 16-256 px) from the app logo:
a light rounded square with the "activity" pulse line, as in the app header.

Needs Pillow (not a dependency of the app):  pip install pillow
"""

from pathlib import Path

from PIL import Image, ImageDraw

ASSETS = Path(__file__).resolve().parent.parent / "assets"
LOGO_FILL, LOGO_LINE = "#f4f4f5", "#18181b"

# The "activity" pulse line from the app header, as fractions of the logo square.
PULSE = [(0.12, 0.5), (0.30, 0.5), (0.40, 0.25), (0.55, 0.78), (0.66, 0.5), (0.88, 0.5)]


def draw_logo(d: ImageDraw.ImageDraw, x: float, y: float, size: float, line: float) -> None:
    """Light rounded square with the dark pulse line."""
    d.rounded_rectangle([x, y, x + size, y + size], radius=size * 0.21, fill=LOGO_FILL)
    d.line([(x + px * size, y + py * size) for px, py in PULSE], fill=LOGO_LINE, width=round(line), joint="curve")


def icon_image(px: int) -> Image.Image:
    """One icon size, drawn at 8x and scaled down. Small sizes get a bolder line to stay legible."""
    S = 8
    img = Image.new("RGBA", (px * S, px * S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    margin = px * S * (0.04 if px <= 32 else 0.06)
    size = px * S - 2 * margin
    line = size * (0.13 if px <= 16 else 0.11 if px <= 32 else 0.075)
    draw_logo(d, margin, margin, size, line)
    return img.resize((px, px), Image.LANCZOS)


def make_icon() -> None:
    sizes = [16, 24, 32, 48, 64, 128, 256]
    images = [icon_image(s) for s in sizes]
    images[-1].save(ASSETS / "icon.ico", sizes=[(s, s) for s in sizes], append_images=images[:-1])


def main() -> None:
    ASSETS.mkdir(exist_ok=True)
    make_icon()
    print(f"wrote {ASSETS / 'icon.ico'}")


if __name__ == "__main__":
    main()
