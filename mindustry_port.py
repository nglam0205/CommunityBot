"""Small, dependency-light Mindustry map and schematic reader/render helper.

This module reads Mindustry's binary formats without starting a game instance.
It is intended for inspection and preview images, not for editing/re-saving game
files. Map previews use a configurable name-to-color palette; exact game colors
and sprite rendering require Mindustry's content/assets registry.
"""

from __future__ import annotations

import base64
import io
import json
import struct
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, Iterable, Mapping

try:
    from PIL import Image, ImageDraw
except ImportError as exc:  # pragma: no cover - import-time guidance
    raise ImportError("mindustry_port requires Pillow; install it with `pip install Pillow`.") from exc


MAX_DIMENSION = 4096
MAX_TILES = 128 * 128
MAX_REGION_BYTES = 512 * 1024 * 1024


@dataclass(slots=True)
class SchematicTile:
    x: int
    y: int
    block: str
    rotation: int = 0
    config: Any = None


@dataclass(slots=True)
class Schematic:
    width: int
    height: int
    tags: dict[str, str] = field(default_factory=dict)
    labels: list[str] = field(default_factory=list)
    tiles: list[SchematicTile] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.tags.get("name", "unknown")

    @property
    def description(self) -> str:
        return self.tags.get("description", "")


@dataclass(slots=True)
class MapTile:
    x: int
    y: int
    floor_id: int = 0
    floor: str = "unknown"
    overlay_id: int = 0
    overlay: str = "air"
    block_id: int = 0
    block: str = "air"


@dataclass(slots=True)
class MapData:
    width: int
    height: int
    version: int
    tags: dict[str, str] = field(default_factory=dict)
    tiles: list[MapTile] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.tags.get("name", self.tags.get("mapname", "unknown"))

    def tile_at(self, x: int, y: int) -> MapTile:
        if not (0 <= x < self.width and 0 <= y < self.height):
            raise IndexError((x, y))
        return self.tiles[y * self.width + x]


class FormatError(ValueError):
    """Malformed, unsupported, or unexpectedly truncated Mindustry data."""


class _Reader:
    def __init__(self, data: bytes):
        self.data = memoryview(data)
        self.pos = 0

    def take(self, size: int) -> bytes:
        if size < 0 or self.pos + size > len(self.data):
            raise FormatError("Unexpected end of Mindustry data")
        out = self.data[self.pos:self.pos + size].tobytes()
        self.pos += size
        return out

    def unpack(self, fmt: str) -> tuple[Any, ...]:
        size = struct.calcsize(fmt)
        if self.pos + size > len(self.data):
            raise FormatError("Unexpected end of Mindustry data")
        out = struct.unpack_from(fmt, self.data, self.pos)
        self.pos += size
        return out

    def u8(self) -> int:
        return self.unpack(">B")[0]

    def i8(self) -> int:
        return self.unpack(">b")[0]

    def u16(self) -> int:
        return self.unpack(">H")[0]

    def i16(self) -> int:
        return self.unpack(">h")[0]

    def i32(self) -> int:
        return self.unpack(">i")[0]

    def u32(self) -> int:
        return self.unpack(">I")[0]

    def java_utf(self) -> str:
        """Read DataInput.readUTF / Arc Reads.str (modified UTF-8)."""
        raw = self.take(self.u16())
        # Java modified UTF-8 encodes NUL as C0 80 and supplementary characters
        # as UTF-16 surrogate pairs. Decode bytes into UTF-16 code units first.
        units: list[int] = []
        i = 0
        while i < len(raw):
            a = raw[i]
            if a & 0x80 == 0:
                units.append(a)
                i += 1
            elif a & 0xE0 == 0xC0 and i + 1 < len(raw):
                b = raw[i + 1]
                if b & 0xC0 != 0x80:
                    raise FormatError("Invalid modified UTF-8 string")
                units.append(((a & 0x1F) << 6) | (b & 0x3F))
                i += 2
            elif a & 0xF0 == 0xE0 and i + 2 < len(raw):
                b, c = raw[i + 1], raw[i + 2]
                if b & 0xC0 != 0x80 or c & 0xC0 != 0x80:
                    raise FormatError("Invalid modified UTF-8 string")
                units.append(((a & 0x0F) << 12) | ((b & 0x3F) << 6) | (c & 0x3F))
                i += 3
            else:
                raise FormatError("Invalid modified UTF-8 string")
        encoded = b"".join(struct.pack(">H", unit) for unit in units)
        return encoded.decode("utf-16-be", "surrogatepass").encode("utf-16-be", "surrogatepass").decode("utf-16-be", "replace")

    def string_map(self) -> dict[str, str]:
        count = self.u16()
        if count > 4096:
            raise FormatError("Unreasonable metadata tag count")
        return {self.java_utf(): self.java_utf() for _ in range(count)}

    def chunk(self) -> bytes:
        length = self.i32()
        if length < 0 or length > MAX_REGION_BYTES:
            raise FormatError(f"Invalid region length: {length}")
        return self.take(length)


