from __future__ import annotations

import colorsys
import json
import math
import os
import queue
import random
import shutil
import string
import struct
import sys
import tempfile
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request
from bisect import bisect_left
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
import xml.etree.ElementTree as ET


APP_DIR = Path(__file__).resolve().parent
VERSION_PATH = APP_DIR / "VERSION"
TEMPLATE_PATH = APP_DIR / "assets" / "xoss_nav_template.ro"
SETTINGS_FILENAME = "settings.json"
DEFAULT_SPLIT_KM = 300.0
MIN_OUTPUT_POINTS = 1_000
MAX_OUTPUT_POINTS = 24_000
DEFAULT_OUTPUT_POINTS = 15_000
EARTH_RADIUS_M = 6_378_137.0
GPX_NS = "http://www.topografix.com/GPX/1/1"
MAP_TILE_SIZE = 256
MAP_MIN_ZOOM = 2
MAP_MAX_ZOOM = 17
MAP_CACHE_DIR = Path(tempfile.gettempdir()) / "xoss_route_converter_map_tiles"
MAP_TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
MAP_ROUTE_COLORS = (
    "#d83b49",
    "#2f6fed",
    "#e08a00",
    "#1b9e77",
    "#8e5bd9",
    "#c4528c",
    "#008c95",
    "#7a8b3e",
)
RID_MIN = 100000
RID_MAX = 999999


def read_app_version() -> str:
    try:
        version = VERSION_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        version = "dev"
    return version or "dev"


APP_VERSION = read_app_version()


def runtime_directory() -> Path:
    """Return the directory beside the script or packaged executable."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return APP_DIR


def read_window_settings(path: Path) -> dict:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return document if isinstance(document, dict) else {}


def write_window_settings(path: Path, geometry: str, split_mode: str) -> None:
    document = {
        "geometry": geometry,
        "split_mode": split_mode,
    }
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


@dataclass
class TrackPoint:
    lat: float
    lon: float
    ele: float | None = None


@dataclass
class GpxWaypoint:
    name: str
    lat: float
    lon: float
    comment: str = ""
    description: str = ""
    waypoint_type: str = ""


@dataclass
class RoutePart:
    number: int
    points: list[TrackPoint]
    distance_m: float
    gain_m: int
    label: str = ""


def haversine_m(a: TrackPoint, b: TrackPoint) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (a.lat, a.lon, b.lat, b.lon))
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2.0 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(h)))


def track_distance(points: list[TrackPoint]) -> float:
    return sum(haversine_m(a, b) for a, b in zip(points, points[1:]))


def elevation_gain(points: list[TrackPoint]) -> int:
    gain = 0.0
    for a, b in zip(points, points[1:]):
        if a.ele is not None and b.ele is not None and b.ele > a.ele:
            gain += b.ele - a.ele
    return max(0, int(round(gain)))


def interpolate(a: TrackPoint, b: TrackPoint, ratio: float) -> TrackPoint:
    ratio = max(0.0, min(1.0, ratio))
    ele = None
    if a.ele is not None and b.ele is not None:
        ele = a.ele + (b.ele - a.ele) * ratio
    elif a.ele is not None:
        ele = a.ele
    elif b.ele is not None:
        ele = b.ele
    return TrackPoint(
        lat=a.lat + (b.lat - a.lat) * ratio,
        lon=a.lon + (b.lon - a.lon) * ratio,
        ele=ele,
    )


def parse_gpx_with_waypoints(path: Path) -> tuple[str, list[TrackPoint], list[GpxWaypoint]]:
    root = ET.parse(path).getroot()
    track_elements = [element for element in root.iter() if element.tag.endswith("trkpt")]
    point_elements = track_elements or [element for element in root.iter() if element.tag.endswith("rtept")]
    points: list[TrackPoint] = []
    for element in point_elements:
        try:
            lat = float(element.attrib["lat"])
            lon = float(element.attrib["lon"])
        except (KeyError, ValueError) as exc:
            raise ValueError(f"緯度・経度を読めない点があります: {exc}") from exc
        ele = None
        for child in element:
            if child.tag.endswith("ele") and child.text:
                try:
                    ele = float(child.text)
                except ValueError:
                    ele = None
                break
        points.append(TrackPoint(lat, lon, ele))

    if len(points) < 2:
        raise ValueError("GPXに2点以上のtrkpt/rteptがありません。")

    title = ""
    for element in root.iter():
        if element.tag.endswith("name") and element.text and element.text.strip():
            title = element.text.strip()
            break
    if not title:
        title = path.stem

    waypoints: list[GpxWaypoint] = []
    for element in root.iter():
        if not element.tag.endswith("wpt"):
            continue
        try:
            lat = float(element.attrib["lat"])
            lon = float(element.attrib["lon"])
        except (KeyError, ValueError):
            continue
        waypoints.append(
            GpxWaypoint(
                name=(element.findtext("{*}name") or "").strip(),
                lat=lat,
                lon=lon,
                comment=(element.findtext("{*}cmt") or "").strip(),
                description=(element.findtext("{*}desc") or "").strip(),
                waypoint_type=(element.findtext("{*}type") or "").strip(),
            )
        )
    return title, points, waypoints


def parse_gpx(path: Path) -> tuple[str, list[TrackPoint]]:
    """Read a GPX route while preserving the original two-value API."""
    title, points, _waypoints = parse_gpx_with_waypoints(path)
    return title, points


def is_checkpoint_waypoint(waypoint: GpxWaypoint) -> bool:
    """Recognize controls and named pass-through checkpoints in common GPX exports."""
    name = waypoint.name.casefold()
    comment = waypoint.comment.casefold()
    waypoint_type = waypoint.waypoint_type.casefold()
    return (
        waypoint_type == "checkpoint"
        or comment == "control"
        or name.startswith("pc")
        or waypoint.name.startswith("通過")
    )


def checkpoint_waypoints(waypoints: list[GpxWaypoint]) -> list[GpxWaypoint]:
    """Return PC and pass-through checkpoint candidates, excluding ordinary waypoints."""
    return [waypoint for waypoint in waypoints if is_checkpoint_waypoint(waypoint)]


def split_track(points: list[TrackPoint], max_distance_m: float | None) -> list[RoutePart]:
    if len(points) < 2:
        raise ValueError("ルートには2点以上必要です。")
    if max_distance_m is None:
        return [RoutePart(1, list(points), track_distance(points), elevation_gain(points))]
    if max_distance_m <= 0:
        raise ValueError("分割距離は0より大きくしてください。")

    parts: list[list[TrackPoint]] = []
    current = [points[0]]
    remaining = max_distance_m

    for start, end in zip(points, points[1:]):
        cursor = start
        segment_remaining = haversine_m(start, end)
        if segment_remaining == 0:
            continue

        while segment_remaining > remaining + 1e-7:
            ratio = remaining / segment_remaining
            split_point = interpolate(cursor, end, ratio)
            current.append(split_point)
            parts.append(current)
            current = [split_point]
            cursor = split_point
            segment_remaining -= remaining
            remaining = max_distance_m

        current.append(end)
        remaining -= segment_remaining
        if remaining <= 1e-7:
            parts.append(current)
            current = [end]
            remaining = max_distance_m

    if len(current) >= 2:
        parts.append(current)
    elif not parts:
        parts.append(points[:])

    return [
        RoutePart(i, part, track_distance(part), elevation_gain(part))
        for i, part in enumerate(parts, start=1)
    ]


def _project_waypoint_to_segment(
    waypoint: GpxWaypoint,
    start: TrackPoint,
    end: TrackPoint,
) -> tuple[TrackPoint, float]:
    """Project a waypoint onto a short route segment using a local flat projection."""
    reference_lat = math.radians((waypoint.lat + start.lat + end.lat) / 3.0)
    scale_x = math.cos(reference_lat)
    start_x = (start.lon - waypoint.lon) * scale_x
    start_y = start.lat - waypoint.lat
    end_x = (end.lon - waypoint.lon) * scale_x
    end_y = end.lat - waypoint.lat
    delta_x = end_x - start_x
    delta_y = end_y - start_y
    denominator = delta_x * delta_x + delta_y * delta_y
    if denominator <= 0:
        ratio = 0.0
    else:
        ratio = (-(start_x * delta_x) - (start_y * delta_y)) / denominator
        ratio = max(0.0, min(1.0, ratio))
    projected = interpolate(start, end, ratio)
    return projected, haversine_m(projected, TrackPoint(waypoint.lat, waypoint.lon))


def _nearest_route_position(
    points: list[TrackPoint],
    waypoint: GpxWaypoint,
) -> tuple[float, TrackPoint, float]:
    """Return a waypoint's nearest fractional point index, projected point, and error."""
    best_distance = math.inf
    best_position = 0.0
    best_point = points[0]
    for index, (start, end) in enumerate(zip(points, points[1:])):
        projected, distance = _project_waypoint_to_segment(waypoint, start, end)
        if distance < best_distance:
            best_distance = distance
            best_position = index + haversine_m(start, projected) / max(haversine_m(start, end), 1e-9)
            best_point = projected
    return best_position, best_point, best_distance


