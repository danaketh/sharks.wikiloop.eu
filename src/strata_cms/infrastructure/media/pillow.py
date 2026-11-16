"""Pillow image processor: content sniffing, validation and metadata stripping.

Untrusted bytes are classified by magic bytes before any Pillow parser runs,
and Pillow is then restricted to exactly the one allowlisted decoder for the
sniffed type, so disallowed formats never reach their (larger, historically
riskier) parsers. Client filenames and Content-Type headers are never inputs.
"""

import struct
import warnings
from collections.abc import Iterator
from dataclasses import dataclass
from io import BytesIO

from PIL import Image, ImageOps, ImageSequence, JpegImagePlugin, features

from strata_cms.application.errors import UnsupportedMediaError
from strata_cms.application.ports.media import ProcessedImage

# Exceptions Pillow's decoders are known to raise on malformed input. This is
# the untrusted-parser boundary: any of them means "not a readable image".
_DECODE_ERRORS: tuple[type[Exception], ...] = (
    OSError,  # includes UnidentifiedImageError and "image file is truncated"
    ValueError,
    SyntaxError,
    EOFError,
    struct.error,
    IndexError,
    KeyError,
    TypeError,
)

# Encoder-relevant info keys kept on re-encode; everything else (exif, xmp,
# comment, photoshop/adobe blocks, ...) is dropped.
_KEPT_INFO_KEYS = frozenset(
    {"transparency", "duration", "loop", "background", "disposal", "blend"}
)

_SVG_PROBE_BYTES = 1024
_LOSSY_QUALITY = 95


@dataclass(frozen=True, slots=True)
class _ImageType:
    pillow_format: str
    content_type: str
    extension: str


_JPEG = _ImageType("JPEG", "image/jpeg", "jpg")
_PNG = _ImageType("PNG", "image/png", "png")
_GIF = _ImageType("GIF", "image/gif", "gif")
_WEBP = _ImageType("WEBP", "image/webp", "webp")
_AVIF = _ImageType("AVIF", "image/avif", "avif")

# Pillow may refine a JPEG into its MPO subclass (multi-picture JPEG).
_ACCEPTED_PILLOW_FORMATS: dict[str, frozenset[str]] = {
    "JPEG": frozenset({"JPEG", "MPO"}),
    "PNG": frozenset({"PNG"}),
    "GIF": frozenset({"GIF"}),
    "WEBP": frozenset({"WEBP"}),
    "AVIF": frozenset({"AVIF"}),
}