def _read_bytes(source: str | Path | bytes | bytearray | BinaryIO) -> bytes:
    if isinstance(source, (bytes, bytearray)):
        return bytes(source)
    if hasattr(source, "read"):
        return source.read()
    return Path(source).read_bytes()


def _inflate(data: bytes) -> bytes:
    # Java DeflaterOutputStream uses the zlib wrapper by default. Also accept
    # raw DEFLATE streams to be tolerant of alternative export tools.
    try:
        return zlib.decompress(data)
    except zlib.error as first:
        try:
            return zlib.decompress(data, -zlib.MAX_WBITS)
        except zlib.error:
            raise FormatError("Invalid or unsupported DEFLATE data") from first


def _bounded_dimensions(width: int, height: int) -> None:
    if width <= 0 or height <= 0 or width > MAX_DIMENSION or height > MAX_DIMENSION:
        raise FormatError(f"Invalid dimensions: {width}x{height}")


def _unpack_point(position: int) -> tuple[int, int]:
    x = position & 0xFFFF
    y = (position >> 16) & 0xFFFF
    return (x - 0x10000 if x & 0x8000 else x,
            y - 0x10000 if y & 0x8000 else y)


def _read_typeio_object(reader: _Reader, depth: int = 0) -> Any:
    """Decode the TypeIO object subset used in schematics (Mindustry TypeIO)."""
    if depth > 8:
        raise FormatError("Nested schematic config is too deep")
    kind = reader.u8()
    if kind == 0:
        return None
    if kind == 1:
        return reader.i32()
    if kind == 2:
        return reader.unpack(">q")[0]
    if kind == 3:
        return reader.unpack(">f")[0]
    if kind == 4:
        return reader.java_utf() if reader.u8() else None
    if kind == 5:
        return {"type": "content", "content_type": reader.u8(), "id": reader.i16()}
    if kind == 6:
        count = reader.i16()
        if not 0 <= count <= 1000:
            raise FormatError("Invalid int-sequence length")
        return [reader.i32() for _ in range(count)]
    if kind == 7:
        return {"x": reader.i32(), "y": reader.i32()}
    if kind == 8:
        count = reader.u8()
        points = []
        for _ in range(count):
            x, y = _unpack_point(reader.i32())
            points.append({"x": x, "y": y})
        return points
    if kind == 9:
        return {"type": "tech_node", "content_type": reader.u8(), "id": reader.i16()}
    if kind == 10:
        return bool(reader.u8())
    if kind == 11:
        return reader.unpack(">d")[0]
    if kind in (12, 17):
        return {"type": "building" if kind == 12 else "unit", "id": reader.i32()}
    if kind == 13:
        return {"type": "logic_access", "id": reader.i16()}
    if kind == 14:
        length = reader.i32()
        if not 0 <= length <= 40_000:
            raise FormatError("Invalid byte-array config length")
        return reader.take(length)
    if kind == 15:
        reader.u8()
        return None
    if kind == 16:
        length = reader.i32()
        if not 0 <= length <= 1000:
            raise FormatError("Invalid boolean-array config length")
        return [bool(reader.u8()) for _ in range(length)]
    if kind == 18:
        length = reader.i16()
        if not 0 <= length <= 1000:
            raise FormatError("Invalid vector-array config length")
        return [[reader.unpack(">f")[0], reader.unpack(">f")[0]] for _ in range(length)]
    if kind == 19:
        return [reader.unpack(">f")[0], reader.unpack(">f")[0]]
    if kind == 20:
        return {"type": "team", "id": reader.u8()}
    if kind == 21:
        length = reader.i16()
        if not 0 <= length <= 1000:
            raise FormatError("Invalid int-array config length")
        return [reader.i32() for _ in range(length)]
    if kind == 22:
        length = reader.i32()
        if not 0 <= length <= 1000:
            raise FormatError("Invalid object-array config length")
        return [_read_typeio_object(reader, depth + 1) for _ in range(length)]
    if kind == 23:
        return {"type": "unit_command", "id": reader.i16()}
    raise FormatError(f"Unknown schematic config type: {kind}")


