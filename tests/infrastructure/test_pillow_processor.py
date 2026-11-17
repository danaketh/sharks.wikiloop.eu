import struct
import warnings
import zlib
from io import BytesIO

import pytest
from PIL import Image, ImageCms, ImageSequence, JpegImagePlugin, features

from strata_cms.application.errors import UnsupportedMediaError
from strata_cms.infrastructure.media.pillow import PillowImageProcessor

PROCESSOR = PillowImageProcessor(max_pixels=50_000_000)
GPS_IFD = 0x8825
ORIENTATION = 0x0112


def _encode(image: Image.Image, image_format: str, **params: object) -> bytes:
    buffer = BytesIO()
    image.save(buffer, image_format, **params)
    return buffer.getvalue()


def _jpeg_with_gps(**params: object) -> bytes:
    image = Image.new("RGB", (32, 16), (200, 30, 30))
    exif = Image.Exif()
    exif[0x010F] = "SharkCam"
    exif.get_ifd(GPS_IFD)[2] = (50.0, 5.0, 0.0)  # GPSLatitude
    return _encode(image, "JPEG", exif=exif, **params)


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    crc = zlib.crc32(kind + payload) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", crc)


def _png_header_only(width: int, height: int) -> bytes:
    """Return a PNG whose header claims a size but whose pixel data is junk."""
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", b"not-zlib-data")
        + _png_chunk(b"IEND", b"")
    )


def _decode(data: bytes) -> Image.Image:
    image = Image.open(BytesIO(data))
    image.load()
    return image


def _frame_durations(image: Image.Image) -> list[int]:
    durations = []
    for frame in ImageSequence.Iterator(image):
        frame.load()  # WebP exposes per-frame duration only after decoding
        durations.append(frame.info["duration"])
    return durations


def test_jpeg_gps_exif_is_stripped() -> None:
    source = _jpeg_with_gps()
    assert GPS_IFD in Image.open(BytesIO(source)).getexif()

    processed = PROCESSOR.process(source, preserve_original=False)

    output = _decode(processed.data)
    assert GPS_IFD not in output.getexif()
    assert len(output.getexif()) == 0
    assert "exif" not in output.info
    assert processed.content_type == "image/jpeg"
    assert processed.extension == "jpg"


def test_jpeg_comment_and_xmp_are_stripped() -> None:
    image = Image.new("RGB", (16, 16), (0, 0, 255))
    source = _encode(
        image,
        "JPEG",
        comment=b"shot at secret reef",
        xmp=b"<x:xmpmeta>gps</x:xmpmeta>",
    )

    processed = PROCESSOR.process(source, preserve_original=False)

    assert b"secret reef" not in processed.data
    assert b"xmpmeta" not in processed.data


def test_jpeg_reencoded_with_source_subsampling() -> None:
    source = _encode(Image.new("RGB", (32, 32), (1, 2, 3)), "JPEG", subsampling=0)

    processed = PROCESSOR.process(source, preserve_original=False)

    assert JpegImagePlugin.get_sampling(_decode(processed.data)) == 0


def test_orientation_applied_before_strip() -> None:
    image = Image.new("RGB", (100, 50), (10, 200, 10))
    exif = Image.Exif()
    exif[ORIENTATION] = 6
    source = _encode(image, "JPEG", exif=exif)

    processed = PROCESSOR.process(source, preserve_original=False)

    assert (processed.width, processed.height) == (50, 100)
    output = _decode(processed.data)
    assert output.size == (50, 100)
    assert ORIENTATION not in output.getexif()


def test_icc_profile_preserved() -> None:
    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    source = _encode(Image.new("RGB", (8, 8)), "PNG", icc_profile=profile)

    processed = PROCESSOR.process(source, preserve_original=False)

    assert _decode(processed.data).info.get("icc_profile") == profile


def test_preserve_original_returns_identical_bytes() -> None:
    source = _jpeg_with_gps()

    processed = PROCESSOR.process(source, preserve_original=True)

    assert processed.data == source
    assert processed.content_type == "image/jpeg"
    assert (processed.width, processed.height) == (32, 16)


