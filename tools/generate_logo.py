"""Generate the RapidMoto "RM" logo + window/exe icon assets.

Draws a slanted, blue-outlined black badge with a bold italic white "RM" and
writes three files into ``rmfantasy/ui/assets/``:

    logo.png   wide badge shown in the in-app header
    icon.png   square icon used for the Tk window on Linux/macOS
    icon.ico   multi-size icon used for the Windows window + built .exe

Run it from the repo root with:  python tools/generate_logo.py

--------------------------------------------------------------------------- #
Using YOUR exact artwork instead
--------------------------------------------------------------------------- #
This script only draws a clean stand-in that matches the RapidMoto look. To use
your real logo, just drop your own PNG in as ``rmfantasy/ui/assets/logo.png``
(a wide transparent PNG works best). For the Windows executable icon, replace
``rmfantasy/ui/assets/icon.ico`` -- or save your logo as a square
``icon.png`` and re-run this script, which will regenerate the .ico from it.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# RapidMoto palette (matches the app theme).
BRAND = (47, 107, 255, 255)     # #2f6bff blue border/glow
BRAND_DK = (28, 74, 196, 255)   # subtle inner blue edge
BLACK = (11, 13, 18, 255)       # badge interior
WHITE = (246, 248, 252, 255)    # RM lettering

ASSETS = Path(__file__).resolve().parents[1] / "rmfantasy" / "ui" / "assets"

# Candidate bold fonts, tried in order (works across dev machines).
_FONT_CANDIDATES = [
    "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "DejaVuSans-Bold.ttf",
]


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    for path in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except Exception:  # noqa: BLE001
            continue
    return ImageFont.load_default()


def _parallelogram(w: int, h: int, margin: int, skew: int):
    """Right-leaning parallelogram points (top shifted right of bottom)."""
    return [
        (margin + skew, margin),
        (w - margin, margin),
        (w - margin - skew, h - margin),
        (margin, h - margin),
    ]


def _inset(points, scale: float):
    cx = sum(p[0] for p in points) / len(points)
    cy = sum(p[1] for p in points) / len(points)
    return [(cx + (x - cx) * scale, cy + (y - cy) * scale) for x, y in points]


def _sheared_rm(height: int, shear: float = 0.30) -> Image.Image:
    """Render a bold, italic-sheared 'RM' as a transparent RGBA image."""
    font = _load_font(height)
    tmp = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
    bbox = tmp.textbbox((0, 0), "RM", font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    pad = int(th * 0.4)
    img = Image.new("RGBA", (tw + pad * 2, th + pad * 2), (0, 0, 0, 0))
    ImageDraw.Draw(img).text((pad - bbox[0], pad - bbox[1]), "RM", font=font, fill=WHITE)

    w, h = img.size
    xshift = int(shear * h)
    # Output pixel (x, y) samples input (x - shear*(h - y), y): top leans right.
    sheared = img.transform(
        (w + xshift, h), Image.AFFINE,
        (1, -shear, shear * h, 0, 1, 0), resample=Image.BICUBIC,
    )
    return sheared.crop(sheared.getbbox())


def build_logo(width: int = 1024, height: int = 560) -> Image.Image:
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    skew = int(height * 0.34)
    outer = _parallelogram(width, height, margin=int(height * 0.12), skew=skew)
    mid = _inset(outer, 0.965)
    inner = _inset(outer, 0.90)

    draw.polygon(outer, fill=BRAND)          # blue border
    draw.polygon(mid, fill=BRAND_DK)         # subtle darker blue ring
    draw.polygon(inner, fill=BLACK)          # black interior

    # RM lettering, sized to the badge interior and centred within it.
    rm = _sheared_rm(int(height * 0.62))
    xs = [p[0] for p in inner]
    ys = [p[1] for p in inner]
    box_cx = (min(xs) + max(xs)) / 2
    box_cy = (min(ys) + max(ys)) / 2
    max_w = (max(xs) - min(xs)) * 0.82
    if rm.width > max_w:
        ratio = max_w / rm.width
        rm = rm.resize((int(rm.width * ratio), int(rm.height * ratio)), Image.LANCZOS)
    img.alpha_composite(rm, (int(box_cx - rm.width / 2), int(box_cy - rm.height / 2)))
    return img


def build_icon(source: Image.Image, size: int = 512) -> Image.Image:
    """Square icon: the badge centred on a transparent square canvas."""
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    scale = min(size / source.width, size / source.height) * 0.94
    resized = source.resize(
        (max(1, int(source.width * scale)), max(1, int(source.height * scale))),
        Image.LANCZOS,
    )
    canvas.alpha_composite(
        resized, ((size - resized.width) // 2, (size - resized.height) // 2)
    )
    return canvas


def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)

    # If a square icon.png already exists (e.g. the user's real artwork), build
    # the .ico from that; otherwise generate everything from the drawn badge.
    logo = build_logo()
    logo.save(ASSETS / "logo.png")

    user_icon = ASSETS / "icon.png"
    icon_src = Image.open(user_icon).convert("RGBA") if user_icon.exists() else build_icon(logo)
    if not user_icon.exists():
        icon_src.save(user_icon)

    icon_src.save(
        ASSETS / "icon.ico",
        sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    print(f"Wrote: {ASSETS / 'logo.png'} ({logo.size[0]}x{logo.size[1]})")
    print(f"Wrote: {ASSETS / 'icon.png'} ({icon_src.size[0]}x{icon_src.size[1]})")
    print(f"Wrote: {ASSETS / 'icon.ico'}")


if __name__ == "__main__":
    main()