def read_schematic(source: str | Path | bytes | bytearray | BinaryIO) -> Schematic:
    """Read a `.msch` file or raw schematic bytes into Python dataclasses."""
    blob = _read_bytes(source)
    if len(blob) < 5 or blob[:4] != b"msch":
        raise FormatError("Not a Mindustry schematic (missing msch header)")
    version = blob[4]
    if version > 1:
        raise FormatError(f"Unsupported schematic version: {version}")
    reader = _Reader(_inflate(blob[5:]))
    width, height = reader.i16(), reader.i16()
    _bounded_dimensions(width, height)

    tags: dict[str, str] = {}
    for _ in range(reader.u8()):
        tags[reader.java_utf()] = reader.java_utf()
    try:
        labels = json.loads(tags.get("labels", "[]"))
        if not isinstance(labels, list):
            labels = []
    except (TypeError, json.JSONDecodeError):
        labels = []

    block_names = [reader.java_utf() for _ in range(reader.u8())]
    total = reader.i32()
    if not 0 <= total <= MAX_TILES:
        raise FormatError(f"Invalid schematic tile count: {total}")

    tiles: list[SchematicTile] = []
    for _ in range(total):
        block_index = reader.i8()
        if block_index < 0 or block_index >= len(block_names):
            raise FormatError(f"Invalid block dictionary index: {block_index}")
        x, y = _unpack_point(reader.i32())
        config = reader.i32() if version == 0 else _read_typeio_object(reader)
        rotation = reader.i8()
        block = block_names[block_index]
        if block != "air":
            tiles.append(SchematicTile(x, y, block, rotation, config))
    tags["labels"] = json.dumps(labels, ensure_ascii=False)
    return Schematic(width, height, tags, labels, tiles)