def test_animated_gif_keeps_frames() -> None:
    frames = [Image.new("RGB", (10, 10), (i * 80, 0, 0)) for i in range(1, 4)]
    source = _encode(
        frames[0],
        "GIF",
        save_all=True,
        append_images=frames[1:],
        duration=[100, 200, 300],
        loop=0,
        comment=b"reef location",
    )

    processed = PROCESSOR.process(source, preserve_original=False)

    output = Image.open(BytesIO(processed.data))
    assert processed.content_type == "image/gif"
    assert getattr(output, "n_frames", 1) == 3
    assert output.info.get("loop") == 0
    durations = _frame_durations(output)
    assert durations == [100, 200, 300]
    assert b"reef location" not in processed.data


def test_animated_webp_keeps_frames_and_durations() -> None:
    frames = [Image.new("RGB", (10, 10), (i * 80, 0, 0)) for i in range(3)]
    source = _encode(
        frames[0],
        "WEBP",
        save_all=True,
        append_images=frames[1:],
        duration=[50, 60, 70],
        loop=2,
        lossless=True,
    )

    processed = PROCESSOR.process(source, preserve_original=False)

    output = Image.open(BytesIO(processed.data))
    assert processed.content_type == "image/webp"
    assert getattr(output, "n_frames", 1) == 3
    assert output.info.get("loop") == 2
    durations = _frame_durations(output)
    assert durations == [50, 60, 70]


def test_lossless_webp_stays_lossless() -> None:
    source = _encode(Image.new("RGB", (8, 8), (5, 6, 7)), "WEBP", lossless=True)

    processed = PROCESSOR.process(source, preserve_original=False)

    assert processed.data[12:16] == b"VP8L"
    assert _decode(processed.data).getpixel((0, 0)) == (5, 6, 7)


def test_lossy_webp_stays_lossy() -> None:
    source = _encode(Image.new("RGB", (8, 8), (5, 6, 7)), "WEBP", quality=80)

    processed = PROCESSOR.process(source, preserve_original=False)

    assert processed.content_type == "image/webp"
    assert b"VP8L" not in processed.data[:64]


def test_png_reencoded_losslessly() -> None:
    image = Image.new("RGBA", (4, 4), (1, 2, 3, 4))
    source = _encode(image, "PNG")

    processed = PROCESSOR.process(source, preserve_original=False)

    assert processed.extension == "png"
    assert _decode(processed.data).getpixel((1, 1)) == (1, 2, 3, 4)


@pytest.mark.parametrize(
    "payload",
    [
        b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg"></svg>',
        b'\xef\xbb\xbf  <svg xmlns="http://www.w3.org/2000/svg"/>',
    ],
)
def test_svg_rejected(payload: bytes) -> None:
    with pytest.raises(UnsupportedMediaError) as error:
        PROCESSOR.process(payload, preserve_original=False)

    assert error.value.reason == "media_type_not_allowed"


@pytest.mark.parametrize("image_format", ["BMP", "TIFF"])
def test_known_but_disallowed_image_types_rejected(image_format: str) -> None:
    source = _encode(Image.new("RGB", (4, 4)), image_format)

    with pytest.raises(UnsupportedMediaError) as error:
        PROCESSOR.process(source, preserve_original=False)

    assert error.value.reason == "media_type_not_allowed"


def test_png_renamed_as_jpg_detected_as_png() -> None:
    # Bytes are the only input: no filename or client type ever reaches here.
    source = _encode(Image.new("RGB", (4, 4)), "PNG")

    processed = PROCESSOR.process(source, preserve_original=False)

    assert processed.content_type == "image/png"
    assert processed.extension == "png"


