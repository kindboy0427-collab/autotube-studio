import base64
import io
import json
import zipfile

from autotube import config, db, producer, web
from tests.base import TempHome

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


def _plan():
    scenes = [{"chapter": f"c{i}", "narration": "n", "visual": {"type": t, "prompt": f"p{i}"}}
              for i, t in enumerate(["ai_image", "real", "ai_image", "diagram"])]
    return {"title": "t", "thumbnail": {"image_prompt": "thumb"}, "scenes": scenes}


def _zip(names):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n in names:
            z.writestr(n, PNG)
    return base64.b64encode(buf.getvalue()).decode()


class ImageZipTest(TempHome):
    def _prod(self):
        out = config.OUTPUT / "p1"
        with db.connect() as con:
            con.execute("INSERT INTO productions(id, plan, output_dir, status, created_at) VALUES(?,?,?,?,?)",
                        ("p1", json.dumps(_plan()), str(out), "waiting_images", db.now()))
        return out

    def test_prompt_asks_for_ordered_zip(self):
        txt = producer.prompts_text(_plan())
        self.assertIn("순서대로 저장해서 zip", txt)
        self.assertIn("00.png", txt)

    def test_numbered_zip_fills_matching_slots(self):
        out = self._prod()
        slots = [s["n"] for s in producer.image_slots(_plan())]
        names = [f"images/{n:02d}.png" for n in slots] + ["__MACOSX/._00.png"]
        r = web.upload_images_zip("p1", web.ZipUpload(b64=_zip(names)))
        self.assertEqual(r["saved"], sorted(slots))
        for n in slots:
            self.assertIsNotNone(producer.my_image(out, n))

    def test_unnumbered_zip_fills_empty_slots_in_order(self):
        out = self._prod()
        r = web.upload_images_zip("p1", web.ZipUpload(b64=_zip(["b.png", "a.png"])))
        slots = [s["n"] for s in producer.image_slots(_plan())]
        self.assertEqual(r["saved"], slots[:2])
        self.assertIsNotNone(producer.my_image(out, slots[0]))