def read_schematic_base64(value: str) -> Schematic:
    """Read the base64 schematic string used by Mindustry share/copy actions."""
    text = value.strip()
    if text.startswith("bXNjaAB"):
        text = text[7:]  # optional share prefix: base64("msch\0...")
    try:
        raw = base64.b64decode(text, validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise FormatError("Invalid base64 schematic") from exc
    # Mindustry's actual serialized schematic base64 includes the msch header.
    # Some callers pass the payload after the conventional bXNjaAB marker.
    if raw[:4] != b"msch" and value.strip().startswith("bXNjaAB"):
        raw = b"msch\x00" + raw
    return read_schematic(raw)


def _read_content_names(chunk: bytes) -> dict[int, str]:
    """Read block-name table from the save content region (ContentType.block=1)."""
    reader = _Reader(chunk)
    mapping: dict[int, str] = {}
    groups = reader.u8()
    for _ in range(groups):
        content_type = reader.i8()
        count = reader.i16()
        if not 0 <= count <= 32767:
            raise FormatError("Invalid content mapping count")
        names = [reader.java_utf() for _ in range(count)]
        if content_type == 1:
            mapping.update(enumerate(names))
    return mapping


def _save_regions(blob: bytes) -> tuple[int, list[bytes]]:
    reader = _Reader(_inflate(blob))
    if reader.take(4) != b"MSAV":
        raise FormatError("Not a Mindustry save/map file (missing MSAV header)")
    version = reader.i32()
    if version < 1 or version > 13:
        raise FormatError(f"Unsupported Mindustry save version: {version}")
    regions: list[bytes] = []
    # Mindustry save versions 1-10: meta/content/map; 11: meta/content/patches/map;
    # 12+: meta/patches/content/map. Region lengths permit safe skipping.
    region_names = ["meta"]
    if version == 11:
        region_names += ["content", "patches", "map"]
    elif version >= 12:
        region_names += ["patches", "content", "map"]
    else:
        region_names += ["content", "map"]
    for _ in region_names:
        regions.append(reader.chunk())
    return version, regions


def read_map(source: str | Path | bytes | bytearray | BinaryIO) -> MapData:
    """Read map metadata and tile IDs/names from a `.msav` save/map file.

    The parser supports the current v13 format and the region layouts for older
    Mindustry save versions. It does not instantiate buildings or decode their
    game-specific entity payloads.
    """
    blob = _read_bytes(source)
    version, regions = _save_regions(blob)
    meta = _Reader(regions[0])
    tags = meta.string_map()
    width = int(tags.get("width", "0"))
    height = int(tags.get("height", "0"))
    _bounded_dimensions(width, height)
    total = width * height
    if total > 16_000_000:
        raise FormatError("Map is too large to load safely")

    if version == 11:
        content_region, map_region = regions[1], regions[3]
    elif version >= 12:
        content_region, map_region = regions[2], regions[3]
    else:
        content_region, map_region = regions[1], regions[2]
    content_names = _read_content_names(content_region)

    reader = _Reader(map_region)
    map_width, map_height = reader.u16(), reader.u16()
    if (map_width, map_height) != (width, height):
        # Dimensions from the map payload are authoritative if metadata is absent
        # or stale, but mismatch is rejected as corrupt data for safety.
        if width == 0 or height == 0:
            width, height, total = map_width, map_height, map_width * map_height
        else:
            raise FormatError("Map dimensions do not match metadata")
    if width <= 0 or height <= 0 or total > 16_000_000:
        raise FormatError("Invalid map dimensions")

    floors: list[tuple[int, int]] = []
    cursor = 0
    while cursor < total:
        floor_id, overlay_id, run = reader.i16(), reader.i16(), reader.u8()
        count = run + 1
        if cursor + count > total:
            raise FormatError("Floor run exceeds map dimensions")
        floors.extend([(floor_id, overlay_id)] * count)
        cursor += count

    blocks: list[int] = []
    cursor = 0
    while cursor < total:
        block_id = reader.i16()
        packed = reader.u8()
        has_entity = bool(packed & 1)
        has_old_data = bool(packed & 2)
        has_data = bool(packed & 4)
        if has_data:
            reader.take(3)  # block, floor, and overlay data bytes
            reader.take(4)  # extra data int
        is_center = True
        if has_entity:
            is_center = bool(reader.u8())
        blocks.append(block_id)
        cursor += 1
        if has_entity and is_center:
            length = reader.i32()
            if length < 0 or length > MAX_REGION_BYTES:
                raise FormatError("Invalid building entity chunk length")
            reader.take(length)
        elif has_old_data:
            reader.u8()
        elif not has_entity and not has_data:
            run = reader.u8()
            if cursor + run > total:
                raise FormatError("Block run exceeds map dimensions")
            blocks.extend([block_id] * run)
            cursor += run

    if len(blocks) != total or len(floors) != total:
        raise FormatError("Map tile data is incomplete")

    tiles = []
    for index, (floor_id, overlay_id) in enumerate(floors):
        block_id = blocks[index]
        x, y = index % width, index // width
        tiles.append(MapTile(
            x=x, y=y,
            floor_id=floor_id, floor=content_names.get(floor_id, f"block-{floor_id}"),
            overlay_id=overlay_id, overlay=content_names.get(overlay_id, "air" if overlay_id == 0 else f"block-{overlay_id}"),
            block_id=block_id, block=content_names.get(block_id, "air" if block_id == 0 else f"block-{block_id}"),
        ))
    return MapData(width, height, version, tags, tiles)


# Preview palette: visual approximations keyed by Mindustry content names.
# Callers can override/extend it via the `palette` argument to map_to_image.
DEFAULT_PALETTE: dict[str, tuple[int, int, int]] = {
    "air": (0, 0, 0), "stone": (93, 93, 93), "stone-wall": (76, 76, 76),
    "dirt": (111, 83, 58), "dirt-wall": (87, 67, 51), "sand": (198, 178, 126),
    "sand-floor": (198, 178, 126), "shale": (104, 98, 91), "shale-wall": (75, 70, 67),
    "salt": (197, 194, 177), "snow": (211, 221, 225), "ice": (121, 183, 199),
    "ice-snow": (167, 202, 207), "deep-water": (37, 83, 137), "shallow-water": (61, 128, 174),
    "tainted-water": (69, 133, 119), "water": (61, 128, 174), "stone-floor": (107, 107, 107),
    "crater-stone": (81, 76, 73), "dacite": (119, 106, 91), "basalt": (90, 79, 74),
    "moss": (67, 102, 74), "spore-moss": (96, 114, 75), "grass": (107, 130, 77),
    "dark-panel-1": (66, 75, 81), "dark-panel-2": (74, 84, 89),
    "copper-wall": (190, 107, 69), "copper-wall-large": (190, 107, 69),
    "titanium-wall": (127, 165, 185), "plastanium-wall": (155, 201, 168),
    "thorium-wall": (207, 123, 194), "surge-wall": (220, 198, 99),
    "spawn": (220, 64, 64), "ore-copper": (193, 113, 69), "ore-lead": (93, 105, 117),
    "ore-coal": (58, 58, 58), "ore-titanium": (109, 176, 202), "ore-thorium": (191, 111, 184),
}


def _color_for_tile(tile: MapTile, palette: Mapping[str, tuple[int, int, int]]) -> tuple[int, int, int]:
    if tile.block != "air":
        color = palette.get(tile.block)
        if color is not None:
            return color
    if tile.overlay != "air":
        color = palette.get(tile.overlay)
        if color is not None:
            return color
    return palette.get(tile.floor, (86, 86, 86))


def map_to_image(map_data: MapData, *, palette: Mapping[str, tuple[int, int, int]] | None = None,
                  scale: int = 1) -> Image.Image:
    """Render a fast palette-based map preview. Returns an RGB Pillow image."""
    if scale < 1:
        raise ValueError("scale must be >= 1")
    colors = palette or DEFAULT_PALETTE
    image = Image.new("RGB", (map_data.width, map_data.height))
    pixels = image.load()
    for tile in map_data.tiles:
        pixels[tile.x, map_data.height - 1 - tile.y] = _color_for_tile(tile, colors)
    if scale != 1:
        image = image.resize((map_data.width * scale, map_data.height * scale), Image.Resampling.NEAREST)
    return image


def schematic_to_image(schematic: Schematic, *, palette: Mapping[str, tuple[int, int, int]] | None = None,
                       cell_size: int = 24, background: tuple[int, int, int] = (28, 30, 34),
                       grid: bool = True) -> Image.Image:
    """Render schematic tiles as a grid preview (not the game's actual sprites)."""
    if cell_size < 2:
        raise ValueError("cell_size must be >= 2")
    _bounded_dimensions(schematic.width, schematic.height)
    colors = palette or DEFAULT_PALETTE
    image = Image.new("RGB", (schematic.width * cell_size, schematic.height * cell_size), background)
    draw = ImageDraw.Draw(image)
    for tile in schematic.tiles:
        if not (0 <= tile.x < schematic.width and 0 <= tile.y < schematic.height):
            continue
        x = tile.x * cell_size
        y = (schematic.height - 1 - tile.y) * cell_size
        color = colors.get(tile.block, _stable_block_color(tile.block))
        inset = max(1, cell_size // 10)
        draw.rectangle((x + inset, y + inset, x + cell_size - inset - 1, y + cell_size - inset - 1), fill=color)
        # Small direction mark for blocks whose orientation is useful to see.
        if tile.rotation:
            cx, cy = x + cell_size // 2, y + cell_size // 2
            radius = max(2, cell_size // 5)
            endpoints = [(cx, cy - radius), (cx + radius, cy), (cx, cy + radius), (cx - radius, cy)]
            ex, ey = endpoints[tile.rotation % 4]
            draw.line((cx, cy, ex, ey), fill=(245, 245, 245), width=max(1, cell_size // 12))
    if grid and cell_size >= 8:
        for x in range(0, image.width, cell_size):
            draw.line((x, 0, x, image.height), fill=(10, 11, 13), width=1)
        for y in range(0, image.height, cell_size):
            draw.line((0, y, image.width, y), fill=(10, 11, 13), width=1)
    return image


def _stable_block_color(name: str) -> tuple[int, int, int]:
    # Deterministic muted color for unknown/modded blocks.
    value = 2166136261
    for byte in name.encode("utf-8"):
        value = ((value ^ byte) * 16777619) & 0xFFFFFFFF
    return (80 + (value & 0x7F), 80 + ((value >> 8) & 0x7F), 80 + ((value >> 16) & 0x7F))


def image_to_buffer(image: Image.Image, format: str = "PNG", *, quality: int = 90) -> io.BytesIO:
    """Encode a Pillow image into a rewound in-memory buffer (PNG/JPEG/etc.)."""
    output = io.BytesIO()
    fmt = format.upper()
    if fmt in {"JPG", "JPEG"}:
        image.convert("RGB").save(output, format="JPEG", quality=quality)
    else:
        image.save(output, format=fmt)
    output.seek(0)
    return output


def save_image(image: Image.Image, destination: str | Path | BinaryIO, format: str | None = None,
               *, quality: int = 90) -> None:
    """Save an image to a path or writable stream, selecting PNG/JPEG by format."""
    fmt = (format or (Path(destination).suffix.lstrip(".") if isinstance(destination, (str, Path)) else "PNG")).upper()
    if fmt in {"JPG", "JPEG"}:
        image.convert("RGB").save(destination, format="JPEG", quality=quality)
    else:
        image.save(destination, format=fmt)


def map_file_to_image(source: str | Path | bytes | bytearray | BinaryIO, **kwargs: Any) -> Image.Image:
    return map_to_image(read_map(source), **kwargs)


def schematic_file_to_image(source: str | Path | bytes | bytearray | BinaryIO, **kwargs: Any) -> Image.Image:
    return schematic_to_image(read_schematic(source), **kwargs)


__all__ = [
    "FormatError", "MapData", "MapTile", "Schematic", "SchematicTile",
    "read_map", "read_schematic", "read_schematic_base64",
    "map_to_image", "schematic_to_image", "map_file_to_image",
    "schematic_file_to_image", "image_to_buffer", "save_image", "DEFAULT_PALETTE",
]
