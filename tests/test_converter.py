import json
import tempfile
import unittest
from pathlib import Path

import xoss_route_converter as converter


ROOT = Path(__file__).resolve().parents[1]
REFERENCE = ROOT / "ReferenceFiles"


class ConverterTests(unittest.TestCase):
    def test_reference_gpx_distance_and_split_preview(self):
        title, points = converter.parse_gpx(REFERENCE / "Original" / "倶知安to登別.gpx")
        self.assertEqual(title, "倶知安to登別")
        self.assertGreater(converter.track_distance(points), 100_000)
        parts = converter.split_track(points, 50_000)
        self.assertEqual(len(parts), 3)
        self.assertTrue(all(part.distance_m <= 50_000.01 for part in parts))

    def test_ro_output_has_xzroutes_header_and_valid_crc(self):
        _, points = converter.parse_gpx(REFERENCE / "Original" / "ムライチ.gpx")
        writer = converter.XossRouteWriter()
        payload = writer.create(points, 654321, "テストルート")
        self.assertTrue(payload.startswith(b"XZRoutes"))
        self.assertEqual(int.from_bytes(payload[8:12], "little"), 654321)
        self.assertEqual(int.from_bytes(payload[16:20], "little"), len(payload))
        self.assertEqual(int.from_bytes(payload[-2:], "little"), converter.crc16_modbus(payload[:-2]))

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

    def test_delete_device_route_creates_backup_and_removes_ro(self):
        with tempfile.TemporaryDirectory() as temp:
            device = Path(temp)
            shutil = __import__("shutil")
            shutil.copy2(REFERENCE / "routebooks.json", device / "routebooks.json")
            routes = device / "Routes"
            routes.mkdir()
            shutil.copy2(REFERENCE / "Routes" / "253228.ro", routes / "253228.ro")
            backup = converter.delete_routes_from_device(device, ["253228"])
            document = json.loads((device / "routebooks.json").read_text(encoding="utf-8"))
            self.assertNotIn(253228, [item["rid"] for item in document["routes"]])
            self.assertFalse((routes / "253228.ro").exists())
            self.assertTrue((backup / "routebooks.json").exists())
            self.assertTrue((backup / "Routes" / "253228.ro").exists())


if __name__ == "__main__":
    unittest.main()