def _point_at_route_position(points: list[TrackPoint], position: float) -> TrackPoint:
    if position <= 0:
        return points[0]
    if position >= len(points) - 1:
        return points[-1]
    index = min(len(points) - 2, max(0, math.floor(position)))
    return interpolate(points[index], points[index + 1], position - index)


def _points_between_route_positions(points: list[TrackPoint], start: float, end: float) -> list[TrackPoint]:
    """Slice a route between two fractional point indexes, retaining original points."""
    result = [_point_at_route_position(points, start)]
    first_original_index = math.floor(start) + 1
    last_original_index = math.ceil(end)
    result.extend(points[first_original_index:last_original_index])
    end_point = _point_at_route_position(points, end)
    if result[-1] != end_point:
        result.append(end_point)
    return result


def split_track_at_checkpoints(
    points: list[TrackPoint],
    checkpoints: list[GpxWaypoint],
) -> list[RoutePart]:
    """Split a route at GPX PC/pass-through waypoints in their route order."""
    if len(points) < 2:
        raise ValueError("ルートには2点以上必要です。")
    if not checkpoints:
        raise ValueError("GPXにチェックポイント（PCまたは通過チェック）がありません。")

    located: list[tuple[float, GpxWaypoint]] = []
    for waypoint in checkpoints:
        position, _projected, _distance = _nearest_route_position(points, waypoint)
        located.append((position, waypoint))
    located.sort(key=lambda item: item[0])

    route_end = float(len(points) - 1)
    boundaries: list[tuple[float, str]] = [(0.0, "START")]
    for position, waypoint in located:
        if position <= 1e-7 or position >= route_end - 1e-7:
            continue
        if boundaries and abs(position - boundaries[-1][0]) <= 1e-7:
            continue
        label = waypoint.name or f"チェックポイント{len(boundaries)}"
        boundaries.append((position, label))
    boundaries.append((route_end, "GOAL"))

    parts: list[RoutePart] = []
    for number, (left, right) in enumerate(zip(boundaries, boundaries[1:]), start=1):
        start_position, start_label = left
        end_position, end_label = right
        segment = _points_between_route_positions(points, start_position, end_position)
        if len(segment) < 2:
            continue
        parts.append(
            RoutePart(
                number=len(parts) + 1,
                points=segment,
                distance_m=track_distance(segment),
                gain_m=elevation_gain(segment),
                label=f"{start_label} → {end_label}",
            )
        )
    if not parts:
        raise ValueError("有効なチェックポイント区間を作成できませんでした。")
    return parts


def point_distance_fractions(points: list[TrackPoint]) -> list[float]:
    if not points:
        return []
    distances = [0.0]
    for a, b in zip(points, points[1:]):
        distances.append(distances[-1] + haversine_m(a, b))
    total = distances[-1]
    if total <= 0:
        return [0.0] * len(points)
    return [value / total for value in distances]


def sample_by_fraction(points: list[TrackPoint], fractions: list[float]) -> list[TrackPoint]:
    if not points:
        return []
    if len(points) == 1:
        return [points[0] for _ in fractions]
    cumulative = point_distance_fractions(points)
    result: list[TrackPoint] = []
    cursor = 0
    for fraction in fractions:
        fraction = max(0.0, min(1.0, fraction))
        while cursor < len(cumulative) - 2 and cumulative[cursor + 1] < fraction:
            cursor += 1
        left = cumulative[cursor]
        right = cumulative[cursor + 1]
        ratio = 0.0 if right <= left else (fraction - left) / (right - left)
        result.append(interpolate(points[cursor], points[cursor + 1], ratio))
    return result


def sample_uniform(points: list[TrackPoint], count: int) -> list[TrackPoint]:
    if count <= 0:
        return []
    if len(points) == count:
        return list(points)
    if count == 1:
        return [points[0]]
    return sample_by_fraction(points, [i / (count - 1) for i in range(count)])


def sample_preserving_points(points: list[TrackPoint], count: int) -> list[TrackPoint]:
    """Resample a route while retaining every source point when there is room."""
    if len(points) >= count:
        return sample_uniform(points, count)
    source_fractions = point_distance_fractions(points)
    intervals = len(source_fractions) - 1
    allocation = [
        max(1, round((source_fractions[i + 1] - source_fractions[i]) * (count - 1)))
        for i in range(intervals)
    ]
    while sum(allocation) < count - 1:
        index = max(range(intervals), key=lambda i: source_fractions[i + 1] - source_fractions[i])
        allocation[index] += 1
    while sum(allocation) > count - 1:
        candidates = [i for i, value in enumerate(allocation) if value > 1]
        if not candidates:
            break
        index = max(candidates, key=lambda i: allocation[i])
        allocation[index] -= 1
    fractions = [source_fractions[0]]
    for interval, steps in enumerate(allocation):
        left = source_fractions[interval]
        right = source_fractions[interval + 1]
        for step in range(1, steps + 1):
            fractions.append(left + (right - left) * step / steps)
    return sample_by_fraction(points, fractions[:count])


def limit_route_points(points: list[TrackPoint], max_points: int) -> list[TrackPoint]:
    """Reduce a route to the requested maximum point count when necessary."""
    if len(points) < 2:
        raise ValueError("ルートには2点以上必要です。")
    if not MIN_OUTPUT_POINTS <= max_points <= MAX_OUTPUT_POINTS:
        raise ValueError(f"間引き後の点数は{MIN_OUTPUT_POINTS:,}～{MAX_OUTPUT_POINTS:,}の範囲で指定してください。")
    if max_points >= len(points):
        return list(points)
    return sample_preserving_points(points, max_points)


def partition_track_points(points: list[TrackPoint], segment_count: int) -> list[list[TrackPoint]]:
    """Partition a route into contiguous segments, sharing boundary points."""
    if len(points) < 2:
        raise ValueError("ルートには2点以上必要です。")
    segment_count = max(1, min(segment_count, len(points) - 1))
    if segment_count == 1:
        return [list(points)]

    fractions = point_distance_fractions(points)
    boundaries = [0]
    for segment_index in range(1, segment_count):
        target = segment_index / segment_count
        minimum = boundaries[-1] + 1
        maximum = len(points) - 1 - (segment_count - segment_index)
        candidate = min(maximum, bisect_left(fractions, target, minimum, maximum + 1))
        candidates = [candidate]
        if candidate > minimum:
            candidates.append(candidate - 1)
        candidate = min(candidates, key=lambda index: abs(fractions[index] - target))
        boundaries.append(candidate)
    boundaries.append(len(points) - 1)
    return [points[start : end + 1] for start, end in zip(boundaries, boundaries[1:])]


