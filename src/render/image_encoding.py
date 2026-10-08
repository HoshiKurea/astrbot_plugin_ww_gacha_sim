"""Compact image encodings without changing draw data or transparent UI edges."""

import io

from PIL import Image


def encode_portrait(content: bytes) -> bytes:
    with Image.open(io.BytesIO(content)) as image:
        if image.format not in {"PNG", "JPEG", "WEBP"}:
            raise ValueError("立绘图片格式无效")
        if image.width * image.height > 20_000_000:
            raise ValueError("立绘图片超过 2000 万像素")
        image.thumbnail((1280, 1280), Image.Resampling.LANCZOS)
        picture = image.convert("RGBA")
    output = io.BytesIO()
    picture.save(output, format="WEBP", quality=90, method=4, exact=True)
    return output.getvalue()


def encode_result_image(image: Image.Image) -> tuple[bytes, str]:
    output = io.BytesIO()
    if image.mode == "P" and "transparency" in image.info:
        image = image.convert("RGBA")
    if "A" in image.getbands() and image.getchannel("A").getextrema()[0] < 255:
        image.save(output, format="PNG", optimize=True)
        suffix = "png"
    else:
        image.convert("RGB").save(output, format="JPEG", quality=90,
                                  subsampling=0, optimize=True)
        suffix = "jpg"
    return output.getvalue(), suffix
