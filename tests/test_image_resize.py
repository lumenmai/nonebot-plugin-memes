from io import BytesIO
import random

from PIL import Image
import pytest

from nonebot_plugin_memes.generation_worker import ImageRejectedError, prepare_static_image


def encode(image: Image.Image, **kwargs) -> bytes:
    output = BytesIO()
    image.save(output, format="PNG", **kwargs)
    return output.getvalue()


@pytest.mark.parametrize("size", [(64, 64), (1024, 768)])
def test_acceptable_static_image_is_returned_unchanged(size):
    original = encode(Image.new("RGB", size, "white"))

    assert prepare_static_image(original, 2_000_000, 8 * 1024 * 1024) == original


def test_pixel_limit_preserves_aspect_ratio_and_transparency():
    original = encode(Image.new("RGBA", (1600, 1200), (255, 0, 0, 100)))

    result = prepare_static_image(original, 800_000, 8 * 1024 * 1024)

    with Image.open(BytesIO(result)) as image:
        assert image.width * image.height <= 800_000
        assert abs(image.width / image.height - 4 / 3) < 0.002
        assert min(image.size) >= 256
        assert image.mode == "RGBA"
        assert image.getpixel((0, 0))[3] == 100


@pytest.mark.parametrize(
    ("max_bytes", "expected_size"), [(1_000_000, 512), (200_000, 256)]
)
def test_byte_limit_halves_dimensions_up_to_twice(max_bytes, expected_size):
    noise = random.Random(0).randbytes(1024 * 1024 * 3)
    original = encode(Image.frombytes("RGB", (1024, 1024), noise))

    result = prepare_static_image(original, 2_000_000, max_bytes)

    assert len(result) <= max_bytes
    with Image.open(BytesIO(result)) as image:
        assert image.size == (expected_size, expected_size)


@pytest.mark.parametrize("size", [(1024, 1024), (2048, 2048)])
def test_unresolved_byte_limit_stops_at_floor_or_retry_limit(size):
    original = encode(Image.new("RGB", size, "white"))

    with pytest.raises(
        ImageRejectedError,
        match=f"bytes too large after resizing.*size={size[0] // 4}x{size[1] // 4}",
    ):
        prepare_static_image(original, 10_000_000, 1)


def test_pixel_limit_does_not_shrink_short_edge_below_256():
    original = encode(Image.new("RGB", (1600, 400), "white"))

    with pytest.raises(ImageRejectedError, match="short edge below 256"):
        prepare_static_image(original, 200_000, 8 * 1024 * 1024)


def test_exif_rotation_is_preserved_when_resizing():
    exif = Image.Exif()
    exif[274] = 6
    original = encode(Image.new("RGB", (1024, 768), "red"), exif=exif)

    result = prepare_static_image(original, 196_608, 8 * 1024 * 1024)

    with Image.open(BytesIO(result)) as image:
        assert image.size == (384, 512)
        assert image.getexif().get(274) is None


def test_animation_is_returned_unchanged_even_if_oversized():
    output = BytesIO()
    Image.new("RGB", (512, 512), "red").save(
        output,
        format="GIF",
        save_all=True,
        append_images=[Image.new("RGB", (512, 512), "blue")],
        duration=[40, 100],
        loop=0,
    )
    original = output.getvalue()

    assert prepare_static_image(original, 1, 1) == original


def test_invalid_image_is_rejected():
    with pytest.raises(ImageRejectedError, match="prepare static image failed"):
        prepare_static_image(b"not an image", 2_000_000, 8 * 1024 * 1024)
