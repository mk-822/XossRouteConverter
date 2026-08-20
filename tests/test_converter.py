import json
import math
import tempfile
import unittest
from pathlib import Path

import xoss_route_converter as converter


ROOT = Path(__file__).resolve().parents[1]
REFERENCE = ROOT / "ReferenceFiles"


class ConverterTests(unittest.TestCase):
    def test_window_settings_round_trip(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "settings.json"
            converter.write_window_settings(path, "1280x760+40+50", "checkpoint")
            self.assertEqual(
                converter.read_window_settings(path),
                {"geometry": "1280x760+40+50", "split_mode": "checkpoint"},
            )

    def test_gpx_waypoints_include_pc_and_pass_through_checkpoints(self):
        gpx = """<?xml version="1.0" encoding="UTF-8"?>
<gpx xmlns="http://www.topografix.com/GPX/1/1" version="1.1">
  <metadata><name>チェックポイントテスト</name></metadata>
  <wpt lat="35.010" lon="139.000"><name>PC1</name><cmt>control</cmt><type>checkpoint</type></wpt>
  <wpt lat="35.020" lon="139.000"><name>通過C-A</name><type>overlook</type></wpt>
  <wpt lat="35.030" lon="139.000"><name>FINISH</name><cmt>stop</cmt><type>generic</type></wpt>
  <trk><trkseg>
    <trkpt lat="35.000" lon="139.000" />
    <trkpt lat="35.010" lon="139.000" />
    <trkpt lat="35.020" lon="139.000" />
    <trkpt lat="35.030" lon="139.000" />
  </trkseg></trk>
</gpx>
"""
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "checkpoints.gpx"
            path.write_text(gpx, encoding="utf-8")
            title, points, waypoints = converter.parse_gpx_with_waypoints(path)

        self.assertEqual(title, "チェックポイントテスト")
        self.assertEqual(len(points), 4)
        self.assertEqual([waypoint.name for waypoint in converter.checkpoint_waypoints(waypoints)], ["PC1", "通過C-A"])

    def test_split_track_at_checkpoints_uses_route_order_and_labels(self):
        points = [converter.TrackPoint(35.000 + index * 0.010, 139.000) for index in range(5)]
        checkpoints = [
            converter.GpxWaypoint("通過C-A", 35.020, 139.000, waypoint_type="overlook"),
            converter.GpxWaypoint("PC1", 35.010, 139.000, waypoint_type="checkpoint"),
        ]

        parts = converter.split_track_at_checkpoints(points, checkpoints)

        self.assertEqual([part.label for part in parts], ["START → PC1", "PC1 → 通過C-A", "通過C-A → GOAL"])
        self.assertEqual(len(parts), 3)
        self.assertAlmostEqual(sum(part.distance_m for part in parts), converter.track_distance(points), places=5)

    def test_reference_gpx_distance_and_split_preview(self):
        title, points = converter.parse_gpx(REFERENCE / "Original" / "倶知安to登別.gpx")
        self.assertEqual(title, "倶知安to登別")
        self.assertGreater(converter.track_distance(points), 100_000)
        parts = converter.split_track(points, 50_000)
        self.assertEqual(len(parts), 3)
        self.assertTrue(all(part.distance_m <= 50_000.01 for part in parts))

    def test_map_projection_and_fit_zoom(self):
        points = [converter.TrackPoint(35.0, 139.0), converter.TrackPoint(35.1, 139.2)]
        first = converter.web_mercator_pixel(points[0], 10)
        last = converter.web_mercator_pixel(points[1], 10)
        self.assertLess(first[0], last[0])
        self.assertLess(last[1], first[1])
        zoom = converter.map_fit_zoom(points, 700, 600)
        self.assertGreaterEqual(zoom, converter.MAP_MIN_ZOOM)
        self.assertLessEqual(zoom, converter.MAP_MAX_ZOOM)

    def test_route_part_colors_are_distinct_for_split_preview(self):
        colors = [converter.route_part_color(index, 3) for index in range(3)]
        self.assertEqual(len(set(colors)), 3)
        self.assertEqual(converter.route_part_color(0, 1), converter.MAP_ROUTE_COLORS[0])

    def test_zoomed_center_keeps_cursor_position_fixed(self):
        center = (1_000.0, 800.0)
        width, height = 800.0, 600.0
        focus = (200.0, 150.0)
        new_center = converter.zoomed_center(center, 10, 11, width, height, *focus)

        old_focus_world = (center[0] + focus[0] - width / 2, center[1] + focus[1] - height / 2)
        new_focus_world = (
            new_center[0] + focus[0] - width / 2,
            new_center[1] + focus[1] - height / 2,
        )
        self.assertEqual(new_focus_world, (old_focus_world[0] * 2, old_focus_world[1] * 2))

    def test_inferred_ro_flags_follow_route_turns_and_finish(self):
        def local_point(x_m, y_m):
            return converter.TrackPoint(
                35.0 + y_m / 111_000.0,
                139.0 + x_m / (111_000.0 * math.cos(math.radians(35.0))),
            )

        points = []
        points.extend(local_point(0, y) for y in range(0, 501, 50))
        points.extend(local_point(x, 500) for x in range(50, 501, 50))
        points.extend(local_point(500, y) for y in range(450, -1, -50))
        points.extend(local_point(x, 0) for x in range(550, 1_001, 50))

        segments = converter.infer_route_segments(points)
        flags = [flag for _segment, flag in segments]

        self.assertEqual(flags, [converter.RO_FLAG_RIGHT, converter.RO_FLAG_RIGHT, converter.RO_FLAG_LEFT, converter.RO_FLAG_FINISH])
        self.assertEqual([len(segment) for segment, _flag in segments], [11, 11, 11, 11])

        payload = converter.XossRouteWriter().create(points, 654321, "推定フラグ")
        record_count = int.from_bytes(payload[0x16:0x18], "little")
        output_flags = [
            int.from_bytes(payload[0x60 + index * 44 + 20 : 0x60 + index * 44 + 22], "little")
            for index in range(record_count)
        ]
        self.assertEqual(output_flags, flags)

    def test_ro_output_has_xzroutes_header_and_valid_crc(self):
        _, points = converter.parse_gpx(REFERENCE / "Original" / "ムライチ.gpx")
        writer = converter.XossRouteWriter()
        payload = writer.create(points, 654321, "テストルート")
        self.assertTrue(payload.startswith(b"XZRoutes"))
        self.assertEqual(int.from_bytes(payload[8:12], "little"), 654321)
        self.assertEqual(int.from_bytes(payload[16:20], "little"), len(payload))
        self.assertEqual(int.from_bytes(payload[-2:], "little"), converter.crc16_modbus(payload[:-2]))

    def test_generated_ro_has_consistent_variable_length_sections(self):
        writer = converter.XossRouteWriter()
        payloads = []
        for source in ("ムライチ.gpx", "倶知安to登別.gpx"):
            _, points = converter.parse_gpx(REFERENCE / "Original" / source)
            payload = writer.create(points, 654321, "テストルート")
            payloads.append(payload)

            record_count = int.from_bytes(payload[0x16:0x18], "little")
            table_end = 0x60 + record_count * 44
            point_counts = []
            first_point_offset = None
            for index in range(record_count):
                offset = 0x60 + index * 44
                point_counts.append(int.from_bytes(payload[offset + 22 : offset + 24], "little"))
                point_offset = int.from_bytes(payload[offset + 24 : offset + 28], "little")
                start_lat, start_lon = converter.XossRouteWriter._read_point(payload, offset).lat, converter.XossRouteWriter._read_point(payload, offset).lon
                end_lat, end_lon = converter.XossRouteWriter._read_point(payload, offset + 8).lat, converter.XossRouteWriter._read_point(payload, offset + 8).lon
                min_lat, min_lon = converter.XossRouteWriter._read_point(payload, offset + 28).lat, converter.XossRouteWriter._read_point(payload, offset + 28).lon
                max_lat, max_lon = converter.XossRouteWriter._read_point(payload, offset + 36).lat, converter.XossRouteWriter._read_point(payload, offset + 36).lon
                self.assertEqual((min_lat, min_lon), (min(start_lat, end_lat), min(start_lon, end_lon)))
                self.assertEqual((max_lat, max_lon), (max(start_lat, end_lat), max(start_lon, end_lon)))
                if first_point_offset is None:
                    first_point_offset = point_offset
            point_end = int.from_bytes(payload[0x30:0x34], "little")
            self.assertEqual(first_point_offset, table_end)
            self.assertEqual(point_end, table_end + sum(point_counts) * 8)

            elev_data_offset = int.from_bytes(payload[0x18:0x1C], "little")
            elev_count = int.from_bytes(payload[0x1C:0x20], "little")
            self.assertEqual(elev_count, len(points))
            self.assertEqual(payload[elev_data_offset - 12 : elev_data_offset - 8], b"ELEV")
            self.assertEqual(len(payload) - 2 - elev_data_offset, elev_count * 8)
            self.assertEqual(int.from_bytes(payload[-2:], "little"), converter.crc16_modbus(payload[:-2]))

        _, short_points = converter.parse_gpx(REFERENCE / "Original" / "倶知安to登別.gpx")
        compact_payload = writer.create(short_points[:100], 654321, "短いテスト")
        self.assertLess(len(compact_payload), len(payloads[1]))

    def test_ro_output_can_be_thinned_to_requested_point_count(self):
        _, points = converter.parse_gpx(REFERENCE / "Original" / "倶知安to登別.gpx")
        writer = converter.XossRouteWriter()
        payload = writer.create(points, 654321, "間引きテスト", max_points=1_000)
        full_payload = writer.create(points, 654321, "全点")
        self.assertEqual(int.from_bytes(payload[0x1C:0x20], "little"), 1_000)
        self.assertLess(len(payload), len(full_payload))
        self.assertEqual(int.from_bytes(payload[-2:], "little"), converter.crc16_modbus(payload[:-2]))

    def test_output_point_limit_accepts_only_configured_range(self):
        _, points = converter.parse_gpx(REFERENCE / "Original" / "倶知安to登別.gpx")
        self.assertEqual(len(converter.limit_route_points(points, 24_000)), len(points))
        with self.assertRaises(ValueError):
            converter.limit_route_points(points, 999)
        with self.assertRaises(ValueError):
            converter.limit_route_points(points, 24_001)

    def test_split_track_can_be_disabled(self):
        _, points = converter.parse_gpx(REFERENCE / "Original" / "倶知安to登別.gpx")
        parts = converter.split_track(points, None)
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0].points, points)
        self.assertAlmostEqual(parts[0].distance_m, converter.track_distance(points))

    def test_reference_ro_files_have_valid_crc(self):
        for path in (REFERENCE / "Routes").glob("*.ro"):
            payload = path.read_bytes()
            self.assertEqual(
                int.from_bytes(payload[-2:], "little"),
                converter.crc16_modbus(payload[:-2]),
                path.name,
            )

    def test_staging_routebooks_has_matching_sizes(self):
        _, points = converter.parse_gpx(REFERENCE / "Original" / "ムライチ.gpx")
        parts = converter.split_track(points, 300_000)
        with tempfile.TemporaryDirectory() as temp:
            staging, document, files = converter.stage_routes(parts, "テストルート")
            self.addCleanup(lambda: __import__("shutil").rmtree(staging, ignore_errors=True))
            self.assertEqual(len(files), 1)
            entries = document["routes"]
            self.assertEqual(entries[-1]["size"], files[0].stat().st_size)
            self.assertEqual(json.loads((staging / "routebooks.json").read_text(encoding="utf-8"))["routes"][-1]["rid"], entries[-1]["rid"])

    def test_transfer_does_not_create_routebooks_backup(self):
        _, points = converter.parse_gpx(REFERENCE / "Original" / "倶知安to登別.gpx")
        parts = converter.split_track(points[:2], None)
        with tempfile.TemporaryDirectory() as temp:
            device = Path(temp)
            shutil = __import__("shutil")
            shutil.copy2(REFERENCE / "routebooks.json", device / "routebooks.json")
            staging, _, _ = converter.stage_routes(parts, "転送テスト")
            self.addCleanup(lambda: shutil.rmtree(staging, ignore_errors=True))
            converter.transfer_staging(staging, device)
            self.assertFalse(any(device.glob("routebooks.json.bak_*")))

    def test_split_route_rids_are_consecutive_and_names_are_numbered(self):
        _, points = converter.parse_gpx(REFERENCE / "Original" / "倶知安to登別.gpx")
        parts = converter.split_track(points, 30_000)
        staging, document, files = converter.stage_routes(parts, "非常に長いルート名を指定した場合でも連番を保持")
        self.addCleanup(lambda: __import__("shutil").rmtree(staging, ignore_errors=True))
        entries = document["routes"][-len(parts) :]
        rids = [entry["rid"] for entry in entries]
        self.assertEqual(rids, list(range(rids[0], rids[0] + len(parts))))
        expected_names = [
            converter.route_part_name("非常に長いルート名を指定した場合でも連番を保持", index, len(parts))
            for index in range(1, len(parts) + 1)
        ]
        self.assertEqual([entry["name"] for entry in entries], expected_names)
        self.assertTrue(all(name.endswith(f"{index:02d}") for index, name in enumerate(expected_names, start=1)))
        self.assertTrue(all(len(name.encode("utf-8")) <= 31 for name in expected_names))
        self.assertEqual(len(files), len(parts))

    def test_delete_device_route_removes_ro_without_backup(self):
        with tempfile.TemporaryDirectory() as temp:
            device = Path(temp)
            shutil = __import__("shutil")
            shutil.copy2(REFERENCE / "routebooks.json", device / "routebooks.json")
            routes = device / "Routes"
            routes.mkdir()
            shutil.copy2(REFERENCE / "Routes" / "253228.ro", routes / "253228.ro")
            result = converter.delete_routes_from_device(device, ["253228"])
            document = json.loads((device / "routebooks.json").read_text(encoding="utf-8"))
            self.assertNotIn(253228, [item["rid"] for item in document["routes"]])
            self.assertFalse((routes / "253228.ro").exists())
            self.assertIsNone(result)
            self.assertFalse(any(device.glob("XOSS_Backup_*")))


if __name__ == "__main__":
    unittest.main()
