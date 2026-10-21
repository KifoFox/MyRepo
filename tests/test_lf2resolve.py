import json
import struct
import sys
import tempfile
import unittest
import zipfile
from fractions import Fraction
from pathlib import Path
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import lf2resolve  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "test_project.lfarchive"
FIXTURE_EFFECTS = Path(__file__).parent / "fixtures" / "test_project_effects.lfarchive"
MEDIA = [("20261019_194920.mp4", 14.85), ("20261019_212354.mp4", 5.087), ("20261019_215836.mp4", 4.69)]


def fake_mp4(path, seconds, timescale=600):
    """Write a minimal MP4 containing only a moov/mvhd atom."""
    mvhd_body = bytes(4) + struct.pack(">IIII", 0, 0, timescale, int(seconds * timescale)) + bytes(80)
    mvhd = struct.pack(">I4s", 8 + len(mvhd_body), b"mvhd") + mvhd_body
    moov = struct.pack(">I4s", 8 + len(mvhd), b"moov") + mvhd
    ftyp = struct.pack(">I4s", 16, b"ftyp") + b"isom" + bytes(4)
    path.write_bytes(ftyp + moov)


class DecodeTest(unittest.TestCase):
    def setUp(self):
        self.archive = json.loads(FIXTURE.read_text())
        self.tmp = tempfile.TemporaryDirectory()
        self.media = Path(self.tmp.name)
        for name, secs in MEDIA:
            fake_mp4(self.media / name, secs)
        self.project = lf2resolve.Decoder(self.archive, self.media).decode()

    def tearDown(self):
        self.tmp.cleanup()

    def track(self, offset):
        return next(t for t in self.project.tracks if t.offset == offset)

    def test_project_settings(self):
        p = self.project
        self.assertEqual((p.width, p.height), (3840, 2160))
        self.assertEqual(p.rate.fps, 30)
        self.assertEqual(p.duration, Fraction(121, 10))
        self.assertEqual(sorted(t.offset for t in p.tracks), [0, 1])

    def test_main_track_transition_straddles_cut(self):
        t = self.track(0)
        a, b = t.items
        (tr,) = t.transitions
        self.assertEqual(tr.alignment, "center")
        self.assertEqual((tr.start, tr.end), (Fraction(59, 6), Fraction(65, 6)))
        cut = (tr.start + tr.end) / 2
        self.assertEqual((a.start, a.end, b.start, b.end), (0, cut, cut, Fraction(121, 10)))
        # Incoming clip borrows half the transition from its head.
        self.assertEqual(b.src_in, Fraction(12567, 1000) - Fraction(1, 2))
        self.assertEqual(a.src_in, Fraction(1001, 1000))

    def test_overlay_track(self):
        t = self.track(1)
        a, b = t.items
        self.assertEqual(a.start, Fraction(28, 30))
        self.assertAlmostEqual(lf2resolve.db(a.volume), -90, places=1)
        self.assertEqual(b.src_in, 0)
        self.assertEqual(b.end, Fraction(49, 6))

    def test_no_warnings_for_supported_project(self):
        self.assertEqual(self.project.warnings, [])

    def test_xml(self):
        out = Path(self.tmp.name) / "out.xml"
        lf2resolve.XmemlWriter(self.project).write(out)
        root = ET.parse(out).getroot()
        seq = root.find("sequence")
        self.assertEqual(seq.findtext("duration"), "363")
        vtracks = seq.findall("media/video/track")
        atracks = seq.findall("media/audio/track")
        self.assertEqual((len(vtracks), len(atracks)), (2, 2))
        v1 = vtracks[0]
        clips = v1.findall("clipitem")
        self.assertEqual([(c.findtext("start"), c.findtext("end")) for c in clips],
                         [("0", "310"), ("310", "363")])
        self.assertEqual(v1.find("transitionitem/alignment").text, "center")
        # Muted clip carries an Audio Levels filter.
        levels = atracks[1].find("clipitem/filter/effect/parameter/value").text
        self.assertAlmostEqual(float(levels), 3.16228e-05, places=8)
        # Each file is fully described exactly once.
        described = [f for f in root.iter("file") if f.find("pathurl") is not None]
        self.assertEqual(len(described), 3)