def web_mercator_pixel(point: TrackPoint, zoom: int) -> tuple[float, float]:
    """Convert a GPS point to world-pixel coordinates for a slippy map."""
    scale = MAP_TILE_SIZE * (2**zoom)
    latitude = max(-85.05112878, min(85.05112878, point.lat))
    x = (point.lon + 180.0) / 360.0 * scale
    latitude_radians = math.radians(latitude)
    y = (1.0 - math.asinh(math.tan(latitude_radians)) / math.pi) / 2.0 * scale
    return x, y


def map_fit_zoom(points: list[TrackPoint], width: int, height: int) -> int:
    """Choose the closest zoom that keeps the complete route in the viewport."""
    if not points or width <= 1 or height <= 1:
        return MAP_MIN_ZOOM
    padding_x = max(80, width * 0.12)
    padding_y = max(100, height * 0.16)
    for zoom in range(MAP_MAX_ZOOM, MAP_MIN_ZOOM - 1, -1):
        projected = [web_mercator_pixel(point, zoom) for point in points]
        route_width = max(x for x, _ in projected) - min(x for x, _ in projected)
        route_height = max(y for _, y in projected) - min(y for _, y in projected)
        if route_width <= width - padding_x and route_height <= height - padding_y:
            return zoom
    return MAP_MIN_ZOOM


def route_part_color(index: int, part_count: int) -> str:
    """Return a stable, distinct color for a route part."""
    if part_count <= 1:
        return MAP_ROUTE_COLORS[0]
    if part_count <= len(MAP_ROUTE_COLORS):
        return MAP_ROUTE_COLORS[index % len(MAP_ROUTE_COLORS)]
    hue = (index / part_count) % 1.0
    red, green, blue = colorsys.hsv_to_rgb(hue, 0.78, 0.86)
    return "#{:02x}{:02x}{:02x}".format(round(red * 255), round(green * 255), round(blue * 255))


def zoomed_center(
    center_world: tuple[float, float],
    old_zoom: int,
    new_zoom: int,
    width: float,
    height: float,
    focus_x: float | None = None,
    focus_y: float | None = None,
) -> tuple[float, float]:
    """Return the new world center while keeping a viewport point fixed."""
    if focus_x is None:
        focus_x = width / 2.0
    if focus_y is None:
        focus_y = height / 2.0
    offset_x = focus_x - width / 2.0
    offset_y = focus_y - height / 2.0
    ratio = 2 ** (new_zoom - old_zoom)
    return (
        (center_world[0] + offset_x) * ratio - offset_x,
        (center_world[1] + offset_y) * ratio - offset_y,
    )


