"""Process-safe entry point for CPU-bound meme generation."""

from __future__ import annotations

from io import BytesIO
import math
import pickle
import struct
import sys
import traceback
from typing import Any

from PIL import Image, ImageOps


class ImageRejectedError(ValueError):
    """An input image could not be reduced within the configured limits."""


def prepare_static_image(image: bytes, max_pixels: int, max_bytes: int) -> bytes:
    """Reduce oversized still images; leave acceptable images and animations intact."""

    try:
        with Image.open(BytesIO(image)) as source:
            if getattr(source, "is_animated", False):
                return image
            pixels = source.width * source.height
            if pixels <= max_pixels and len(image) <= max_bytes:
                return image

            target = source.size
            if pixels > max_pixels:
                ratio = math.sqrt(max_pixels / pixels)
                target = (
                    max(1, int(source.width * ratio)),
                    max(1, int(source.height * ratio)),
                )
                if min(target) < 256:
                    raise ImageRejectedError(
                        f"static image pixel limit requires short edge below 256, "
                        f"size={source.width}x{source.height}, limit={max_pixels}"
                    )

            has_alpha = "A" in source.getbands() or "transparency" in source.info
            if source.size != target:
                source.thumbnail(target, Image.Resampling.LANCZOS)
            oriented = ImageOps.exif_transpose(source)
            reduced = oriented.convert("RGBA" if has_alpha else "RGB")
            reduced.info.clear()

        # Encode once at the pixel limit, then halve each dimension at most twice.
        for attempt in range(3):
            output = BytesIO()
            reduced.save(output, format="PNG")
            result = output.getvalue()
            if len(result) <= max_bytes:
                return result
            target = (reduced.width // 2, reduced.height // 2)
            if attempt == 2 or min(target) < 256:
                break
            reduced = reduced.resize(target, Image.Resampling.LANCZOS)

        raise ImageRejectedError(
            f"static image bytes too large after resizing, bytes={len(result)}, "
            f"limit={max_bytes}, size={reduced.width}x{reduced.height}"
        )
    except ImageRejectedError:
        raise
    except Exception as e:
        raise ImageRejectedError(
            f"prepare static image failed: {type(e).__name__}: {e}"
        ) from e


def generate_meme_payload(
    meme_key: str,
    images: list[bytes],
    texts: list[str],
    args: dict[str, Any],
    image_limits: tuple[int, int],
) -> bytes:
    """Generate a meme in a worker process and return its encoded payload."""

    from meme_generator import get_meme

    images = [prepare_static_image(image, *image_limits) for image in images]
    result = get_meme(meme_key)(images=images, texts=texts, args=args)
    return result.getvalue()


def _read_exact(size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = sys.stdin.buffer.read(remaining)
        if not chunk:
            raise EOFError("incomplete meme worker request")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _read_request() -> (
    tuple[str, list[bytes], list[str], dict[str, Any], tuple[int, int]] | None
):
    header = sys.stdin.buffer.read(8)
    if not header:
        return None
    if len(header) != 8:
        raise EOFError("incomplete meme worker request header")
    size = struct.unpack("!Q", header)[0]
    return pickle.loads(_read_exact(size))


def _write_response(status: str, payload: Any) -> None:
    response = pickle.dumps((status, payload))
    sys.stdout.buffer.write(struct.pack("!Q", len(response)) + response)
    sys.stdout.buffer.flush()


def serve() -> None:
    """Serve generation requests until the parent closes or kills the process."""

    from meme_generator import get_meme  # noqa: F401 - eagerly warm template imports
    from meme_generator.exception import MemeGeneratorException

    _write_response("ready", None)
    while request := _read_request():
        try:
            _write_response("ok", generate_meme_payload(*request))
        except ImageRejectedError as e:
            _write_response("image_rejected", str(e))
        except MemeGeneratorException as e:
            _write_response("meme_error", e.message)
        except BaseException:
            _write_response("worker_error", traceback.format_exc())


if __name__ == "__main__":
    if sys.argv[1:] != ["--serve"]:
        raise SystemExit("usage: python generation_worker.py --serve")
    serve()
