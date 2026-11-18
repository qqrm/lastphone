#!/usr/bin/env python3
"""Build a tiny, synthetic real-format library for Android integration tests.

No copyrighted books, models, or personal coordinates are included. The map is at
the equator/prime meridian and all source texts explicitly say they are test data.
"""
import argparse
import json
import shutil
import sqlite3
import struct
import subprocess
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT = ROOT / "mobile/android/app/src/androidTest/assets/library"


def pdf(text: str) -> bytes:
    stream = f"BT /F1 18 Tf 40 180 Td ({text}) Tj ET".encode("ascii")
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
               b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 320 240] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
               b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
               b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"]
    body, offsets = bytearray(b"%PDF-1.4\n"), [0]
    for i, obj in enumerate(objects, 1):
        offsets.append(len(body)); body += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    start = len(body)
    body += f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode()
    body += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets[1:])
    body += f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n".encode()
    return bytes(body)


def make(destination: Path):
    destination.mkdir(parents=True, exist_ok=True)
    lib, geo = destination / "library-data", destination / "derived/geo"
    for folder in ["documents", "pages", "videos", "transcripts", "text", "assets", "metadata/videos"]:
        (lib / folder).mkdir(parents=True, exist_ok=True)
    geo.mkdir(parents=True, exist_ok=True)
    (destination / "derived/cache").mkdir(parents=True, exist_ok=True)
    (lib / "documents/fixture.pdf").write_bytes(pdf("Lastphone PDF integration fixture"))
    (lib / "text/fixture-pdf.txt").write_text("Test fixture: clean water and offline reference reading.\n", encoding="utf-8")
    (lib / "pages/fixture.html").write_text(
        '<!doctype html><title>Offline article fixture</title><h1>Offline article fixture</h1><p>Тест: вода и поход.</p>'
        '<script>parent.__untrustedExecuted=true</script><img src="https://example.invalid/tracker.png">', encoding="utf-8")
    (lib / "text/fixture-html.txt").write_text("Тестовый документ: вода, поход и автономная библиотека.", encoding="utf-8")
    (lib / "transcripts/fixture.vtt").write_text("WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nOffline video fixture\n\n00:00:01.000 --> 00:00:02.000\nSecond caption for seeking\n", encoding="utf-8")
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=darkgreen:s=160x90:r=10",
                    "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100", "-t", "2", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-movflags", "+faststart", "-metadata", "title=Lastphone synthetic fixture", str(lib / "videos/fixture.mp4")], check=True)
    records = [
        {"id": "fixture-pdf", "kind": "pdf", "path": "documents/fixture.pdf", "title": "Water test manual", "pages": 1},
        {"id": "fixture-html", "kind": "html", "path": "pages/fixture.html", "title": "Вода: тестовый документ"},
        {"id": "fixture-video", "kind": "video", "path": "videos/fixture.mp4", "title": "Offline video fixture", "duration": 2},
        {"id": "fixture-transcript", "kind": "transcript", "path": "transcripts/fixture.vtt", "title": "Offline video fixture captions", "video_path": "videos/fixture.mp4"},
    ]
    for record in records:
        record.update(collection="Test fixtures", category="test", publisher="Lastphone test suite", bytes=(lib / record["path"]).stat().st_size)
    (lib / "catalog.json").write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
    (lib / "summary.json").write_text(json.dumps({"resources": len(records)}))
    (lib / "search.sqlite").unlink(missing_ok=True)
    with sqlite3.connect(lib / "search.sqlite") as db:
        db.execute('CREATE VIRTUAL TABLE pages USING fts5(doc_id UNINDEXED,title,collection,category,path UNINDEXED,page UNINDEXED,body,tokenize="unicode61")')
        for record in records[:2]:
            text = (lib / f'text/{record["id"]}.txt').read_text(encoding="utf-8")
            db.execute("INSERT INTO pages VALUES (?,?,?,?,?,?,?)", (record["id"], record["title"], record["collection"], "test", record["path"], 1, text))
    (geo / "map.sqlite").unlink(missing_ok=True)
    with sqlite3.connect(geo / "map.sqlite") as db:
        db.execute("CREATE TABLE feat(id INTEGER PRIMARY KEY, layer TEXT, cls TEXT, name TEXT, geom BLOB)")
        db.execute("CREATE VIRTUAL TABLE feat_idx USING rtree(id,minx,maxx,miny,maxy,minz,maxz)")
        db.execute("CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT)")
        # One actual two-point road: little-endian delta encoded lon/lat * 1e6.
        geometry = struct.pack("<6i", 1, 2, 1000, -1000, 1000, -1000)
        db.execute("INSERT INTO feat VALUES (1,'road','residential','Fixture Road',?)", (geometry,))
        db.execute("INSERT INTO feat_idx VALUES (1,0.001,0.002,-0.002,-0.001,0,20)")
        db.execute("INSERT INTO meta VALUES ('built','fixture')")
    (geo / "buildings.sqlite").unlink(missing_ok=True)
    with sqlite3.connect(geo / "buildings.sqlite") as db:
        db.execute("CREATE TABLE bldg(x INTEGER,y INTEGER,data BLOB)")
        building = struct.pack("<10i", 4, 120, 536880000, 536880000, 100, 0, 0, 100, -100, 0)
        db.execute("INSERT INTO bldg VALUES (8192,8192,?)", (zlib.compress(building),))
    (destination / "pack.json").write_text(json.dumps({"id": "test", "title": "Synthetic integration fixture"}))
    print(f"Android fixture: {destination}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(); parser.add_argument("--output", type=Path, default=DEFAULT)
    make(parser.parse_args().output)