class FeaturesTest(unittest.TestCase):
    """Second sample: title, speed + reverse, keyframes, effects, crop/opacity."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        media = Path(self.tmp.name)
        for name, secs in MEDIA:
            fake_mp4(media / name, secs)
        archive = json.loads(FIXTURE_EFFECTS.read_text())
        self.project = lf2resolve.Decoder(archive, media).decode()

    def tearDown(self):
        self.tmp.cleanup()

    def clip(self, title):
        return next(i for t in self.project.tracks for i in t.items if i.title == title)

    def test_speed_and_reverse(self):
        it = self.clip("20261019_212354.mp4")
        self.assertEqual(it.speed, Fraction(53, 50))
        self.assertTrue(it.reversed)
        src_in, src_out = it.file_range()
        # Source covers timeline length x speed, mirrored from the reversed copy.
        self.assertEqual(src_out - src_in, (it.end - it.start) * it.speed)
        self.assertEqual(src_out, it.media.duration - it.src_in)

    def test_title_becomes_marker(self):
        (mk,) = self.project.markers
        self.assertEqual(mk.name, "Title: Your Text Here")
        self.assertEqual((mk.start, mk.end), (Fraction(4, 3), Fraction(16, 3)))
        self.assertIn("#FFFF00", mk.comment)

    def test_rebuild_notes(self):
        notes = "; ".join(self.clip("20261019_215836.mp4").notes)
        self.assertIn("Keyframed Scale, TransformRotation, Translation (4 keyframes)", notes)
        main_b = self.project.tracks[-1].items[-1]
        notes = "; ".join(main_b.notes)
        for needle in ("Zoom 1.485", "Rotation 7.3°", "Crop to 82.9% x 67.2%", "Gaussian 10", "Person Key"):
            self.assertIn(needle, notes)
        self.assertAlmostEqual(main_b.opacity, 0.519)

    def test_xml(self):
        out = Path(self.tmp.name) / "out.xml"
        lf2resolve.XmemlWriter(self.project).write(out)
        seq = ET.parse(out).getroot().find("sequence")
        remaps = [e for e in seq.iter("effect") if e.findtext("effectid") == "timeremap"]
        self.assertEqual(len(remaps), 2)  # video + audio of the sped-up clip
        params = {p.findtext("parameterid"): p.findtext("value") for p in remaps[0].iter("parameter")}
        self.assertEqual((params["speed"], params["reverse"]), ("106", "TRUE"))
        opacity = [e for e in seq.iter("effect") if e.findtext("effectid") == "opacity"]
        self.assertEqual(opacity[0].findtext("parameter/value"), "51.9")
        names = [m.findtext("name") for m in seq.findall("marker")]
        self.assertEqual(names, ["Title: Your Text Here", "Rebuild: 20261019_215836.mp4",
                                 "Rebuild: 20261019_194920.mp4"])


class PackageTest(unittest.TestCase):
    def test_convert_package_end_to_end(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            pkg = d / "My Project.lfpackage"
            mp4 = d / "src.mp4"
            fake_mp4(mp4, 15)
            with zipfile.ZipFile(pkg, "w") as z:
                z.write(FIXTURE, "test project.lfarchive")
                for name in ("20261019_194920.mp4", "20261019_212354.mp4", "20261019_215836.mp4"):
                    z.write(mp4, name)
                z.writestr("../evil.mp4", b"x")
            self.assertEqual(lf2resolve.main([str(pkg)]), 0)
            out = d / "My Project (Resolve)"
            self.assertTrue((out / "test project.xml").exists())
            self.assertTrue((out / "media" / "20261019_194920.mp4").exists())
            self.assertFalse((d / "evil.mp4").exists())

    def test_mp4_duration(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "a.mp4"
            fake_mp4(p, 4.5)
            self.assertEqual(lf2resolve.mp4_duration(p), Fraction(9, 2))

    def test_path_url_escapes(self):
        url = lf2resolve.path_url(Path("/tmp/My Project (Resolve)/a b.mp4"))
        self.assertEqual(url, "file://localhost/tmp/My%20Project%20%28Resolve%29/a%20b.mp4")


if __name__ == "__main__":
    unittest.main()
