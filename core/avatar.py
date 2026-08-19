from io import BytesIO

from PIL import Image, ImageOps


AVATAR_SIZE = (256, 256)


def optimize_avatar(upload):
    upload.seek(0)
    with Image.open(upload) as image:
        image = ImageOps.exif_transpose(image)
        image = ImageOps.fit(image.convert("RGB"), AVATAR_SIZE, Image.Resampling.LANCZOS)
        output = BytesIO()
        image.save(output, format="WEBP", quality=82, method=6)
    return output.getvalue(), "image/webp"