def crc16_modbus(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc & 0xFFFF


class XossRouteWriter:
    """Creates variable-length XZRoutes v2 files from the supplied schema template."""

    def __init__(self, template_path: Path = TEMPLATE_PATH):
        if not template_path.is_file():
            raise FileNotFoundError(
                f"XOSSの参照テンプレートが見つかりません: {template_path}\n"
                "ReferenceFiles を残したまま実行してください。"
            )
        self.template = template_path.read_bytes()
        if not self.template.startswith(b"XZRoutes"):
            raise ValueError("参照テンプレートがXZRoutes形式ではありません。")
        self.header_size = 0x60
        self.record_size = 44
        self.template_record_count = struct.unpack_from("<H", self.template, 0x16)[0]
        if self.template_record_count <= 0:
            raise ValueError("参照テンプレートの区間レコード数が不正です。")
        template_point_start = self.header_size + self.record_size * self.template_record_count
        template_point_end = struct.unpack_from("<I", self.template, 0x30)[0]
        self.elev_marker_offset = self.template.index(b"ELEV")
        if not template_point_start < template_point_end < self.elev_marker_offset:
            raise ValueError("参照テンプレートの座標列位置を解析できません。")
        if (template_point_end - template_point_start) % 8:
            raise ValueError("参照テンプレートの座標列サイズが不正です。")
        self.elev_gap = self.elev_marker_offset - template_point_end
        self.template_elev_count = struct.unpack_from("<I", self.template, self.elev_marker_offset + 4)[0]
        if self.template_elev_count <= 0:
            raise ValueError("参照テンプレートのELEV件数が不正です。")
        self.template_records = [
            self.template[self.header_size + index * self.record_size : self.header_size + (index + 1) * self.record_size]
            for index in range(self.template_record_count)
        ]

    def _record_count_for_points(self, point_count: int) -> int:
        # Each record count is a uint16. Boundary points are shared by adjacent
        # records, so a segment can contribute at most 0xffff - 1 new points.
        minimum = max(1, math.ceil((point_count - 1) / 0xFFFE))
        return max(minimum, min(self.template_record_count, point_count - 1))

    def create(
        self,
        points: list[TrackPoint],
        rid: int,
        name: str,
        max_points: int = DEFAULT_OUTPUT_POINTS,
    ) -> bytes:
        if len(points) < 2:
            raise ValueError("ルートには2点以上必要です。")
        route_points = limit_route_points(points, max_points)
        point_count = len(route_points)
        record_count = self._record_count_for_points(point_count)
        segments = partition_track_points(route_points, record_count)
        if record_count > 0xFFFF:
            raise ValueError("ROの区間レコード数が上限を超えています。")

        record_table = bytearray()
        coordinate_data = bytearray()
        point_offset = self.header_size + self.record_size * record_count
        for index, segment in enumerate(segments):
            template_record = self.template_records[index % self.template_record_count]
            record = bytearray(template_record)
            start = segment[0]
            end = segment[-1]
            self._write_point(record, 0, start)
            self._write_point(record, 8, end)
            struct.pack_into("<I", record, 16, round(track_distance(segment)))
            struct.pack_into("<H", record, 22, len(segment))
            struct.pack_into("<I", record, 24, point_offset)
            self._write_point(record, 28, TrackPoint(min(start.lat, end.lat), min(start.lon, end.lon)))
            self._write_point(record, 36, TrackPoint(max(start.lat, end.lat), max(start.lon, end.lon)))
            record_table.extend(record)
            for point in segment:
                coordinate_data.extend(
                    struct.pack("<ii", round(point.lat * 1_000_000), round(point.lon * 1_000_000))
                )
            point_offset += len(segment) * 8

        point_end = point_offset
        elevation_points = route_points
        elevation_count = len(elevation_points)
        elev_marker_offset = point_end + self.elev_gap
        elev_data_offset = elev_marker_offset + 12
        elev_data = bytearray(b"ELEV" + struct.pack("<II", elevation_count, 0))
        previous = elevation_points[0]
        for index, point in enumerate(elevation_points):
            altitude = int(point.ele) if point.ele is not None else 0
            delta = round(haversine_m(previous, point)) if index else 0
            elev_data.extend(struct.pack("<II", max(0, altitude), max(0, delta)))
            previous = point

        data = bytearray(self.template[: self.header_size])
        total_m = track_distance(points)
        gain_m = elevation_gain(points)

        struct.pack_into("<I", data, 0x08, rid)
        struct.pack_into("<I", data, 0x14, 2 | (record_count << 16))
        struct.pack_into("<I", data, 0x18, elev_data_offset)
        struct.pack_into("<I", data, 0x1C, elevation_count)
        struct.pack_into("<I", data, 0x30, point_end)
        lats = [p.lat for p in points]
        lons = [p.lon for p in points]
        struct.pack_into(
            "<iiii",
            data,
            0x20,
            round(min(lats) * 1_000_000),
            round(min(lons) * 1_000_000),
            round(max(lats) * 1_000_000),
            round(max(lons) * 1_000_000),
        )
        struct.pack_into("<I", data, 0x34, gain_m)
        struct.pack_into("<I", data, 0x38, round(total_m))
        data[0x40:0x60] = b"\x00" * 32
        name_bytes = truncate_utf8(name, 31).encode("utf-8")
        data[0x40 : 0x40 + len(name_bytes)] = name_bytes

        data.extend(record_table)
        data.extend(coordinate_data)
        data.extend(b"\x00" * self.elev_gap)
        data.extend(elev_data)
        data.extend(b"\x00\x00")
        struct.pack_into("<I", data, 0x10, len(data))

        # The final two bytes are the little-endian CRC-16/Modbus of the file.
        struct.pack_into("<H", data, len(data) - 2, crc16_modbus(bytes(data[:-2])))
        return bytes(data)

    @staticmethod
    def _read_point(data: bytes | bytearray, offset: int) -> TrackPoint:
        lat, lon = struct.unpack_from("<ii", data, offset)
        return TrackPoint(lat / 1_000_000.0, lon / 1_000_000.0)

    @staticmethod
    def _write_point(data: bytearray, offset: int, point: TrackPoint) -> None:
        struct.pack_into("<ii", data, offset, round(point.lat * 1_000_000), round(point.lon * 1_000_000))

class MapPreview(ttk.Frame):
    """A lightweight OSM tile view with a GPX route drawn above it."""

    def __init__(self, parent: tk.Misc) -> None:
        super().__init__(parent)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        toolbar = ttk.Frame(self)
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        toolbar.columnconfigure(0, weight=1)
        self.status_label = ttk.Label(toolbar, text="GPXを選択すると地図を表示します。", foreground="#555")
        self.status_label.grid(row=0, column=0, sticky="w")
        ttk.Button(toolbar, text="ルートに合わせる", command=self.fit_route).grid(row=0, column=1, padx=(8, 0))
        ttk.Button(toolbar, text="−", width=3, command=lambda: self.change_zoom(-1)).grid(row=0, column=2, padx=(8, 0))
        ttk.Button(toolbar, text="＋", width=3, command=lambda: self.change_zoom(1)).grid(row=0, column=3, padx=(3, 0))
        ttk.Button(toolbar, text="地図を再取得", command=self.reload_tiles).grid(row=0, column=4, padx=(8, 0))
        ttk.Label(toolbar, text="© OpenStreetMap contributors", foreground="#777").grid(row=0, column=5, padx=(12, 0))

        self.canvas = tk.Canvas(self, background="#e8eef2", highlightthickness=0)
        self.canvas.grid(row=1, column=0, sticky="nsew")
        self.canvas.bind("<Configure>", self._on_resize)
        self.canvas.bind("<MouseWheel>", self._on_mousewheel)
        self.canvas.bind("<Button-4>", lambda event: self._on_mousewheel(event, 1))
        self.canvas.bind("<Button-5>", lambda event: self._on_mousewheel(event, -1))
        self.canvas.bind("<ButtonPress-1>", self._on_pan_start)
        self.canvas.bind("<B1-Motion>", self._on_pan_motion)
        self.canvas.bind("<ButtonRelease-1>", self._on_pan_end)

        self.points: list[TrackPoint] = []
        self.route_parts: list[RoutePart] = []
        self.checkpoints: list[GpxWaypoint] = []
        self.zoom = MAP_MIN_ZOOM
        self.center_world = (0.0, 0.0)
        self._pan_last: tuple[int, int] | None = None
        self._fit_after_id: str | None = None
        self._tile_requests: queue.Queue[tuple[int, int, int]] = queue.Queue()
        self._tile_results: queue.Queue[tuple[tuple[int, int, int], bool]] = queue.Queue()
        self._pending_tiles: set[tuple[int, int, int]] = set()
        self._failed_tiles: set[tuple[int, int, int]] = set()
        self._tile_images: dict[tuple[int, int, int], tk.PhotoImage] = {}
        self._worker = threading.Thread(target=self._tile_worker, daemon=True)
        self._worker.start()
        self.after(100, self._poll_tile_results)

    def set_route(self, points: list[TrackPoint]) -> None:
        if points:
            self.set_route_parts([RoutePart(1, list(points), track_distance(points), elevation_gain(points))])
        else:
            self.set_route_parts([])

    def set_checkpoint_waypoints(self, checkpoints: list[GpxWaypoint]) -> None:
        self.checkpoints = list(checkpoints)
        if self.points:
            self._render()

    def set_route_parts(self, parts: list[RoutePart]) -> None:
        """Set the route geometry and preserve each split part for colored drawing."""
        previous_points = self.points
        self.route_parts = [part for part in parts if len(part.points) >= 2]
        self.points = []
        for part in self.route_parts:
            if self.points and self.points[-1] == part.points[0]:
                self.points.extend(part.points[1:])
            else:
                self.points.extend(part.points)
        self._failed_tiles.clear()
        if not self.points:
            self.center_world = (0.0, 0.0)
            self.canvas.delete("all")
            self.status_label.configure(text="GPXを選択すると地図を表示します。")
            return
        if previous_points:
            self._render()
        else:
            self.fit_route()

    def fit_route(self) -> None:
        if not self.points:
            return
        width = self.canvas.winfo_width()
        height = self.canvas.winfo_height()
        if width <= 2 or height <= 2:
            if self._fit_after_id is None:
                self._fit_after_id = self.after(100, self._fit_when_ready)
            return
        self._fit_after_id = None
        self.zoom = map_fit_zoom(self.points, width, height)
        projected = [web_mercator_pixel(point, self.zoom) for point in self.points]
        self.center_world = (
            (min(x for x, _ in projected) + max(x for x, _ in projected)) / 2.0,
            (min(y for _, y in projected) + max(y for _, y in projected)) / 2.0,
        )
        self._render()

    def _fit_when_ready(self) -> None:
        self._fit_after_id = None
        self.fit_route()

    def change_zoom(self, delta: int, focus_x: float | None = None, focus_y: float | None = None) -> None:
        if not self.points:
            return
        new_zoom = max(MAP_MIN_ZOOM, min(MAP_MAX_ZOOM, self.zoom + delta))
        if new_zoom == self.zoom:
            return
        self.center_world = zoomed_center(
            self.center_world,
            self.zoom,
            new_zoom,
            self.canvas.winfo_width(),
            self.canvas.winfo_height(),
            focus_x,
            focus_y,
        )
        self.zoom = new_zoom
        self._render()

    def _on_mousewheel(self, event: tk.Event, direction: int | None = None) -> str:
        if direction is None:
            direction = 1 if event.delta > 0 else -1 if event.delta < 0 else 0
        if direction:
            self.change_zoom(direction, event.x, event.y)
        return "break"

    def _on_pan_start(self, event: tk.Event) -> None:
        if not self.points:
            return
        self._pan_last = (event.x, event.y)
        self.canvas.configure(cursor="fleur")

    def _on_pan_motion(self, event: tk.Event) -> None:
        if self._pan_last is None:
            return
        last_x, last_y = self._pan_last
        self.center_world = (
            self.center_world[0] - (event.x - last_x),
            self.center_world[1] - (event.y - last_y),
        )
        self._pan_last = (event.x, event.y)
        self._render()

    def _on_pan_end(self, _event: tk.Event) -> None:
        self._pan_last = None
        self.canvas.configure(cursor="")

    def _on_resize(self, _event: tk.Event) -> None:
        if self.points:
            self.fit_route()

    def reload_tiles(self) -> None:
        self._failed_tiles.clear()
        self._render()

    def _tile_path(self, zoom: int, tile_x: int, tile_y: int) -> Path:
        return MAP_CACHE_DIR / str(zoom) / str(tile_x) / f"{tile_y}.png"

    def _request_tile(self, key: tuple[int, int, int]) -> None:
        if key in self._pending_tiles or key in self._failed_tiles:
            return
        self._pending_tiles.add(key)
        self._tile_requests.put(key)

    def _tile_worker(self) -> None:
        while True:
            zoom, tile_x, tile_y = self._tile_requests.get()
            path = self._tile_path(zoom, tile_x, tile_y)
            success = False
            try:
                MAP_CACHE_DIR.mkdir(parents=True, exist_ok=True)
                path.parent.mkdir(parents=True, exist_ok=True)
                request = urllib.request.Request(
                    MAP_TILE_URL.format(z=zoom, x=tile_x, y=tile_y),
                    headers={"User-Agent": "XOSS-Route-Converter/1.0"},
                )
                with urllib.request.urlopen(request, timeout=8) as response:
                    payload = response.read()
                if payload.startswith(b"\x89PNG\r\n\x1a\n"):
                    temporary = path.with_suffix(".tmp")
                    temporary.write_bytes(payload)
                    os.replace(temporary, path)
                    success = True
                else:
                    raise ValueError("地図タイルの形式が不正です")
            except (OSError, urllib.error.URLError, ValueError):
                success = False
            self._tile_results.put(((zoom, tile_x, tile_y), success))

    def _poll_tile_results(self) -> None:
        try:
            while True:
                key, success = self._tile_results.get_nowait()
                self._pending_tiles.discard(key)
                if success:
                    self._failed_tiles.discard(key)
                else:
                    self._failed_tiles.add(key)
                self._render()
        except queue.Empty:
            pass
        try:
            self.after(100, self._poll_tile_results)
        except tk.TclError:
            pass

    def _render(self) -> None:
        self.canvas.delete("all")
        if not self.points:
            self.status_label.configure(text="GPXを選択すると地図を表示します。")
            return
        width = self.canvas.winfo_width()
        height = self.canvas.winfo_height()
        if width <= 2 or height <= 2:
            return

        left = self.center_world[0] - width / 2.0
        top = self.center_world[1] - height / 2.0
        right = left + width
        bottom = top + height
        tile_count = 2**self.zoom
        first_tile_x = math.floor(left / MAP_TILE_SIZE)
        last_tile_x = math.floor((right - 1) / MAP_TILE_SIZE)
        first_tile_y = max(0, math.floor(top / MAP_TILE_SIZE))
        last_tile_y = min(tile_count - 1, math.floor((bottom - 1) / MAP_TILE_SIZE))
        loading = 0

        for tile_x in range(first_tile_x, last_tile_x + 1):
            wrapped_x = tile_x % tile_count
            for tile_y in range(first_tile_y, last_tile_y + 1):
                key = (self.zoom, wrapped_x, tile_y)
                image = self._tile_images.get(key)
                path = self._tile_path(*key)
                if image is None and path.is_file():
                    try:
                        image = tk.PhotoImage(file=str(path))
                        self._tile_images[key] = image
                    except tk.TclError:
                        image = None
                if image is not None:
                    self.canvas.create_image(
                        tile_x * MAP_TILE_SIZE - left,
                        tile_y * MAP_TILE_SIZE - top,
                        image=image,
                        anchor="nw",
                        tags="map_tile",
                    )
                else:
                    self.canvas.create_rectangle(
                        tile_x * MAP_TILE_SIZE - left,
                        tile_y * MAP_TILE_SIZE - top,
                        (tile_x + 1) * MAP_TILE_SIZE - left,
                        (tile_y + 1) * MAP_TILE_SIZE - top,
                        fill="#e8eef2",
                        outline="#d4e0e6",
                        tags="map_tile",
                    )
                    if key not in self._failed_tiles:
                        self._request_tile(key)
                    loading += 1

        # route_parts is the active split result: distance mode supplies 300 km
        # parts, checkpoint mode supplies checkpoint parts, and no-split mode
        # supplies one part, so one color is used in that last case.
        route_parts = self.route_parts or [RoutePart(1, self.points, track_distance(self.points), elevation_gain(self.points))]
        total_route_points = sum(len(part.points) for part in route_parts)
        for part_index, part in enumerate(route_parts):
            sample_count = min(
                len(part.points),
                max(2, round(4000 * len(part.points) / total_route_points)),
            )
            route = part.points if len(part.points) <= sample_count else sample_uniform(part.points, sample_count)
            projected = [
                (web_mercator_pixel(point, self.zoom)[0] - left, web_mercator_pixel(point, self.zoom)[1] - top)
                for point in route
            ]
            if len(projected) >= 2:
                self.canvas.create_line(projected, fill="#ffffff", width=7, joinstyle="round", capstyle="round")
                self.canvas.create_line(
                    projected,
                    fill=route_part_color(part_index, len(route_parts)),
                    width=4,
                    joinstyle="round",
                    capstyle="round",
                )
        start_x, start_y = web_mercator_pixel(self.points[0], self.zoom)
        end_x, end_y = web_mercator_pixel(self.points[-1], self.zoom)
        self._draw_marker(start_x - left, start_y - top, "#2f9e62", "START")
        self._draw_marker(end_x - left, end_y - top, "#c43d4b", "GOAL")
        for checkpoint in self.checkpoints:
            checkpoint_x, checkpoint_y = web_mercator_pixel(
                TrackPoint(checkpoint.lat, checkpoint.lon),
                self.zoom,
            )
            self._draw_marker(
                checkpoint_x - left,
                checkpoint_y - top,
                "#8e5bd9",
                checkpoint.name or "CHECKPOINT",
            )

        if self._failed_tiles:
            self.status_label.configure(text="背景地図を取得できないため、ルート線のみ表示中")
        elif loading:
            self.status_label.configure(text=f"背景地図を読み込み中…（残り {loading} 枚）")
        else:
            self.status_label.configure(text=f"背景地図表示中 ／ ズーム {self.zoom}")

    def _draw_marker(self, x: float, y: float, color: str, label: str) -> None:
        radius = 7
        self.canvas.create_oval(x - radius, y - radius, x + radius, y + radius, fill=color, outline="white", width=2)
        self.canvas.create_text(x + 12, y - 12, text=label, anchor="sw", fill=color, font=("Segoe UI", 9, "bold"))


def make_rid(existing: set[int]) -> int:
    for _ in range(1000):
        rid = random.randint(RID_MIN, RID_MAX)
        if rid not in existing:
            return rid
    rid = int(time.time()) % (RID_MAX - RID_MIN + 1) + RID_MIN
    while rid in existing:
        rid = rid + 1 if rid < RID_MAX else RID_MIN
    return rid


def make_rids(existing: set[int], count: int) -> list[int]:
    """Return a collision-free consecutive RID block for split routes."""
    if count < 1:
        return []
    if count > RID_MAX - RID_MIN + 1:
        raise ValueError("分割ルート数がRIDの上限を超えています。")

    latest_start = RID_MAX - count + 1
    for _ in range(1000):
        start = random.randint(RID_MIN, latest_start)
        candidate = range(start, start + count)
        if all(rid not in existing for rid in candidate):
            return list(candidate)

    # Fall back to a deterministic scan if the RID space becomes crowded.
    start = RID_MIN
    while start <= latest_start:
        candidate = range(start, start + count)
        if all(rid not in existing for rid in candidate):
            return list(candidate)
        start += 1
    raise ValueError("使用可能な連番RIDを確保できませんでした。")


def truncate_utf8(text: str, max_bytes: int) -> str:
    encoded = text.encode("utf-8")[:max_bytes]
    while encoded:
        try:
            return encoded.decode("utf-8")
        except UnicodeDecodeError:
            encoded = encoded[:-1]
    return ""


def route_part_name(title: str, part_number: int, part_total: int) -> str:
    if part_total <= 1:
        return title
    width = max(2, len(str(part_total)))
    suffix = f" {part_number:0{width}d}"
    title_part = truncate_utf8(title, 31 - len(suffix.encode("utf-8")))
    return title_part + suffix


def read_routebooks(path: Path) -> dict:
    if not path.is_file():
        return {
            "version": "2.0.0",
            "device_model": "A2",
            "sn": "",
            "update_at": int(time.time()),
            "routes": [],
        }
    try:
        document = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"routebooks.jsonを読み込めません: {exc}") from exc
    if not isinstance(document, dict) or not isinstance(document.get("routes", []), list):
        raise ValueError("routebooks.jsonの形式が想定と異なります。")
    document.setdefault("version", "2.0.0")
    document.setdefault("device_model", "A2")
    document.setdefault("sn", "")
    return document


