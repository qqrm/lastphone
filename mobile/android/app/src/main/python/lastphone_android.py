"""Android embedding of the unmodified Lastbook request/retrieval/setup core.

Only platform facilities live here: APK/data separation, native dependencies,
WebView transport, lifecycle, model settings and explicitly enabled LAN sharing.
No endpoint whitelist or second implementation of maps/search/chat lives here.
"""
import importlib
import json
import os
import secrets
import socket
import sqlite3
import sys
import threading
import time
import types
import urllib.parse
import weakref
from pathlib import Path

from lastphone_content import configure_pdf, configure_retrieval, install_modules, native_bytes
from lastphone_llm import NativeLocal, attach

ORIGIN = "https://lastphone.invalid"
_CURRENT = None
_CONFIG_LOCK = threading.RLock()


def _module(name):
    return importlib.reload(sys.modules[name]) if name in sys.modules else importlib.import_module(name)


class AndroidResponse:
    """Keep original bridge accounting until Android closes its InputStream."""
    def __init__(self, response, engine=None):
        from app.bridge import headers_dict
        self.status, self.reason = response.status, response.reason or "Response"
        self.headers_json = json.dumps(headers_dict(response.headers))
        self.body = response.body
        self.path = str(response.path) if response.path is not None else None
        self._response, self._lock = response, threading.Lock()
        if engine is not None:
            with engine._responses_lock:
                engine._responses.add(self)

    def close(self):
        with self._lock:
            response, self._response = self._response, None
        if response is not None:
            try:
                response.release()
            finally:
                if response.delete and response.path:
                    Path(response.path).unlink(missing_ok=True)

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


class _Runtime:
    def __init__(self):
        self.stop = threading.Event()
        self.condition = threading.Condition()
        self.active = 0
        self.index_thread = None


def _close_connections(server):
    """Thread-local read handles must not outlive the selected portable folder."""
    for holder in (getattr(server, "_tls", None), getattr(getattr(server, "INDEX", None), "_tls", None)):
        if holder is None:
            continue
        for name, value in list(vars(holder).items()):
            if isinstance(value, sqlite3.Connection):
                value.close()
                delattr(holder, name)


def _owned_handler(base, runtime, server):
    class Handler(base):
        def __init__(self, *args, **kwargs):
            with runtime.condition:
                if runtime.stop.is_set():
                    return
                runtime.active += 1
            try:
                super().__init__(*args, **kwargs)
            finally:
                _close_connections(server)
                with runtime.condition:
                    runtime.active -= 1
                    runtime.condition.notify_all()
    return Handler


