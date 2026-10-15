"""Code-native JAKH identity assets; no customer data or upstream logo modification."""
from functools import lru_cache
from io import BytesIO

from PIL import Image, ImageDraw


@lru_cache(maxsize=8)
def icon_png(size=192, colour='#172e39', accent='#91c8b6'):
    if size not in (32, 180, 192, 512):
        raise ValueError('Unsupported platform icon size')
    image = Image.new('RGB', (512, 512), colour)
    draw = ImageDraw.Draw(image)
    # A geometric J monogram with generous safe area for maskable app icons.
    draw.line([(174, 140), (328, 140), (328, 318), (304, 360), (224, 360), (184, 320)], fill='white', width=36)
    draw.rounded_rectangle((170, 402, 342, 422), radius=10, fill=accent)
    if size != 512:
        image = image.resize((size, size), Image.Resampling.LANCZOS)
    output = BytesIO()
    image.save(output, format='PNG')
    return output.getvalue()


@lru_cache(maxsize=2)
def company_logo_png(colour='#172e39', accent='#146e65'):
    image = Image.new('RGBA', (540, 160), (255, 255, 255, 0))
    draw = ImageDraw.Draw(image)
    width = 15
    paths = [
        [(20, 24), (96, 24), (96, 104), (80, 122), (42, 122), (20, 102)],
        [(135, 122), (175, 24), (215, 122)],
        [(150, 88), (200, 88)],
        [(255, 24), (255, 122)],
        [(325, 24), (260, 78), (329, 122)],
        [(371, 24), (371, 122)],
        [(448, 24), (448, 122)],
        [(371, 75), (448, 75)],
    ]
    for points in paths:
        draw.line(points, fill=colour, width=width, joint='curve')
    draw.rounded_rectangle((20, 143, 455, 151), radius=4, fill=accent)
    output = BytesIO()
    image.save(output, format='PNG')
    return output.getvalue()