def test_truncated_jpeg_rejected() -> None:
    source = _encode(Image.new("RGB", (64, 64), (9, 9, 9)), "JPEG")

    with pytest.raises(UnsupportedMediaError) as error:
        PROCESSOR.process(source[: len(source) // 2], preserve_original=False)

    assert error.value.reason == "media_unreadable"


def test_html_polyglot_rejected() -> None:
    with pytest.raises(UnsupportedMediaError) as error:
        PROCESSOR.process(
            b"<html><script>alert(1)</script></html>", preserve_original=False
        )

    assert error.value.reason == "media_unreadable"


def test_non_image_bytes_rejected() -> None:
    with pytest.raises(UnsupportedMediaError) as error:
        PROCESSOR.process(b"%PDF-1.4\n%junk", preserve_original=False)

    assert error.value.reason == "media_unreadable"


def test_empty_payload_rejected() -> None:
    with pytest.raises(UnsupportedMediaError) as error:
        PROCESSOR.process(b"", preserve_original=False)

    assert error.value.reason == "media_unreadable"


def test_decompression_bomb_rejected() -> None:
    with pytest.raises(UnsupportedMediaError) as error:
        PROCESSOR.process(_png_header_only(20_000, 20_000), preserve_original=False)

    assert error.value.reason == "media_too_many_pixels"


def test_just_over_pixel_limit_rejected_before_decode() -> None:
    # 8000 x 6251 = 50_008_000 > 50_000_000. The pixel data is junk, so a
    # full decode would fail as unreadable: this proves the check runs first.
    with pytest.raises(UnsupportedMediaError) as error:
        PROCESSOR.process(_png_header_only(8_000, 6_251), preserve_original=False)

    assert error.value.reason == "media_too_many_pixels"


def test_pixel_limit_is_per_processor_and_inclusive() -> None:
    processor = PillowImageProcessor(max_pixels=100)
    at_limit = _encode(Image.new("RGB", (10, 10)), "PNG")
    over_limit = _encode(Image.new("RGB", (11, 10)), "PNG")

    assert processor.process(at_limit, preserve_original=False).width == 10
    with pytest.raises(UnsupportedMediaError) as error:
        processor.process(over_limit, preserve_original=False)
    assert error.value.reason == "media_too_many_pixels"
    assert Image.MAX_IMAGE_PIXELS != 100


def test_pillow_bomb_warning_maps_to_too_many_pixels_without_leaking() -> None:
    # 10_000 x 10_000 lies between Pillow's warning and error thresholds.
    processor = PillowImageProcessor(max_pixels=10**9)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with pytest.raises(UnsupportedMediaError) as error:
            processor.process(_png_header_only(10_000, 10_000), preserve_original=False)

    assert error.value.reason == "media_too_many_pixels"
    assert caught == []


def test_preserve_original_still_rejects_svg() -> None:
    with pytest.raises(UnsupportedMediaError) as error:
        PROCESSOR.process(b"<svg onload='x()'/>", preserve_original=True)

    assert error.value.reason == "media_type_not_allowed"


def test_preserve_original_still_rejects_corrupt_file() -> None:
    source = _encode(Image.new("RGB", (64, 64), (9, 9, 9)), "JPEG")

    with pytest.raises(UnsupportedMediaError) as error:
        PROCESSOR.process(source[: len(source) // 2], preserve_original=True)

    assert error.value.reason == "media_unreadable"


def test_preserve_original_still_checks_pixel_limit() -> None:
    with pytest.raises(UnsupportedMediaError) as error:
        PROCESSOR.process(_png_header_only(8_000, 6_251), preserve_original=True)

    assert error.value.reason == "media_too_many_pixels"


def _avif() -> bytes:
    exif = Image.Exif()
    exif.get_ifd(GPS_IFD)[2] = (1.0, 2.0, 3.0)
    return _encode(Image.new("RGB", (8, 8), (40, 50, 60)), "AVIF", exif=exif)


@pytest.mark.skipif(not features.check("avif"), reason="Pillow built without AVIF")
def test_avif_accepted_and_stripped_when_supported() -> None:
    processed = PROCESSOR.process(_avif(), preserve_original=False)

    assert (processed.content_type, processed.extension) == ("image/avif", "avif")
    assert GPS_IFD not in _decode(processed.data).getexif()


def test_avif_rejected_when_pillow_lacks_support(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(features, "check", lambda _feature: False)
    processor = PillowImageProcessor(max_pixels=50_000_000)
    avif_header = b"\x00\x00\x00\x1cftypavif\x00\x00\x00\x00avifmif1miaf"

    with pytest.raises(UnsupportedMediaError) as error:
        processor.process(avif_header, preserve_original=False)

    assert error.value.reason == "media_type_not_allowed"


def test_heic_rejected_as_disallowed_type() -> None:
    heic_header = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic"

    with pytest.raises(UnsupportedMediaError) as error:
        PROCESSOR.process(heic_header, preserve_original=False)

    assert error.value.reason == "media_type_not_allowed"


def test_unknown_isobmff_brand_is_unreadable() -> None:
    with pytest.raises(UnsupportedMediaError) as error:
        PROCESSOR.process(
            b"\x00\x00\x00\x14ftypisom\x00\x00\x00\x00", preserve_original=False
        )

    assert error.value.reason == "media_unreadable"


def test_multi_picture_jpeg_stored_as_single_jpeg() -> None:
    frames = [Image.new("RGB", (8, 8), (i * 100, 0, 0)) for i in range(2)]
    source = _encode(frames[0], "MPO", save_all=True, append_images=frames[1:])

    processed = PROCESSOR.process(source, preserve_original=False)

    output = _decode(processed.data)
    assert processed.content_type == "image/jpeg"
    assert output.format == "JPEG"
    assert getattr(output, "n_frames", 1) == 1


def _animation(image_format: str, frame_count: int) -> bytes:
    frames = [
        Image.new("RGB", (10, 10), (i * 40 % 256, 0, 0)) for i in range(frame_count)
    ]
    params: dict[str, object] = {}
    if image_format == "WEBP":
        params["lossless"] = True
    return _encode(
        frames[0],
        image_format,
        save_all=True,
        append_images=frames[1:],
        duration=[10] * frame_count,
        **params,
    )


@pytest.mark.parametrize("image_format", ["GIF", "WEBP", "PNG"])
@pytest.mark.parametrize("preserve_original", [False, True])
def test_animation_over_total_pixel_budget_rejected(
    image_format: str, preserve_original: bool
) -> None:
    # One 10x10 frame (100 px) fits; 5 frames (500 px) exceed the 400 budget.
    processor = PillowImageProcessor(max_pixels=400)

    with pytest.raises(UnsupportedMediaError) as error:
        processor.process(
            _animation(image_format, 5), preserve_original=preserve_original
        )

    assert error.value.reason == "media_too_many_pixels"


@pytest.mark.parametrize("image_format", ["GIF", "WEBP", "PNG"])
@pytest.mark.parametrize("preserve_original", [False, True])
def test_animation_within_total_pixel_budget_accepted(
    image_format: str, preserve_original: bool
) -> None:
    processor = PillowImageProcessor(max_pixels=400)
    source = _animation(image_format, 4)

    processed = processor.process(source, preserve_original=preserve_original)

    output = Image.open(BytesIO(processed.data))
    assert getattr(output, "n_frames", 1) == 4
    if preserve_original:
        assert processed.data == source


def test_gif_without_loop_extension_does_not_gain_one() -> None:
    frames = [Image.new("RGB", (10, 10), (i * 80, 0, 0)) for i in range(1, 4)]
    source = _encode(
        frames[0], "GIF", save_all=True, append_images=frames[1:], duration=100
    )
    assert "loop" not in Image.open(BytesIO(source)).info

    processed = PROCESSOR.process(source, preserve_original=False)

    output = Image.open(BytesIO(processed.data))
    assert getattr(output, "n_frames", 1) == 3
    assert "loop" not in output.info
    assert b"NETSCAPE2.0" not in processed.data


def test_gif_loop_count_is_kept() -> None:
    frames = [Image.new("RGB", (10, 10), (i * 80, 0, 0)) for i in range(1, 3)]
    source = _encode(frames[0], "GIF", save_all=True, append_images=frames[1:], loop=3)

    processed = PROCESSOR.process(source, preserve_original=False)

    assert Image.open(BytesIO(processed.data)).info.get("loop") == 3


@pytest.mark.parametrize("orientation", [5, 6, 7, 8])
def test_preserve_original_reports_displayed_dimensions(orientation: int) -> None:
    exif = Image.Exif()
    exif[ORIENTATION] = orientation
    source = _encode(Image.new("RGB", (100, 50)), "JPEG", exif=exif)

    processed = PROCESSOR.process(source, preserve_original=True)

    assert processed.data == source
    assert (processed.width, processed.height) == (50, 100)


@pytest.mark.parametrize("orientation", [1, 2, 3, 4])
def test_preserve_original_keeps_unrotated_dimensions(orientation: int) -> None:
    exif = Image.Exif()
    exif[ORIENTATION] = orientation
    source = _encode(Image.new("RGB", (100, 50)), "JPEG", exif=exif)

    processed = PROCESSOR.process(source, preserve_original=True)

    assert (processed.width, processed.height) == (100, 50)


def test_isobmff_brand_scan_is_capped() -> None:
    # A huge declared ftyp size must not make the sniffer scan the whole upload.
    header = b"\x7f\xff\xff\xffftypisom\x00\x00\x00\x00"
    payload = header + b"\x00" * 4096 + b"heic"

    with pytest.raises(UnsupportedMediaError) as error:
        PROCESSOR.process(payload, preserve_original=False)

    assert error.value.reason == "media_unreadable"