class Engine:
    def __init__(self, program_dir, data_dir, settings_dir, native=None, callbacks=None):
        self.program = Path(program_dir).resolve()
        self.settings = Path(settings_dir).resolve()
        if not (self.program / "web" / "index.html").is_file():
            raise ValueError("The APK's extracted engine assets are incomplete")
        self.settings.mkdir(parents=True, exist_ok=True)
        self.native, self.callbacks = native, callbacks
        self._lifecycle = threading.RLock()
        self._responses_lock, self._responses = threading.Lock(), weakref.WeakSet()
        self._runtime, self.bridge, self.local, self.setup, self.server = None, None, None, None, None
        self._lan, self._lan_thread, self._lan_info = None, None, None
        self._closed = False
        self._load(Path(data_dir).resolve())

    def _load(self, data):
        data.mkdir(parents=True, exist_ok=True)
        if data == self.program or self.program in data.parents or data in self.program.parents:
            raise ValueError("Keep the portable library separate from the APK's program files")
        # This is an embedded packaged application. Original library.py must
        # never add library-data/tools/vendor or remote_storage.py to imports.
        sys.frozen, sys._MEIPASS = True, str(self.program)
        os.environ.update(LASTBOOK_DATA=str(data), LASTBOOK_LIBRARY=str(data / "library-data"),
                          LASTBOOK_LOCAL_AI="0", SURVIVAL_DISABLE_INDEX_WATCH="1", SURVIVAL_PDF_ENGINE="pdfium",
                          ORT_DISABLE_TELEMETRY="1")
        install_modules(self.native)
        from engine.setup import api, manifest, hardware, download
        pending, note = api.startup(self.settings, only=data)
        self.data = Path(pending).resolve() if pending else data
        os.environ["LASTBOOK_DATA"] = str(self.data)
        os.environ["LASTBOOK_LIBRARY"] = str(self.data / "library-data")
        paths = _module("engine.paths")
        library = _module("engine.library")
        # The only code loaded is packaged by Gradle from the upstream revision.
        if not library.FROZEN or str(library.VENDOR) in sys.path:
            raise RuntimeError("Portable library code must never enter Python's import path")
        runtime = self._runtime = _Runtime()
        retrieval = _module("retrieval")
        configure_retrieval(retrieval, self.native, runtime.stop)
        assistant = _module("assistant")
        llm = _module("llm_local")
        paths_list = [self.program / "setup" / "manifest-v1.json", self.program / "engine" / "setup" / "manifest-v1.json"]
        bundled = manifest.load_bundled(paths_list)
        self.local = NativeLocal(self.native, self.data, self.settings, llm, bundled.get("models", []))
        attach(assistant, llm, self.local)
        # Use the same installer, pins, rollback and repair. Opening its state is
        # offline; a remote manifest is not fetched as a side effect of startup.
        self.manifest = manifest.Manifest(bundled=bundled, remote=None)
        self._select_setup_model()
        download.roots_path = lambda: self.program / "engine" / "setup" / "roots.pem"
        self._platform_hardware(hardware)
        def checked(path, sha256):
            p = Path(path).resolve()
            entry = next((m for m in self.local.models if m["file"] == p.name), None)
            if (entry is None or self.data not in p.parents or p.stat().st_size != entry["size"]
                    or sha256 != entry["sha256"]):
                raise ValueError("Not a published model in the selected library")
            self.local.saved["checked"] = [str(p), p.stat().st_size, p.stat().st_mtime_ns]
            self.local._save()
        llm.mark_checked = checked
        has_catalog = (paths.LIB / "catalog.json").is_file()
        ready = has_catalog and (paths.LIB / "search.sqlite").is_file()
        self.setup = api.configure(mode="manage" if has_catalog else "first-run", settings_dir=self.settings,
            default_parent=self.data.parent, current=self.data if has_catalog else None,
            manifest=self.manifest, on_finish=self._finished_setup, pick_folder=self._pick_folder,
            look_for_libraries=lambda: [self.data] if (self.data / "library-data" / "catalog.json").is_file() else [],
            home=self.data.parent, llm_local=llm)
        if ready:
            server = self.server = _module("server")
            server.catalog()
            try:
                server.INDEX = retrieval.Index(server.catalog, server.trust, library.DB)
            except Exception as error:
                # A bad/missing optional model must not prevent access to manuals.
                # Keep the explicit error in status, and retry when the user fixes
                # the model and reopens this library. Do not claim semantic parity.
                retrieval.AVAILABLE = False
                server.INDEX = retrieval.Index(server.catalog, server.trust, library.DB)
                server.INDEX.status.update(state="error", note="Semantic model could not load: " + type(error).__name__)
            assistant.init(server, server.INDEX)
            attach(assistant, llm, self.local)
            server.Handler = _owned_handler(server.Handler, runtime, server)
            target = server
            api.opened(self.settings, self.data, note)
        else:
            self.server = None
            target = types.SimpleNamespace(Handler=_owned_handler(api.SetupHandler, runtime, None))
        from app import bridge as B
        class Bridge(B.Bridge):
            def _grow_lanes(self):
                while not runtime.stop.is_set():
                    self.waiting_now.wait(0.2)
                    if runtime.stop.wait(B.GROW_AFTER):
                        break
                    self.waiting_now.clear()
                    if any([lane.grow() for lane in self.lanes.values()]):
                        self.waiting_now.set()
        temporary = self.data / "derived" / "android-tmp"
        temporary.mkdir(parents=True, exist_ok=True)
        self.bridge = Bridge(target, "buffer", ORIGIN, tmpdir=str(temporary))
        self.bridge.meta_redirects = True  # WebResourceResponse cannot carry 3xx.
        if ready:
            configure_pdf(self.server, self.bridge, self.native)
            runtime.index_thread = threading.Thread(target=self._index_loop, args=(runtime, self.server), daemon=True, name="android-semantic-index")
            runtime.index_thread.start()
        self._configure_updater()
        self.start_url = ORIGIN + ("/" if ready else "/ui/setup.html")
        self.local.start_background()

    def _platform_hardware(self, hardware):
        if not hasattr(hardware, "_android_original_detect"):
            hardware._android_original_detect = hardware.detect
        original = hardware._android_original_detect
        def detect(extra=()):
            value = original(extra)
            value.update(os="android", unified_memory=True, ai_runtime=self.local.settings["backend"])
            if self.native is not None:
                try:
                    info = json.loads(str(self.native.llmCapabilities()))
                    value["ram_bytes"] = info.get("ram_total") or value.get("ram_bytes")
                    value["gpus"] = [{"name": d.get("description", d.get("name", "Vulkan")), "vram_bytes": 0}
                                     for d in info.get("devices", []) if str(d.get("name", "")).startswith("Vulkan")]
                except Exception:
                    pass
            if not value.get("ram_bytes"):
                try:
                    value["ram_bytes"] = hardware._linux_ram()
                except OSError:
                    pass
            return value
        def measure(_llm, path, timeout=180):
            if self.native is None:
                return None
            info = json.loads(str(self.native.llmMeasure(str(path), json.dumps(self.local.options()))))
            if info.get("ev") == "error":
                return {"fits": False, "needs_bytes": None, "estimate": False, "why": info.get("message", "Native model measurement failed.")}
            fits = info.get("status") == "fits"
            needs = int(info.get("host_bytes", 0)) + int(info.get("device_bytes", 0))
            return {"fits": fits, "needs_bytes": needs, "estimate": False,
                    "why": "The selected model and context fit available memory." if fits else "The selected model/context do not fit available memory. Choose a smaller model or context."}
        hardware.detect, hardware.measure_ai = detect, measure

    def _select_setup_model(self):
        models = self.manifest.current.get("models", [])
        selected = self.local.settings.get("setup_model")
        if not selected:
            selected = next((m["id"] for m in models if self.local.settings.get("path", "").endswith("/" + m["file"])), None)
        if not selected and models:
            selected = min((m for m in models if m.get("available")), key=lambda m: m["size"], default=models[0])["id"]
        models.sort(key=lambda m: (m["id"] != selected, m["size"]))
        if selected:
            self.local.settings["setup_model"] = selected

    def _configure_updater(self):
        from engine import update
        callbacks = self.callbacks
        try:
            version = str(callbacks.appVersion()) if callbacks is not None and hasattr(callbacks, "appVersion") else (self.program / ".complete").read_text().strip()
        except (OSError, AttributeError):
            version = "0.1.0"
        class AndroidUpdater(update.Updater):
            def __init__(self):
                super().__init__(key="android", version=version)

            def state(self):
                result = super().state()
                result["website"] = "github.com/qqrm/lastphone/releases"
                return result

            def _check(self):
                if callbacks is None or not hasattr(callbacks, "checkUpdate"):
                    return self._set(state="error", code="platform", message="The Android update transport is unavailable.")
                try:
                    response = json.loads(str(callbacks.checkUpdate()))
                    if response["status"] == 404:
                        return self._set(state="not_published")
                    if response["status"] != 200 or len(response.get("body", "").encode()) > 256 * 1024:
                        raise ValueError("Release metadata was not available")
                    release = json.loads(response["body"])
                    tag = str(release.get("tag_name", "")).removeprefix("v")
                    if not update._ver(tag):
                        raise ValueError("Release version is not a numeric Android version")
                    assets = release.get("assets") or []
                    if release.get("draft") or release.get("prerelease") or not any(
                        str(a.get("name", "")).endswith(".apk") and a.get("size", 0) > 0
                        and str(a.get("browser_download_url", "")).startswith("https://github.com/qqrm/lastphone/releases/download/") for a in assets):
                        return self._set(state="not_published")
                    if update._ver(tag) <= (update._ver(version) or (0,)):
                        return self._set(state="up_to_date", latest=tag)
                    self._set(state="available", version=tag)
                except Exception as error:
                    self._set(state="error", code="network", message="Could not check Lastphone releases (" + type(error).__name__ + "). The offline library is still available.")
        update.UPDATER = AndroidUpdater()

    @staticmethod
    def _index_loop(runtime, server):
        index = server.INDEX
        try:
            if index.emb is None:
                return
            while not runtime.stop.is_set():
                try:
                    # Each document remains one atomic index transaction. The
                    # native embedder checks stop between bounded batches.
                    index.update(budget_s=2, batch=8)
                    index.status.pop("error", None)
                except Exception as error:
                    if runtime.stop.is_set():
                        break
                    index.status.update(state="error", error=type(error).__name__)
                wait = 0.2 if index.status.get("pending") and index.status.get("state") != "error" else 300
                if runtime.stop.wait(wait):
                    break
        finally:
            _close_connections(server)

    def _pick_folder(self, files=False):
        if self.callbacks is None:
            return None
        result = self.callbacks.pickFolder(bool(files))
        return str(result) if result else None

    def _finished_setup(self, path):
        if self.callbacks is not None:
            self.callbacks.openLibrary(str(path))

    def top_script(self):
        return self.bridge.top_script()

    def grant_navigation(self, url):
        self.bridge.give_ticket(str(url))

    def navigation(self, url, is_top=True, gesture=False):
        url = str(url)
        if is_top and self.bridge.top_url_ok(url):
            return json.dumps({"action": "allow", "url": url})
        if not is_top:
            return json.dumps({"action": "allow" if self.bridge.frame_url_ok(url) else "block", "url": url})
        try:
            action, target = self.bridge.new_window(url, True, bool(gesture))
        except AttributeError:  # First-run setup has no library catalog yet.
            action, target = "nothing", None
        return json.dumps({"action": action, "url": target})

    def request(self, method, url, headers_json="{}", body=None, is_document=None):
        from app import bridge as B
        def unavailable():
            return AndroidResponse(B.Response(503, "Library changing", [("Content-Type", "text/plain")], body=b"The library is changing. Retry this request."))
        headers = json.loads(str(headers_json or "{}"))
        if not isinstance(headers, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in headers.items()):
            return AndroidResponse(B.Response(400, "Bad headers", [], body=b"Invalid request headers"))
        received, completed = [], threading.Event()
        with self._lifecycle:
            if self._closed or self._runtime.stop.is_set():
                return unavailable()
            runtime, bridge = self._runtime, self.bridge
            def done(response):
                if runtime.stop.is_set():
                    AndroidResponse(response).close()
                else:
                    received.append(response)
                completed.set()
            exchange = bridge.fetch(str(method), str(url), headers, native_bytes(body) if body is not None else None, done, is_document)
        while not completed.wait(0.2):
            if runtime.stop.is_set():
                if exchange:
                    exchange.cancel()
                return unavailable()
        return AndroidResponse(received[0], self) if received else unavailable()

    def model_settings(self):
        return json.dumps(self.local.public_settings())

    def set_model_settings(self, value):
        with self._lifecycle:
            if self.setup and self.setup.thread and self.setup.thread.is_alive():
                raise RuntimeError("Pause the active installation before changing models")
            result = self.local.set_settings(json.loads(str(value)))
            import assistant, llm_local
            attach(assistant, llm_local, self.local)
            self._select_setup_model()
            self.setup.ai_measure, self.setup.measuring = None, False
            return json.dumps(result)

    def work_state(self):
        with self._runtime.condition:
            chat = bool(self.local.generating or self.local.state in ("loading", "checking"))
        if self.bridge:
            with self.bridge.stream_lock:
                chat = chat or bool(self.bridge.streams)
        setup = bool(self.setup and self.setup.thread and self.setup.thread.is_alive())
        lan = self._lan is not None
        return json.dumps({"running": chat or setup or lan, "chat": chat, "setup": setup, "lan": lan})

    def cancel_work(self):
        self.local.cancel()
        if self.bridge:
            with self.bridge.stream_lock:
                streams = list(self.bridge.streams.values())
                self.bridge.streams.clear()
            for stream in streams:
                stream["ex"].cancel()
        self.local.stop("asked")
        if self.setup:
            self.setup.pause()  # keeps verified bytes; never deletes the install.
        self.stop_lan()
        return self.work_state()

    def start_lan(self, port=0):
        with self._lifecycle:
            if not self.server:
                raise RuntimeError("Open a library before sharing it")
            if self._lan is not None:
                return json.dumps(self._lan_info)
            if isinstance(port, bool) or not 0 <= int(port) <= 65535:
                raise ValueError("Invalid port")
            from http.server import ThreadingHTTPServer
            self.server.LAN_KEY = secrets.token_urlsafe(24)
            self._lan = ThreadingHTTPServer(("0.0.0.0", int(port)), self.server.Handler)
            self._lan.daemon_threads = True
            self._lan_thread = threading.Thread(target=self._lan.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True, name="android-lan-sharing")
            self._lan_thread.start()
            address = "127.0.0.1"
            try:
                addresses = json.loads(str(self.callbacks.localAddresses())) if self.callbacks is not None else []
                import ipaddress
                address = next((str(ipaddress.IPv4Address(value)) for value in addresses
                                if not ipaddress.ip_address(value).is_loopback), address)
            except (OSError, AttributeError, ValueError):
                pass
            port = self._lan.server_address[1]
            self._lan_info = {"url": f"http://{address}:{port}/?key={self.server.LAN_KEY}", "port": port, "key": self.server.LAN_KEY}
            return json.dumps(self._lan_info)

    def stop_lan(self):
        with self._lifecycle:
            server, self._lan = self._lan, None
            if server:
                server.shutdown()
                server.server_close()
                self._lan_thread.join(3)
                self._lan_thread = None
            if self.server:
                self.server.LAN_KEY = None
            self._lan_info = None

    def _retire(self):
        runtime = self._runtime
        if runtime is None:
            return
        runtime.stop.set()
        self.cancel_work()
        self.local.close()
        with self._responses_lock:
            for response in list(self._responses):
                response.close()
        for lane in self.bridge.lanes.values():
            with lane.cond:
                for exchange in lane.waiting:
                    exchange.cancel()
                lane.waiting.clear()
                lane.workers = 0
                lane.cond.notify_all()
        with runtime.condition:
            if not runtime.condition.wait_for(lambda: runtime.active == 0, timeout=60):
                raise RuntimeError("Requests have not stopped; the library was not switched")
        if runtime.index_thread:
            runtime.index_thread.join(60)
            if runtime.index_thread.is_alive():
                raise RuntimeError("Semantic indexing has not stopped; the library was not switched")
        if self.setup and self.setup.thread:
            self.setup.thread.join(30)
            if self.setup.thread.is_alive():
                raise RuntimeError("The installer has not paused; the library was not switched")
        if self.native is not None:
            self.native.semanticClose()
            self.native.zimClose()

    def open_library(self, path):
        chosen = Path(str(path)).resolve()
        if not (chosen / "library-data" / "catalog.json").is_file():
            raise ValueError("Choose the folder containing library-data/catalog.json")
        if not (chosen / "library-data" / "search.sqlite").is_file():
            raise ValueError("This library is missing search.sqlite. Repair or finish importing the library first.")
        records = json.loads((chosen / "library-data" / "catalog.json").read_text(encoding="utf-8"))
        if not isinstance(records, list):
            raise ValueError("The library catalog is not a record list")
        with self._lifecycle:
            previous = self.data
            self._retire()
            try:
                self._load(chosen)
            except Exception:
                self._load(previous)
                raise
            return self.start_url

    def close(self):
        with self._lifecycle:
            if not self._closed:
                self._retire()
                self._closed = True


def configure(program_dir, data_dir, settings_dir, native=None, callbacks=None):
    """One selected library per Android process, switchable without process death."""
    global _CURRENT
    with _CONFIG_LOCK:
        if _CURRENT is not None:
            _CURRENT.close()
        _CURRENT = Engine(program_dir, data_dir, settings_dir, native, callbacks)
        return _CURRENT