def write_routebooks(path: Path, document: dict) -> None:
    path.write_text(json.dumps(document, ensure_ascii=False, indent="\t") + "\n", encoding="utf-8")


def write_routebooks_atomic(path: Path, document: dict) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    write_routebooks(temporary, document)
    os.replace(temporary, path)


def delete_routes_from_device(device_root: Path, route_ids: list[str]) -> None:
    """Remove selected route files and their routebook entries without creating a backup."""
    routebooks_path = device_root / "routebooks.json"
    document = read_routebooks(routebooks_path)
    requested_ids = list(dict.fromkeys(str(rid) for rid in route_ids))
    existing_ids = {str(route.get("rid", "")) for route in document.get("routes", [])}
    target_ids = [rid for rid in requested_ids if rid in existing_ids]
    if not target_ids:
        raise ValueError("選択したルートはすでに端末から削除されています。")
    route_files = [device_root / "Routes" / f"{rid}.ro" for rid in target_ids]
    route_contents: dict[Path, bytes] = {}
    for route_file in route_files:
        if route_file.is_file():
            route_contents[route_file] = route_file.read_bytes()
    try:
        for route_file in route_files:
            if route_file.is_file():
                route_file.unlink()
        target_id_set = set(target_ids)
        document["routes"] = [route for route in document.get("routes", []) if str(route.get("rid", "")) not in target_id_set]
        document["update_at"] = int(time.time())
        write_routebooks_atomic(routebooks_path, document)
    except Exception:
        for route_file, contents in route_contents.items():
            route_file.write_bytes(contents)
        raise


