#!/usr/bin/env python3
"""Generate toolbar icon variants for fulcra-attention.

Reads the master `fulcra-mark.png` (256x256, white-on-black hexagon +
spiral) and emits 12 PNGs:

    icon-active-{16,32,48,128}.png  → default, full color, mint accent
    icon-paused-{16,32,48,128}.png  → desaturated grey + transparent bg
    icon-error-{16,32,48,128}.png   → red overlay

The active variant tints the white pixels with the fulcra mint
(#56d6b7); paused tints with mid-grey (#8a8a8e); error tints with the
brand danger red (#d92638). The black hexagon ring stays black on all
three so the silhouette reads consistently in a Chrome toolbar.

Run once from chrome/icons/: ../../.venv/bin/python _generate.py
"""
from pathlib import Path
from PIL import Image

HERE = Path(__file__).parent
MASTER = HERE / "fulcra-mark.png"

VARIANTS = {
    "active": (0x56, 0xd6, 0xb7),  # --primary-green from fulcra.ai
    "paused": (0x8a, 0x8a, 0x8e),  # neutral slate
    "error":  (0xd9, 0x26, 0x38),  # brand danger red
}
SIZES = (16, 32, 48, 128)


def tint_white(img: Image.Image, color: tuple[int, int, int]) -> Image.Image:
    """Replace every (near-)white pixel with `color`; keep alpha + non-white."""
    out = img.convert("RGBA").copy()
    pixels = out.load()
    r0, g0, b0 = color
    for y in range(out.height):
        for x in range(out.width):
            r, g, b, a = pixels[x, y]
            # Treat "white" as channels > 200 (the source has slight
            # antialiasing on the spiral edges).
            if r > 200 and g > 200 and b > 200 and a > 0:
                # Scale the colour by source brightness so antialiasing
                # gradients survive.
                scale = min(r, g, b) / 255
                pixels[x, y] = (
                    int(r0 * scale), int(g0 * scale), int(b0 * scale), a,
                )
    return out


def main() -> None:
    master = Image.open(MASTER).convert("RGBA")
    for name, color in VARIANTS.items():
        tinted = tint_white(master, color)
        for size in SIZES:
            small = tinted.resize((size, size), Image.LANCZOS)
            out_path = HERE / f"icon-{name}-{size}.png"
            small.save(out_path)
            print(f"  → {out_path.name}")
    # Also keep the legacy `icon-{16,32,48,128}.png` aliases (= active)
    # so a stale dist/ or pre-update manifest doesn't 404.
    for size in SIZES:
        active = HERE / f"icon-active-{size}.png"
        alias  = HERE / f"icon-{size}.png"
        alias.write_bytes(active.read_bytes())
        print(f"  → {alias.name} (alias)")


if __name__ == "__main__":
    main()