_AVIF_BRANDS = frozenset({b"avif", b"avis"})
_OTHER_ISOBMFF_IMAGE_BRANDS = frozenset(
    {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1"}
)
_DISALLOWED_IMAGE_SIGNATURES: tuple[bytes, ...] = (
    b"BM",  # BMP
    b"II*\x00",  # TIFF little-endian
    b"MM\x00*",  # TIFF big-endian
    b"\x00\x00\x01\x00",  # ICO
    b"8BPS",  # PSD
    b"\xff\x0a",  # JPEG XL codestream
    b"\x00\x00\x00\x0cJXL ",  # JPEG XL container
    b"qoif",  # QOI
)


class PillowImageProcessor:
    """`ImageProcessor` adapter backed by Pillow."""

    def __init__(self, *, max_pixels: int) -> None:
        """Bind the per-image pixel limit; AVIF is allowed only if supported."""
        self._max_pixels = max_pixels
        self._avif_supported = bool(features.check("avif"))

    def process(self, data: bytes, *, preserve_original: bool) -> ProcessedImage:
        """Sniff, validate and (unless preserving) strip metadata from ``data``."""
        image_type = self._sniff(data)
        # Pillow runs its own process-global bomb check inside Image.open(),
        # i.e. before our header check can run, warning above MAX_IMAGE_PIXELS
        # and raising above twice that. Escalate the warning locally so it
        # maps to a stable rejection instead of leaking; never mutate the
        # global. catch_warnings() is not thread-safe before Python 3.14's
        # context-aware warnings: under a race the warning may merely be
        # emitted (not escalated); our width*height check below still enforces
        # the configured limit, so the race can't bypass it.
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            try:
                return self._decode_and_encode(data, image_type, preserve_original)
            except UnsupportedMediaError:
                raise
            except (Image.DecompressionBombError, Image.DecompressionBombWarning):
                raise UnsupportedMediaError("media_too_many_pixels") from None
            except _DECODE_ERRORS:
                raise UnsupportedMediaError("media_unreadable") from None

    def _sniff(self, data: bytes) -> _ImageType:
        if data.startswith(b"\xff\xd8\xff"):
            return _JPEG
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            return _PNG
        if data[:6] in {b"GIF87a", b"GIF89a"}:
            return _GIF
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return _WEBP
        if data[4:8] == b"ftyp":
            brands = _isobmff_brands(data)
            if brands & _AVIF_BRANDS:
                if self._avif_supported:
                    return _AVIF
                raise UnsupportedMediaError("media_type_not_allowed")
            if brands & _OTHER_ISOBMFF_IMAGE_BRANDS:
                raise UnsupportedMediaError("media_type_not_allowed")
        if data.startswith(_DISALLOWED_IMAGE_SIGNATURES) or _looks_like_svg(data):
            raise UnsupportedMediaError("media_type_not_allowed")
        raise UnsupportedMediaError("media_unreadable")

    def _decode_and_encode(
        self, data: bytes, image_type: _ImageType, preserve_original: bool
    ) -> ProcessedImage:
        image = Image.open(BytesIO(data), formats=[image_type.pillow_format])
        if image.format not in _ACCEPTED_PILLOW_FORMATS[image_type.pillow_format]:
            raise UnsupportedMediaError("media_unreadable")
        width, height = image.size
        # Header-only dimensions are checked before any pixel data is decoded.
        if width < 1 or height < 1:
            raise UnsupportedMediaError("media_unreadable")
        if width * height > self._max_pixels:
            raise UnsupportedMediaError("media_too_many_pixels")
        frames = [_loaded_copy(frame) for frame in _frames(image)]
        if preserve_original:
            return ProcessedImage(
                data=data,
                content_type=image_type.content_type,
                extension=image_type.extension,
                width=width,
                height=height,
            )
        encoded, out_width, out_height = _encode(image, frames, image_type, data)
        return ProcessedImage(
            data=encoded,
            content_type=image_type.content_type,
            extension=image_type.extension,
            width=out_width,
            height=out_height,
        )


def _frames(image: Image.Image) -> Iterator[Image.Image]:
    # A multi-picture JPEG (MPO) is stored as a single still JPEG.
    if image.format == "MPO" or not getattr(image, "is_animated", False):
        image.load()
        yield image
        return
    yield from ImageSequence.Iterator(image)


def _loaded_copy(frame: Image.Image) -> Image.Image:
    """Fully decode one frame (rejecting truncation) and orient it."""
    frame.load()
    oriented = ImageOps.exif_transpose(frame)
    oriented.info = {
        key: value for key, value in frame.info.items() if key in _KEPT_INFO_KEYS
    }
    return oriented


def _encode(
    source: Image.Image,
    frames: list[Image.Image],
    image_type: _ImageType,
    original: bytes,
) -> tuple[bytes, int, int]:
    first = frames[0]
    params: dict[str, object] = {}
    icc_profile = source.info.get("icc_profile")
    if icc_profile and image_type is not _GIF:
        params["icc_profile"] = icc_profile
    if image_type is _JPEG:
        params["quality"] = _LOSSY_QUALITY
        # Equivalent to subsampling="keep", which Pillow only accepts on the
        # unmodified JpegImageFile (not on an oriented copy or an MPO).
        sampling = JpegImagePlugin.get_sampling(source)
        if sampling != -1:
            params["subsampling"] = sampling
    elif image_type is _WEBP:
        if _webp_is_lossless(original):
            params["lossless"] = True
        else:
            params["quality"] = _LOSSY_QUALITY
    elif image_type is _AVIF:
        params["quality"] = _LOSSY_QUALITY
    if len(frames) > 1:
        params["save_all"] = True
        params["append_images"] = frames[1:]
        params["duration"] = [frame.info.get("duration", 0) for frame in frames]
        params["loop"] = source.info.get("loop", 0)
    buffer = BytesIO()
    first.save(buffer, image_type.pillow_format, **params)
    return buffer.getvalue(), first.width, first.height


def _isobmff_brands(data: bytes) -> frozenset[bytes]:
    """Return the major and compatible brands of an ISO-BMFF ``ftyp`` box."""
    if len(data) < 16:  # noqa: PLR2004 - size(4) + "ftyp"(4) + major(4) + minor(4)
        return frozenset()
    box_size = int.from_bytes(data[0:4], "big")
    end = min(max(box_size, 16), len(data))
    brands = {data[8:12]}
    brands.update(data[offset : offset + 4] for offset in range(16, end - 3, 4))
    return frozenset(brands)


def _looks_like_svg(data: bytes) -> bool:
    head = data[:_SVG_PROBE_BYTES].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    return head.startswith((b"<?xml", b"<svg", b"<!doctype svg")) and (b"<svg" in head)


def _webp_is_lossless(data: bytes) -> bool:
    """Return True when every bitstream in a WebP RIFF container is VP8L."""
    kinds: list[bytes] = []
    offset = 12
    while offset + 8 <= len(data):
        chunk_id = data[offset : offset + 4]
        size = int.from_bytes(data[offset + 4 : offset + 8], "little")
        payload = offset + 8
        if chunk_id in {b"VP8 ", b"VP8L"}:
            kinds.append(chunk_id)
        elif chunk_id == b"ANMF":
            kinds.extend(_anmf_bitstream_kinds(data, payload + 16, payload + size))
        offset = payload + size + (size & 1)
    return bool(kinds) and all(kind == b"VP8L" for kind in kinds)


def _anmf_bitstream_kinds(data: bytes, start: int, end: int) -> list[bytes]:
    kinds: list[bytes] = []
    offset = start
    while offset + 8 <= min(end, len(data)):
        chunk_id = data[offset : offset + 4]
        size = int.from_bytes(data[offset + 4 : offset + 8], "little")
        if chunk_id in {b"VP8 ", b"VP8L"}:
            kinds.append(chunk_id)
        offset += 8 + size + (size & 1)
    return kinds
