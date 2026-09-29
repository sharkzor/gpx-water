"""Minimale decoder voor Mapbox Vector Tiles (MVT, protobuf).

Alleen wat nodig is om BGT-tegels van PDOK te lezen: lagen, features met hun
attributen en (multi)polygonen. Zo hoeft er geen extra afhankelijkheid
(protobuf + mapbox-vector-tile) in de container voor ~100 regels code.

Specificatie: https://github.com/mapbox/vector-tile-spec/tree/master/2.1
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Iterator

#: Geometrietypen uit de specificatie.
POINT, LINESTRING, POLYGON = 1, 2, 3


class MvtError(ValueError):
    """De tegel is geen geldige vector tile."""


@dataclass(slots=True)
class Feature:
    type: int
    properties: dict[str, object]
    #: Ringen (polygonen) of lijnen in tegelcoördinaten (0..extent, y naar beneden).
    rings: list[list[tuple[int, int]]] = field(default_factory=list)


@dataclass(slots=True)
class Layer:
    name: str
    extent: int
    features: list[Feature]


def _varint(data: bytes, pos: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        if pos >= len(data):
            raise MvtError("Afgebroken varint")
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            raise MvtError("Varint te lang")


def _fields(data: bytes) -> Iterator[tuple[int, int, object]]:
    """Loop door de velden van een protobuf-bericht: (nummer, type, waarde)."""
    pos = 0
    while pos < len(data):
        key, pos = _varint(data, pos)
        number, wire = key >> 3, key & 7
        if wire == 0:
            value, pos = _varint(data, pos)
        elif wire == 1:
            value, pos = data[pos : pos + 8], pos + 8
        elif wire == 2:
            size, pos = _varint(data, pos)
            value, pos = data[pos : pos + size], pos + size
        elif wire == 5:
            value, pos = data[pos : pos + 4], pos + 4
        else:
            raise MvtError(f"Onbekend wire type {wire}")
        if pos > len(data):
            raise MvtError("Afgebroken bericht")
        yield number, wire, value


def _packed(data: bytes) -> list[int]:
    values: list[int] = []
    pos = 0
    while pos < len(data):
        value, pos = _varint(data, pos)
        values.append(value)
    return values


def _zigzag(value: int) -> int:
    return (value >> 1) ^ -(value & 1)


def _value(data: bytes) -> object:
    for number, _wire, raw in _fields(data):
        if number == 1:
            return raw.decode("utf-8", "replace")  # type: ignore[union-attr]
        if number == 2:
            return struct.unpack("<f", raw)[0]  # type: ignore[arg-type]
        if number == 3:
            return struct.unpack("<d", raw)[0]  # type: ignore[arg-type]
        if number in (4, 5):
            return raw
        if number == 6:
            return _zigzag(raw)  # type: ignore[arg-type]
        if number == 7:
            return bool(raw)
    return None


def _geometry(commands: list[int]) -> list[list[tuple[int, int]]]:
    """Zet MVT-tekencommando's om in ringen of lijnen."""
    parts: list[list[tuple[int, int]]] = []
    current: list[tuple[int, int]] = []
    x = y = 0
    i = 0
    while i < len(commands):
        command, count = commands[i] & 7, commands[i] >> 3
        i += 1
        if command == 7:  # ClosePath
            if current:
                current.append(current[0])
            continue
        if command not in (1, 2):
            raise MvtError(f"Onbekend geometriecommando {command}")
        if i + 2 * count > len(commands):
            raise MvtError("Afgebroken geometrie")
        for _ in range(count):
            x += _zigzag(commands[i])
            y += _zigzag(commands[i + 1])
            i += 2
            if command == 1:  # MoveTo: begin een nieuwe ring of lijn
                if current:
                    parts.append(current)
                current = [(x, y)]
            else:
                current.append((x, y))
    if current:
        parts.append(current)
    return parts


def _layer(data: bytes, wanted: set[str] | None) -> Layer | None:
    name = ""
    extent = 4096
    keys: list[str] = []
    values: list[object] = []
    raw_features: list[bytes] = []
    for number, _wire, raw in _fields(data):
        if number == 1:
            name = raw.decode("utf-8", "replace")  # type: ignore[union-attr]
            if wanted is not None and name not in wanted:
                return None
        elif number == 2:
            raw_features.append(raw)  # type: ignore[arg-type]
        elif number == 3:
            keys.append(raw.decode("utf-8", "replace"))  # type: ignore[union-attr]
        elif number == 4:
            values.append(_value(raw))  # type: ignore[arg-type]
        elif number == 5:
            extent = int(raw)  # type: ignore[arg-type]

    features: list[Feature] = []
    for raw in raw_features:
        tags: list[int] = []
        commands: list[int] = []
        geom_type = 0
        for number, _wire, value in _fields(raw):
            if number == 2:
                tags = _packed(value)  # type: ignore[arg-type]
            elif number == 3:
                geom_type = int(value)  # type: ignore[arg-type]
            elif number == 4:
                commands = _packed(value)  # type: ignore[arg-type]
        properties: dict[str, object] = {}
        for k, v in zip(tags[::2], tags[1::2]):
            if k < len(keys) and v < len(values):
                properties[keys[k]] = values[v]
        features.append(Feature(geom_type, properties, _geometry(commands)))
    return Layer(name, extent, features)


def decode(data: bytes, layers: set[str] | None = None) -> dict[str, Layer]:
    """Decodeer een tegel; optioneel alleen de lagen in `layers`."""
    result: dict[str, Layer] = {}
    for number, wire, raw in _fields(data):
        if number == 3 and wire == 2:
            layer = _layer(raw, layers)  # type: ignore[arg-type]
            if layer is not None:
                result[layer.name] = layer
    return result


def signed_area(ring: list[tuple[int, int]]) -> float:
    """Oppervlakte met teken (shoelace); in MVT is een buitenring positief."""
    total = 0
    for (x1, y1), (x2, y2) in zip(ring, ring[1:]):
        total += x1 * y2 - x2 * y1
    return total / 2.0
