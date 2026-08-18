from __future__ import annotations

import json
import math
import os
import random
import shutil
import string
import struct
import tempfile
import time
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
import xml.etree.ElementTree as ET


APP_DIR = Path(__file__).resolve().parent
TEMPLATE_PATH = APP_DIR / "assets" / "xoss_nav_template.ro"
DEFAULT_SPLIT_KM = 300.0
EARTH_RADIUS_M = 6_378_137.0
GPX_NS = "http://www.topografix.com/GPX/1/1"


@dataclass
class TrackPoint:
    lat: float
    lon: float
    ele: float | None = None


@dataclass
class RoutePart:
    number: int
    points: list[TrackPoint]
    distance_m: float
    gain_m: int


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


def parse_gpx(path: Path) -> tuple[str, list[TrackPoint]]:
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
    return title, points


def split_track(points: list[TrackPoint], max_distance_m: float) -> list[RoutePart]:
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


def crc16_modbus(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc & 0xFFFF


class XossRouteWriter:
    """Creates XZRoutes v2 files using the supplied NAV+ reference file as a schema template."""

    def __init__(self, template_path: Path = TEMPLATE_PATH):
        if not template_path.is_file():
            raise FileNotFoundError(
                f"XOSSの参照テンプレートが見つかりません: {template_path}\n"
                "ReferenceFiles を残したまま実行してください。"
            )
        self.template = template_path.read_bytes()
        if not self.template.startswith(b"XZRoutes"):
            raise ValueError("参照テンプレートがXZRoutes形式ではありません。")
        self.elev_offset = self.template.index(b"ELEV")
        self.raw_start = 0x480
        self.raw_end = self._find_raw_end()
        if self.raw_start >= self.raw_end or (self.raw_end - self.raw_start) % 8:
            raise ValueError("参照テンプレートの経路点ブロックを解析できません。")
        self.raw_points = [
            TrackPoint(lat / 1_000_000.0, lon / 1_000_000.0)
            for lat, lon in struct.iter_unpack("<ii", self.template[self.raw_start : self.raw_end])
        ]
        self.raw_fractions = point_distance_fractions(self.raw_points)
        self.elev_count = struct.unpack_from("<I", self.template, self.elev_offset + 4)[0]
        if self.elev_count <= 0:
            raise ValueError("参照テンプレートのELEV件数が不正です。")
        self.record_count = (self.raw_start - 0x60) // 44

    def _find_raw_end(self) -> int:
        cursor = self.raw_start
        limit = self.elev_offset
        while cursor + 16 <= limit and self.template[cursor : cursor + 16] != b"\x00" * 16:
            cursor += 8
        return cursor

    def create(self, points: list[TrackPoint], rid: int, name: str) -> bytes:
        if len(points) < 2:
            raise ValueError("ルートには2点以上必要です。")
        data = bytearray(self.template)
        route_points = sample_uniform(points, self.elev_count)
        raw_points = sample_preserving_points(points, len(self.raw_points))
        total_m = track_distance(points)
        gain_m = elevation_gain(points)

        struct.pack_into("<I", data, 0x08, rid)
        struct.pack_into("<I", data, 0x10, len(data))
        struct.pack_into("<I", data, 0x1C, self.elev_count)
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
        name_bytes = name.encode("utf-8")
        while len(name_bytes) > 31:
            name_bytes = name_bytes[:-1]
            try:
                name_bytes.decode("utf-8")
                break
            except UnicodeDecodeError:
                continue
        data[0x40 : 0x40 + len(name_bytes)] = name_bytes

        # The first part is a fixed-size table of route segments. Keep its
        # instruction type but move each segment onto the new route.
        for index in range(self.record_count):
            offset = 0x60 + index * 44
            old_start = self._read_point(self.template, offset)
            old_end = self._read_point(self.template, offset + 8)
            start_fraction = self._nearest_fraction(old_start)
            end_fraction = self._nearest_fraction(old_end)
            new_start = sample_by_fraction(points, [start_fraction])[0]
            new_end = sample_by_fraction(points, [end_fraction])[0]
            self._write_point(data, offset, new_start)
            self._write_point(data, offset + 8, new_end)
            struct.pack_into("<I", data, offset + 16, round(haversine_m(new_start, new_end)))
            old_count = struct.unpack_from("<H", self.template, offset + 22)[0]
            if old_count > 0:
                new_count = max(2, round(old_count * max(0.01, end_fraction - start_fraction) * self.elev_count))
                struct.pack_into("<H", data, offset + 22, min(0xFFFF, new_count))
            self._write_point(data, offset + 28, TrackPoint(new_start.lat, new_end.lon))
            self._write_point(data, offset + 36, TrackPoint(new_end.lat, new_start.lon))

        for index, point in enumerate(raw_points):
            self._write_point(data, self.raw_start + index * 8, point)

        # ELEV stores integer elevation and distance since the previous point.
        elev_offset = self.elev_offset + 12
        previous = route_points[0]
        for point in route_points:
            altitude = int(point.ele) if point.ele is not None else 0
            delta = round(haversine_m(previous, point)) if point is not route_points[0] else 0
            struct.pack_into("<II", data, elev_offset, max(0, altitude), max(0, delta))
            elev_offset += 8
            previous = point

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

    def _nearest_fraction(self, point: TrackPoint) -> float:
        best_index = min(
            range(len(self.raw_points)),
            key=lambda i: (self.raw_points[i].lat - point.lat) ** 2 + (self.raw_points[i].lon - point.lon) ** 2,
        )
        return self.raw_fractions[best_index]


def make_rid(existing: set[int]) -> int:
    for _ in range(1000):
        rid = random.randint(100000, 999999)
        if rid not in existing:
            return rid
    rid = int(time.time()) % 900000 + 100000
    while rid in existing:
        rid = rid + 1 if rid < 999999 else 100000
    return rid


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


def create_device_backup(device_root: Path, route_ids: list[str]) -> Path:
    stamp = time.strftime("%Y%m%d_%H%M%S")
    backup = device_root / f"XOSS_Backup_{stamp}"
    suffix = 2
    while backup.exists():
        backup = device_root / f"XOSS_Backup_{stamp}_{suffix}"
        suffix += 1
    backup_routes = backup / "Routes"
    backup_routes.mkdir(parents=True)
    shutil.copy2(device_root / "routebooks.json", backup / "routebooks.json")
    for rid in route_ids:
        route_file = device_root / "Routes" / f"{rid}.ro"
        if route_file.is_file():
            shutil.copy2(route_file, backup_routes / route_file.name)
    return backup


def delete_routes_from_device(device_root: Path, route_ids: list[str]) -> Path:
    """Back up and remove selected route files and their routebook entries."""
    routebooks_path = device_root / "routebooks.json"
    document = read_routebooks(routebooks_path)
    requested_ids = list(dict.fromkeys(str(rid) for rid in route_ids))
    existing_ids = {str(route.get("rid", "")) for route in document.get("routes", [])}
    target_ids = [rid for rid in requested_ids if rid in existing_ids]
    if not target_ids:
        raise ValueError("選択したルートはすでに端末から削除されています。")
    backup = create_device_backup(device_root, target_ids)
    route_files = [device_root / "Routes" / f"{rid}.ro" for rid in target_ids]
    removed_files: list[Path] = []
    try:
        for route_file in route_files:
            if route_file.is_file():
                route_file.unlink()
                removed_files.append(route_file)
        target_id_set = set(target_ids)
        document["routes"] = [route for route in document.get("routes", []) if str(route.get("rid", "")) not in target_id_set]
        document["update_at"] = int(time.time())
        write_routebooks_atomic(routebooks_path, document)
    except Exception:
        for route_file in removed_files:
            backup_file = backup / "Routes" / route_file.name
            if backup_file.is_file():
                shutil.copy2(backup_file, route_file)
        raise
    return backup


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
) -> tuple[Path, dict, list[Path]]:
    writer = XossRouteWriter(template_path)
    staging = Path(tempfile.mkdtemp(prefix="xoss_route_converter_"))
    routes_dir = staging / "Routes"
    routes_dir.mkdir()
    routebooks = read_routebooks(staging / "routebooks.json")
    existing = {int(item.get("rid")) for item in routebooks.get("routes", []) if str(item.get("rid", "")).isdigit()}
    generated: list[Path] = []
    new_entries: list[dict] = []
    for part in parts:
        rid = make_rid(existing)
        existing.add(rid)
        part_name = title if len(parts) == 1 else f"{title} {part.number}"
        payload = writer.create(part.points, rid, part_name)
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
    backup = device_root / f"routebooks.json.bak_{time.strftime('%Y%m%d_%H%M%S')}"
    shutil.copy2(destination_json, backup)
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
        self.title("XOSS NAV+ ルート転送ツール")
        self.geometry("900x780")
        self.minsize(800, 650)
        self.gpx_path: Path | None = None
        self.gpx_title = ""
        self.points: list[TrackPoint] = []
        self.parts: list[RoutePart] = []
        self.drives: list[Path] = []
        self.staging: Path | None = None
        self._build_ui()
        self.refresh_drives()

    def _build_ui(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        root = ttk.Frame(self, padding=16)
        root.pack(fill="both", expand=True)
        root.columnconfigure(1, weight=1)
        root.rowconfigure(4, weight=1)

        ttk.Label(root, text="GPXファイル").grid(row=0, column=0, sticky="w", padx=(0, 10), pady=5)
        self.gpx_label = ttk.Label(root, text="未選択", foreground="#555")
        self.gpx_label.grid(row=0, column=1, sticky="ew", pady=5)
        ttk.Button(root, text="GPXを選択…", command=self.select_gpx).grid(row=0, column=2, padx=(10, 0), pady=5)

        ttk.Label(root, text="ルート名").grid(row=1, column=0, sticky="w", padx=(0, 10), pady=5)
        self.title_var = tk.StringVar()
        ttk.Entry(root, textvariable=self.title_var).grid(row=1, column=1, columnspan=2, sticky="ew", pady=5)

        ttk.Label(root, text="1ルートあたりの上限距離").grid(row=2, column=0, sticky="w", padx=(0, 10), pady=5)
        self.split_var = tk.StringVar(value=str(DEFAULT_SPLIT_KM))
        split_entry = ttk.Entry(root, textvariable=self.split_var, width=12)
        split_entry.grid(row=2, column=1, sticky="w", pady=5)
        ttk.Label(root, text="km（300km超のルートは自動分割）").grid(row=2, column=2, sticky="w", padx=(10, 0), pady=5)
        self.split_var.trace_add("write", lambda *_: self.update_preview())

        ttk.Label(root, text="転送先").grid(row=3, column=0, sticky="w", padx=(0, 10), pady=5)
        self.drive_var = tk.StringVar()
        self.drive_combo = ttk.Combobox(root, textvariable=self.drive_var, state="readonly")
        self.drive_combo.grid(row=3, column=1, sticky="ew", pady=5)
        self.drive_combo.bind("<<ComboboxSelected>>", lambda _event: self.refresh_device_routes())
        ttk.Button(root, text="再検索", command=self.refresh_drives).grid(row=3, column=2, padx=(10, 0), pady=5)

        preview_box = ttk.LabelFrame(root, text="分割プレビュー", padding=10)
        preview_box.grid(row=4, column=0, columnspan=3, sticky="nsew", pady=(14, 10))
        preview_box.columnconfigure(0, weight=1)
        preview_box.rowconfigure(1, weight=1)
        self.summary_label = ttk.Label(preview_box, text="GPXを選択すると距離と分割数を表示します。")
        self.summary_label.grid(row=0, column=0, sticky="w", pady=(0, 8))
        columns = ("part", "distance", "points", "gain")
        self.tree = ttk.Treeview(preview_box, columns=columns, show="headings", height=10)
        headings = {"part": "区間", "distance": "距離", "points": "点数", "gain": "獲得標高"}
        widths = {"part": 90, "distance": 180, "points": 120, "gain": 160}
        for column in columns:
            self.tree.heading(column, text=headings[column])
            self.tree.column(column, width=widths[column], anchor="center")
        scroll = ttk.Scrollbar(preview_box, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.grid(row=1, column=0, sticky="nsew")
        scroll.grid(row=1, column=1, sticky="ns")

        options = ttk.Frame(root)
        options.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(0, 8))
        ttk.Button(options, text="変換してフォルダへ保存…", command=self.save_routes).pack(side="right", padx=(8, 0))
        self.transfer_button = ttk.Button(options, text="XOSS NAV+へ転送", command=self.transfer_routes)
        self.transfer_button.pack(side="right")

        device_box = ttk.LabelFrame(root, text="端末内ルート管理", padding=10)
        device_box.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(0, 10))
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
        self.status.grid(row=7, column=0, columnspan=3, sticky="ew")
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
            self.gpx_title, self.points = parse_gpx(Path(selected))
        except (OSError, ET.ParseError, ValueError) as exc:
            messagebox.showerror("GPX読込エラー", str(exc))
            return
        self.gpx_path = Path(selected)
        self.gpx_label.configure(text=str(self.gpx_path))
        self.title_var.set(self.gpx_title)
        self.log(f"読込完了: {len(self.points):,}点、全長 {track_distance(self.points) / 1000:.1f}km")
        self.update_preview()

    def update_preview(self) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)
        if not self.points:
            return
        try:
            max_km = float(self.split_var.get().replace(",", "."))
            if max_km <= 0:
                raise ValueError
            self.parts = split_track(self.points, max_km * 1000.0)
        except ValueError:
            self.summary_label.configure(text="分割距離を正の数値で入力してください。")
            self.parts = []
            return
        total = track_distance(self.points)
        self.summary_label.configure(
            text=f"全長 {total / 1000:.2f}km ／ {len(self.parts)}ルートに分割（上限 {max_km:g}km）"
        )
        for part in self.parts:
            self.tree.insert("", "end", values=(part.number, f"{part.distance_m / 1000:.2f} km", f"{len(part.points):,}", f"{part.gain_m:,} m"))

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
            f"次の{len(route_ids)}件を端末から削除します。\n\n{preview}\n\n削除前にバックアップを作成します。続行しますか？",
        ):
            return
        try:
            backup = delete_routes_from_device(drive, route_ids)
        except (OSError, ValueError) as exc:
            messagebox.showerror("削除エラー", str(exc))
            self.refresh_device_routes()
            return
        self.refresh_device_routes()
        self.log(f"端末から{len(route_ids)}件を削除しました。バックアップ: {backup}")

    def make_staging(self) -> Path:
        if not self.parts:
            raise ValueError("GPXと分割距離を確認してください。")
        title = self.title_var.get().strip()
        if not title:
            raise ValueError("ルート名を入力してください。")
        if self.staging and self.staging.exists():
            shutil.rmtree(self.staging, ignore_errors=True)
        self.staging, _, generated = stage_routes(self.parts, title)
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
        self.log(f"転送完了: {drive}（既存routebooks.jsonのバックアップも作成）")
        messagebox.showinfo("転送完了", "転送が完了しました。NAV+を安全に取り外して、Routebookから選択してください。")


def main() -> None:
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