def find_xoss_drives() -> list[Path]:
    drives: list[Path] = []
    if os.name == "nt":
        for letter in string.ascii_uppercase:
            root = Path(f"{letter}:\\")
            try:
                if root.exists() and (root / "routebooks.json").is_file():
                    drives.append(root)
            except OSError:
                continue
    else:
        for candidate in (Path("/media"), Path("/run/media"), Path("/Volumes")):
            if candidate.is_dir():
                for root in candidate.iterdir():
                    if (root / "routebooks.json").is_file():
                        drives.append(root)
    return drives


def stage_routes(
    parts: list[RoutePart],
    title: str,
    template_path: Path = TEMPLATE_PATH,
    max_points: int = DEFAULT_OUTPUT_POINTS,
) -> tuple[Path, dict, list[Path]]:
    writer = XossRouteWriter(template_path)
    staging = Path(tempfile.mkdtemp(prefix="xoss_route_converter_"))
    routes_dir = staging / "Routes"
    routes_dir.mkdir()
    routebooks = read_routebooks(staging / "routebooks.json")
    existing = {int(item.get("rid")) for item in routebooks.get("routes", []) if str(item.get("rid", "")).isdigit()}
    generated: list[Path] = []
    new_entries: list[dict] = []
    rids = make_rids(existing, len(parts))
    for part, rid in zip(parts, rids):
        existing.add(rid)
        part_name = route_part_name(title, part.number, len(parts))
        payload = writer.create(part.points, rid, part_name, max_points=max_points)
        output = routes_dir / f"{rid}.ro"
        output.write_bytes(payload)
        generated.append(output)
        new_entries.append(
            {
                "rid": rid,
                "size": len(payload),
                "source": 1,
                "name": part_name,
                "type": "Cycling",
                "verison": "2",
                "length": round(part.distance_m),
                "gain": part.gain_m,
            }
        )
    routebooks["update_at"] = int(time.time())
    routebooks["routes"] = list(routebooks.get("routes", [])) + new_entries
    write_routebooks(staging / "routebooks.json", routebooks)
    return staging, routebooks, generated


