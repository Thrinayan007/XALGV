"""Confined, bounded screenshot transfer for the local vision fallback."""

import base64
import io

from PIL import Image

from .errors import BrowserError


def image_data(session, path):
    target = session.artifact_path(path)
    if target.suffix.lower() != ".png" or not target.is_file() or target.stat().st_size > 20 * 1024 * 1024:
        raise BrowserError("INVALID_IMAGE", "Expected an existing workspace PNG no larger than 20 MiB")
    try:
        with Image.open(target) as picture:
            if picture.format != "PNG" or picture.width * picture.height > 16_000_000:
                raise BrowserError("INVALID_IMAGE", "PNG dimensions exceed the vision transfer limit")
            picture.thumbnail((1280, 1280))
            buffer = io.BytesIO()
            picture.convert("RGB").save(buffer, format="PNG")
            return {"image_base64": base64.b64encode(buffer.getvalue()).decode(), "width": picture.width, "height": picture.height}
    except (OSError, Image.DecompressionBombError) as exc:
        raise BrowserError("INVALID_IMAGE", "Unable to decode screenshot") from exc
