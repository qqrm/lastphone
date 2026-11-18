"""Native content adapters; HTTP routes and retrieval stay in the original engine."""
import hashlib
import json
import os
import sys
import threading
import types
from pathlib import Path

from lastphone_tokenizer import WordPieceTokenizer


def native_bytes(value):
    """Chaquopy byte[] and host-test bytes, including signed Java byte values."""
    try:
        return bytes(value)
    except ValueError:
        return bytes(x & 255 for x in value)


def install_modules(native):
    """Provide only the libzim API actually consumed by engine/library.py.

    Never look for Python modules in a data folder. Both the compatibility module
    and its Java implementation are APK-owned.
    """
    if native is not None:
        class Item:
            def __init__(self, archive, entry, info):
                self.archive, self.entry, self.mimetype = archive, entry, info["mimetype"]

            @property
            def content(self):
                return native_bytes(native.zimRead(self.archive, self.entry))

        class Entry:
            is_redirect = False  # NativeZim resolves redirects before returning info.

            def __init__(self, archive, entry):
                self.archive = archive
                self.info = json.loads(str(native.zimEntryInfo(archive, entry)))
                self.title, self.path = self.info["title"], self.info["path"]

            def get_item(self):
                return Item(self.archive, self.path, self.info)

        class Archive:
            def __init__(self, path):
                self.path = str(Path(path).resolve())

            @property
            def main_entry(self):
                return Entry(self.path, str(native.zimMainPath(self.path)))

            @property
            def has_fulltext_index(self):
                return bool(native.zimHasFulltext(self.path))

            def get_entry_by_path(self, entry):
                return Entry(self.path, str(entry))

        class Query:
            def set_query(self, query):
                self.query = str(query)
                return self

        class Searcher:
            def __init__(self, archive):
                self.archive = archive

            def search(self, query):
                archive = self.archive

                class Results:
                    def getResults(self, start, count):
                        rows = json.loads(str(native.zimSearch(archive.path, query.query, min(1000, start + count))))
                        return [row["path"] for row in rows[start:start + count]]
                return Results()

        package = types.ModuleType("libzim")
        package.__path__ = []
        reader, search = types.ModuleType("libzim.reader"), types.ModuleType("libzim.search")
        reader.Archive, search.Searcher, search.Query = Archive, Searcher, Query
        package.reader, package.search = reader, search
        sys.modules.update({"libzim": package, "libzim.reader": reader, "libzim.search": search})

    # pypdfium is unavailable on Android. Only its bounded-text and page-size API
    # is needed after the renderer is replaced below; pypdf is pure Python.
    module = types.ModuleType("pypdfium2")

    class PdfDocument:
        def __init__(self, path):
            from pypdf import PdfReader
            self.reader = PdfReader(str(path))

        def __len__(self):
            return len(self.reader.pages)

        def __getitem__(self, index):
            page = self.reader.pages[index]

            class Page:
                def get_textpage(self):
                    return self

                def get_text_bounded(self):
                    return page.extract_text() or ""

                def get_size(self):
                    return float(page.mediabox.width), float(page.mediabox.height)
            return Page()

        def __iter__(self):
            return (self[i] for i in range(len(self)))

        def get_page_size(self, index):
            return self[index].get_size()

        def close(self):
            self.reader.close()

    module.PdfDocument = PdfDocument
    sys.modules["pypdfium2"] = module


def configure_pdf(server, bridge, native):
    if native is None:
        return

    def render_page(record, number, width):
        width, number = max(160, min(1600, int(width))), int(number)
        if number < 1:
            raise ValueError("PDF pages start at one")
        path = (server.LIB / record["path"]).resolve()
        # All callers pass through the original file_ok/catalog path validation.
        # Repeat confinement here because native code receives a filesystem path.
        if server.LIB.resolve() not in path.parents:
            raise ValueError("PDF is outside the selected library")
        stat = path.stat()
        key = hashlib.sha256(f"{path}:{stat.st_mtime_ns}:{stat.st_size}:{number}:{width}".encode()).hexdigest()
        cache = server.CACHE / ("android-pdf-" + key + ".png")
        server.CACHE.mkdir(parents=True, exist_ok=True)
        if cache.is_file():
            return cache.read_bytes()
        with server._pdf_lock:
            data = native_bytes(native.pdfRender(str(path), number, width))
        if not data.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("Native PDF renderer returned no PNG")
        tmp = cache.with_suffix(f".{threading.get_ident()}.tmp")
        try:
            tmp.write_bytes(data)
            os.replace(tmp, cache)
        finally:
            tmp.unlink(missing_ok=True)
        return data

    def sizes(path):
        path = Path(path).resolve()
        if server.LIB.resolve() not in path.parents:
            raise ValueError("PDF is outside the selected library")
        stat = path.stat()
        key = (str(path), stat.st_mtime_ns, stat.st_size)
        with bridge.pdf_lock:
            rows = bridge.pdf_sizes.get(key)
            if rows is None:
                rows = json.loads(str(native.pdfPageSizes(str(path))))
                if len(rows) > 20000 or any(len(row) != 2 or min(row) <= 0 for row in rows):
                    raise ValueError("Invalid native PDF dimensions")
                while len(bridge.pdf_sizes) >= 16:
                    bridge.pdf_sizes.pop(next(iter(bridge.pdf_sizes)))
                bridge.pdf_sizes[key] = rows
            return rows

    server.render_page, bridge._pdf_page_sizes = render_page, sizes


def configure_retrieval(retrieval, native, stop):
    """Replace the two Android-incompatible dependencies, retain Index and RRF."""
    import numpy as np
    retrieval.np = np
    retrieval.AVAILABLE = bool(native is not None and (retrieval.MODEL / "model.onnx").is_file()
                               and (retrieval.MODEL / "tokenizer.json").is_file())
    retrieval.TELEMETRY = "Android ORT: telemetry disabled before session creation; offline model files only"

    class Embedder:
        def __init__(self):
            self.tok = WordPieceTokenizer(retrieval.MODEL / "tokenizer.json")
            info = json.loads(str(native.semanticLoad(str(retrieval.MODEL / "model.onnx"), max(1, min(4, (os.cpu_count() or 4) // 2)))))
            self.inputs = set(info.get("inputs", info) if isinstance(info, dict) else info)
            if not {"input_ids", "attention_mask"}.issubset(self.inputs):
                raise ValueError("Unsupported BGE ONNX input names")
            self.lock = threading.Lock()

        def embed(self, texts):
            if stop.is_set():
                raise RuntimeError("Library is closing")
            ids, masks = self.tok.encode_batch(texts)
            if not ids:
                return np.empty((0, retrieval.DIM), dtype=np.float32)
            types_ = [[0] * len(row) for row in ids] if "token_type_ids" in self.inputs else None
            try:
                from java import jarray, jlong
                def array(rows):
                    return jarray(jarray(jlong))([jarray(jlong)(row) for row in rows]) if rows is not None else None
                ids, masks, types_ = array(ids), array(masks), array(types_)
            except ImportError:
                pass  # host contract tests call the same adapter without a JVM.
            with self.lock:
                result = native.semanticEmbed(ids, masks, types_)
            vectors = np.asarray([list(row) for row in result], dtype=np.float32)
            if vectors.shape != (len(texts), retrieval.DIM) or not np.isfinite(vectors).all():
                raise ValueError("Invalid BGE embedding output")
            return vectors

    retrieval.Embedder = Embedder