def transfer_staging(staging: Path, device_root: Path, keep_existing: bool = True) -> None:
    if not (device_root / "routebooks.json").is_file():
        raise ValueError("選択したドライブのルートにroutebooks.jsonがありません。")
    device_routes = device_root / "Routes"
    device_routes.mkdir(exist_ok=True)
    staged_routes = staging / "Routes"
    for source in staged_routes.glob("*.ro"):
        shutil.copy2(source, device_routes / source.name)

    destination_json = device_root / "routebooks.json"
    if keep_existing:
        current = read_routebooks(destination_json)
        incoming = read_routebooks(staging / "routebooks.json")
        current["update_at"] = incoming.get("update_at", int(time.time()))
        current["routes"] = list(current.get("routes", [])) + [
            item for item in incoming.get("routes", []) if item.get("rid") not in {x.get("rid") for x in current.get("routes", [])}
        ]
        write_routebooks(destination_json, current)
    else:
        shutil.copy2(staging / "routebooks.json", destination_json)


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"XOSS NAV+ ルート転送ツール ({APP_VERSION})")
        self.settings_path = runtime_directory() / SETTINGS_FILENAME
        self.window_settings = read_window_settings(self.settings_path)
        saved_geometry = self.window_settings.get("geometry")
        try:
            self.geometry(saved_geometry if isinstance(saved_geometry, str) and saved_geometry else "1400x820")
        except tk.TclError:
            self.geometry("1400x820")
        self.minsize(1100, 650)
        self.gpx_path: Path | None = None
        self.gpx_title = ""
        self.points: list[TrackPoint] = []
        self.checkpoints: list[GpxWaypoint] = []
        self.parts: list[RoutePart] = []
        self.drives: list[Path] = []
        self.staging: Path | None = None
        self._settings_save_after_id: str | None = None
        self._build_ui()
        self.bind("<Configure>", self._on_window_configure, add="+")
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.refresh_drives()

    def _build_ui(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        root = ttk.Frame(self, padding=16)
        root.pack(fill="both", expand=True)
        root.columnconfigure(0, weight=0, minsize=460)
        root.columnconfigure(1, weight=1, minsize=560)
        root.rowconfigure(0, weight=1)

        left_panel = ttk.Frame(root)
        left_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 14))
        left_panel.columnconfigure(1, weight=1)
        left_panel.rowconfigure(5, weight=1)

        map_box = ttk.LabelFrame(root, text="ルート地図プレビュー", padding=10)
        map_box.grid(row=0, column=1, sticky="nsew")
        map_box.columnconfigure(0, weight=1)
        map_box.rowconfigure(0, weight=1)
        self.map_preview = MapPreview(map_box)
        self.map_preview.grid(row=0, column=0, sticky="nsew")

        root = left_panel

        ttk.Label(root, text="GPXファイル").grid(row=0, column=0, sticky="w", padx=(0, 10), pady=5)
        self.gpx_label = ttk.Label(root, text="未選択", foreground="#555")
        self.gpx_label.grid(row=0, column=1, sticky="ew", pady=5)
        ttk.Button(root, text="GPXを選択…", command=self.select_gpx).grid(row=0, column=2, padx=(10, 0), pady=5)

        ttk.Label(root, text="ルート名").grid(row=1, column=0, sticky="w", padx=(0, 10), pady=5)
        self.title_var = tk.StringVar()
        ttk.Entry(root, textvariable=self.title_var).grid(row=1, column=1, columnspan=2, sticky="ew", pady=5)

        ttk.Label(root, text="分割方式").grid(row=2, column=0, sticky="w", padx=(0, 10), pady=5)
        saved_split_mode = self.window_settings.get("split_mode")
        if saved_split_mode not in {"distance", "checkpoint", "none"}:
            saved_split_mode = "distance"
        self.split_mode_var = tk.StringVar(value=saved_split_mode)
        self.split_var = tk.StringVar(value=str(DEFAULT_SPLIT_KM))
        split_options = ttk.Frame(root)
        split_options.grid(row=2, column=1, columnspan=2, sticky="w", pady=5)
        distance_options = ttk.Frame(split_options)
        distance_options.pack(side="left")
        ttk.Radiobutton(
            distance_options,
            text="距離で分割",
            variable=self.split_mode_var,
            value="distance",
            command=self._on_split_mode_change,
        ).pack(side="left")
        self.split_entry = ttk.Entry(distance_options, textvariable=self.split_var, width=12)
        self.split_entry.pack(side="left", padx=(8, 4))
        self.split_distance_label = ttk.Label(distance_options, text="km")
        self.split_distance_label.pack(side="left")
        ttk.Radiobutton(
            split_options,
            text="チェックポイントで分割",
            variable=self.split_mode_var,
            value="checkpoint",
            command=self._on_split_mode_change,
        ).pack(side="left", padx=(12, 0))
        ttk.Radiobutton(
            split_options,
            text="分割しない",
            variable=self.split_mode_var,
            value="none",
            command=self._on_split_mode_change,
        ).pack(side="left", padx=(12, 0))
        self.split_var.trace_add("write", lambda *_: self.update_preview())

        ttk.Label(root, text="点数による間引き").grid(row=3, column=0, sticky="w", padx=(0, 10), pady=5)
        self.point_limit_var = tk.StringVar(value=str(DEFAULT_OUTPUT_POINTS))
        point_options = ttk.Frame(root)
        point_options.grid(row=3, column=1, columnspan=2, sticky="w", pady=5)
        ttk.Entry(point_options, textvariable=self.point_limit_var, width=12).pack(side="left")
        ttk.Label(
            point_options,
            text=f"点（{MIN_OUTPUT_POINTS:,}～{MAX_OUTPUT_POINTS:,}、元の点数が少なければそのまま）",
        ).pack(side="left", padx=(6, 0))
        self.point_limit_var.trace_add("write", lambda *_: self.update_preview())

        ttk.Label(root, text="転送先").grid(row=4, column=0, sticky="w", padx=(0, 10), pady=5)
        self.drive_var = tk.StringVar()
        self.drive_combo = ttk.Combobox(root, textvariable=self.drive_var, state="readonly")
        self.drive_combo.grid(row=4, column=1, sticky="ew", pady=5)
        self.drive_combo.bind("<<ComboboxSelected>>", lambda _event: self.refresh_device_routes())
        ttk.Button(root, text="再検索", command=self.refresh_drives).grid(row=4, column=2, padx=(10, 0), pady=5)

        preview_box = ttk.LabelFrame(root, text="分割プレビュー", padding=10)
        preview_box.grid(row=5, column=0, columnspan=3, sticky="nsew", pady=(14, 10))
        preview_box.columnconfigure(0, weight=1)
        preview_box.rowconfigure(1, weight=1)
        self.summary_label = ttk.Label(
            preview_box,
            text="GPXを選択すると距離と分割数を表示します。",
            wraplength=440,
            justify="left",
        )
        self.summary_label.grid(row=0, column=0, sticky="w", pady=(0, 8))
        columns = ("part", "distance", "points", "gain")
        self.tree = ttk.Treeview(preview_box, columns=columns, show="headings", height=10)
        headings = {"part": "区間 / チェックポイント", "distance": "距離", "points": "点数", "gain": "獲得標高"}
        widths = {"part": 170, "distance": 130, "points": 110, "gain": 130}
        for column in columns:
            self.tree.heading(column, text=headings[column])
            self.tree.column(
                column,
                width=widths[column],
                minwidth=widths[column],
                anchor="center",
                stretch=column == "part",
            )
        scroll = ttk.Scrollbar(preview_box, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.grid(row=1, column=0, sticky="nsew")
        scroll.grid(row=1, column=1, sticky="ns")

        options = ttk.Frame(root)
        options.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(0, 8))
        ttk.Button(options, text="変換してフォルダへ保存…", command=self.save_routes).pack(side="right", padx=(8, 0))
        self.transfer_button = ttk.Button(options, text="XOSS NAV+へ転送", command=self.transfer_routes)
        self.transfer_button.pack(side="right")

        device_box = ttk.LabelFrame(root, text="端末内ルート管理", padding=10)
        device_box.grid(row=7, column=0, columnspan=3, sticky="ew", pady=(0, 10))
        device_box.columnconfigure(0, weight=1)
        device_box.rowconfigure(0, weight=1)
        device_columns = ("rid", "name", "distance", "size")
        self.device_tree = ttk.Treeview(device_box, columns=device_columns, show="headings", selectmode="extended", height=6)
        device_headings = {"rid": "RID", "name": "ルート名", "distance": "距離", "size": "ファイルサイズ"}
        device_widths = {"rid": 90, "name": 360, "distance": 150, "size": 140}
        for column in device_columns:
            self.device_tree.heading(column, text=device_headings[column])
            self.device_tree.column(column, width=device_widths[column], anchor="center")
        device_scroll = ttk.Scrollbar(device_box, orient="vertical", command=self.device_tree.yview)
        self.device_tree.configure(yscrollcommand=device_scroll.set)
        self.device_tree.grid(row=0, column=0, sticky="ew")
        device_scroll.grid(row=0, column=1, sticky="ns")
        device_actions = ttk.Frame(device_box)
        device_actions.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        self.device_status = ttk.Label(device_actions, text="転送先を選択すると端末内ルートを表示します。", foreground="#555")
        self.device_status.pack(side="left")
        ttk.Button(device_actions, text="一覧を更新", command=self.refresh_device_routes).pack(side="right", padx=(8, 0))
        ttk.Button(device_actions, text="選択したルートを削除", command=self.delete_device_routes).pack(side="right")

        self.status = tk.Text(root, height=5, state="disabled", wrap="word", background="#f5f5f5")
        self.status.grid(row=8, column=0, columnspan=3, sticky="ew")
        self._on_split_mode_change()
        self.log("GPXファイルを選択してください。")

    def log(self, message: str) -> None:
        self.status.configure(state="normal")
        self.status.insert("end", message + "\n")
        self.status.see("end")
        self.status.configure(state="disabled")

    def select_gpx(self) -> None:
        selected = filedialog.askopenfilename(
            title="GPXファイルを選択",
            filetypes=[("GPX files", "*.gpx"), ("All files", "*.*")],
        )
        if not selected:
            return
        try:
            self.gpx_title, self.points, waypoints = parse_gpx_with_waypoints(Path(selected))
        except (OSError, ET.ParseError, ValueError) as exc:
            messagebox.showerror("GPX読込エラー", str(exc))
            return
        self.checkpoints = checkpoint_waypoints(waypoints)
        self.gpx_path = Path(selected)
        self.gpx_label.configure(text=str(self.gpx_path))
        self.title_var.set(self.gpx_title)
        self.map_preview.set_route(self.points)
        self.map_preview.set_checkpoint_waypoints(self.checkpoints)
        self.log(
            f"読込完了: {len(self.points):,}点、全長 {track_distance(self.points) / 1000:.1f}km、"
            f"チェックポイント候補 {len(self.checkpoints)}件"
        )
        self.update_preview()

    def _on_split_mode_change(self) -> None:
        enabled = self.split_mode_var.get() == "distance"
        self.split_entry.configure(state="normal" if enabled else "disabled")
        self.split_distance_label.configure(state="normal" if enabled else "disabled")
        self.update_preview()
        self._save_window_settings()

    def _save_window_settings(self) -> None:
        self._settings_save_after_id = None
        try:
            write_window_settings(self.settings_path, self.geometry(), self.split_mode_var.get())
        except OSError:
            pass

    def _on_window_configure(self, event: tk.Event) -> None:
        if event.widget is not self:
            return
        if self._settings_save_after_id is not None:
            try:
                self.after_cancel(self._settings_save_after_id)
            except tk.TclError:
                pass
        self._settings_save_after_id = self.after(400, self._save_window_settings)

    def _on_close(self) -> None:
        if self._settings_save_after_id is not None:
            try:
                self.after_cancel(self._settings_save_after_id)
            except tk.TclError:
                pass
        self._save_window_settings()
        self.destroy()

    def _get_point_limit(self) -> int:
        value = self.point_limit_var.get().strip().replace(",", "")
        if not value:
            raise ValueError(f"間引き後の点数を{MIN_OUTPUT_POINTS:,}～{MAX_OUTPUT_POINTS:,}で入力してください。")
        try:
            limit = int(value)
        except ValueError as exc:
            raise ValueError("間引き後の点数を整数で入力してください。") from exc
        if not MIN_OUTPUT_POINTS <= limit <= MAX_OUTPUT_POINTS:
            raise ValueError(f"間引き後の点数は{MIN_OUTPUT_POINTS:,}～{MAX_OUTPUT_POINTS:,}の範囲で指定してください。")
        return limit

    def _show_unsplit_route_on_map(self) -> None:
        """Keep the map in one neutral route color when no valid split is available."""
        self.parts = []
        if self.points:
            self.map_preview.set_route(self.points)

    def update_preview(self) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)
        if not self.points:
            return
        try:
            max_points = self._get_point_limit()
        except ValueError as exc:
            self.summary_label.configure(text=str(exc))
            self._show_unsplit_route_on_map()
            return

        def output_point_count(part: RoutePart) -> int:
            return min(len(part.points), max_points)

        mode = self.split_mode_var.get()
        if mode == "none":
            self.parts = split_track(self.points, None)
            self.map_preview.set_route_parts(self.parts)
            total = track_distance(self.points)
            point_text = f" ／ 出力点数 {output_point_count(self.parts[0]):,}点"
            self.summary_label.configure(text=f"全長 {total / 1000:.2f}km ／ 分割なし（1ルート）{point_text}")
            part = self.parts[0]
            self.tree.insert(
                "",
                "end",
                values=(part.number, f"{part.distance_m / 1000:.2f} km", f"{output_point_count(part):,}", f"{part.gain_m:,} m"),
            )
            return

        if mode == "checkpoint":
            try:
                self.parts = split_track_at_checkpoints(self.points, self.checkpoints)
            except ValueError as exc:
                self.summary_label.configure(text=str(exc))
                self._show_unsplit_route_on_map()
                return
            self.map_preview.set_route_parts(self.parts)
            total = track_distance(self.points)
            point_limit_text = f" ／ 出力点数上限 {max_points:,}点"
            self.summary_label.configure(
                text=(
                    f"全長 {total / 1000:.2f}km ／ {len(self.parts)}ルートに分割"
                    f"（チェックポイント {len(self.checkpoints)}件）{point_limit_text}"
                )
            )
            for part in self.parts:
                self.tree.insert(
                    "",
                    "end",
                    values=(
                        part.label or part.number,
                        f"{part.distance_m / 1000:.2f} km",
                        f"{output_point_count(part):,}",
                        f"{part.gain_m:,} m",
                    ),
                )
            return

        try:
            max_km = float(self.split_var.get().replace(",", "."))
            if max_km <= 0:
                raise ValueError
            self.parts = split_track(self.points, max_km * 1000.0)
        except ValueError:
            self.summary_label.configure(text="分割距離を正の数値で入力してください。")
            self._show_unsplit_route_on_map()
            return
        self.map_preview.set_route_parts(self.parts)
        total = track_distance(self.points)
        point_limit_text = f" ／ 出力点数上限 {max_points:,}点"
        self.summary_label.configure(
            text=f"全長 {total / 1000:.2f}km ／ {len(self.parts)}ルートに分割（上限 {max_km:g}km）{point_limit_text}"
        )
        for part in self.parts:
            self.tree.insert("", "end", values=(part.number, f"{part.distance_m / 1000:.2f} km", f"{output_point_count(part):,}", f"{part.gain_m:,} m"))

    def refresh_drives(self) -> None:
        self.drives = find_xoss_drives()
        labels = [f"{path}（routebooks.json検出）" for path in self.drives]
        self.drive_combo["values"] = labels
        if labels:
            self.drive_combo.current(0)
            self.log("XOSS NAV+候補のドライブを検出しました。")
            self.refresh_device_routes()
        else:
            self.drive_var.set("")
            self.refresh_device_routes()
            self.log("routebooks.jsonのあるドライブが見つかりません。NAV+接続後に再検索してください。")

    def selected_drive(self) -> Path | None:
        index = self.drive_combo.current()
        return self.drives[index] if 0 <= index < len(self.drives) else None

    def refresh_device_routes(self) -> None:
        for item in self.device_tree.get_children():
            self.device_tree.delete(item)
        drive = self.selected_drive()
        if drive is None:
            self.device_status.configure(text="転送先を選択すると端末内ルートを表示します。")
            return
        try:
            document = read_routebooks(drive / "routebooks.json")
        except (OSError, ValueError) as exc:
            self.device_status.configure(text="routebooks.jsonを読めません。")
            self.log(f"端末ルート一覧エラー: {exc}")
            return
        routes = document.get("routes", [])
        for route in routes:
            rid = str(route.get("rid", ""))
            try:
                length_km = f"{int(route.get('length', 0)) / 1000:.2f} km"
            except (TypeError, ValueError):
                length_km = "-"
            try:
                size = f"{int(route.get('size', 0)) / 1024:.1f} KB"
            except (TypeError, ValueError):
                size = "-"
            self.device_tree.insert("", "end", values=(rid, route.get("name", ""), length_km, size))
        self.device_status.configure(text=f"{len(routes)}件のルートを検出しました: {drive}")

    def delete_device_routes(self) -> None:
        selected = self.device_tree.selection()
        if not selected:
            messagebox.showwarning("ルート未選択", "削除するルートを選択してください。")
            return
        drive = self.selected_drive()
        if drive is None:
            messagebox.showwarning("転送先なし", "XOSS NAV+ドライブを選択してください。")
            return
        selected_rows = [self.device_tree.item(item, "values") for item in selected]
        route_ids = [str(row[0]) for row in selected_rows]
        route_names = [str(row[1]) for row in selected_rows]
        preview = "\n".join(f"・{name} (RID {rid})" for rid, name in zip(route_ids, route_names))
        if not messagebox.askyesno(
            "ルート削除の確認",
            f"次の{len(route_ids)}件を端末から削除します。\n\n{preview}\n\n削除後は元に戻せません。続行しますか？",
        ):
            return
        try:
            delete_routes_from_device(drive, route_ids)
        except (OSError, ValueError) as exc:
            messagebox.showerror("削除エラー", str(exc))
            self.refresh_device_routes()
            return
        self.refresh_device_routes()
        self.log(f"端末から{len(route_ids)}件を削除しました。")

    def make_staging(self) -> Path:
        if not self.parts:
            raise ValueError("GPX、分割方式、間引き設定を確認してください。")
        title = self.title_var.get().strip()
        if not title:
            raise ValueError("ルート名を入力してください。")
        if self.staging and self.staging.exists():
            shutil.rmtree(self.staging, ignore_errors=True)
        self.staging, _, generated = stage_routes(self.parts, title, max_points=self._get_point_limit())
        self.log(f"変換完了: {len(generated)}件のROファイルを作成しました。")
        return self.staging

    def save_routes(self) -> None:
        try:
            staging = self.make_staging()
        except (OSError, ValueError) as exc:
            messagebox.showerror("変換エラー", str(exc))
            return
        destination = filedialog.askdirectory(title="変換結果の保存先を選択")
        if not destination:
            return
        try:
            target = Path(destination)
            shutil.copy2(staging / "routebooks.json", target / "routebooks.json")
            target_routes = target / "Routes"
            target_routes.mkdir(exist_ok=True)
            for source in (staging / "Routes").glob("*.ro"):
                shutil.copy2(source, target_routes / source.name)
        except OSError as exc:
            messagebox.showerror("保存エラー", str(exc))
            return
        self.log(f"保存完了: {destination}")
        messagebox.showinfo("保存完了", "routebooks.json と Routes フォルダを保存しました。")

    def transfer_routes(self) -> None:
        drive = self.selected_drive()
        if drive is None:
            messagebox.showwarning("転送先なし", "routebooks.jsonのあるXOSS NAV+ドライブを接続して再検索してください。")
            return
        if not messagebox.askyesno("XOSS NAV+へ転送", f"転送先: {drive}\n新しいルートを追加します。続行しますか？"):
            return
        try:
            staging = self.make_staging()
            transfer_staging(staging, drive, True)
        except (OSError, ValueError) as exc:
            messagebox.showerror("転送エラー", str(exc))
            return
        self.refresh_device_routes()
        self.log(f"転送完了: {drive}")
        messagebox.showinfo("転送完了", "転送が完了しました。NAV+を安全に取り外して、Routebookから選択してください。")


def main() -> None:
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
