from io import BytesIO

from PIL import Image, ImageOps


AVATAR_SIZE = (256, 256)
BACKGROUND_MAX_SIZE = (2560, 2560)


def optimize_avatar(upload):
    upload.seek(0)
    with Image.open(upload) as image:
        image = ImageOps.exif_transpose(image)
        image = ImageOps.fit(image.convert("RGB"), AVATAR_SIZE, Image.Resampling.LANCZOS)
        output = BytesIO()
        image.save(output, format="WEBP", quality=82, method=6)
    return output.getvalue(), "image/webp"


def optimize_background(upload):
    upload.seek(0)
    with Image.open(upload) as image:
        image = ImageOps.exif_transpose(image).convert("RGB")
        image.thumbnail(BACKGROUND_MAX_SIZE, Image.Resampling.LANCZOS)
        output = BytesIO()
        image.save(output, format="WEBP", quality=80, method=6)
    return output.getvalue(), "image/webp"
