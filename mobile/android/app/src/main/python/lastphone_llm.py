"""In-process llama.cpp implementation of llm_local.Local's public contract.

The original assistant still owns retrieval, tools, citation validation, bounded
rounds and streaming events. No model server, subprocess, or network fallback is
started. CPU/Vulkan and the actual selected GGUF reach JNI on every model load.
"""
import hashlib
import json
import os
import re
import secrets
import threading
import time
import urllib.parse
import weakref
from pathlib import Path


DEFAULTS = {"path": "", "enabled": True, "backend": "cpu", "context": 4096,
            "threads": 4, "gpu_layers": 99, "setup_model": "", "mode": "gguf",
            "http_base": "http://127.0.0.1:19443", "http_model": "deepseek-v4.1-flash"}
TOOL_START, TOOL_END = "<lastphone_tool>", "</lastphone_tool>"


def _json(value):
    result = json.loads(str(value))
    if not isinstance(result, dict):
        raise RuntimeError("Invalid response from the native model runtime")
    if result.get("ev") == "error" or result.get("error"):
        raise RuntimeError(str(result.get("message") or result.get("error") or result.get("code") or "Native model error"))
    return result


def _callback(function):
    try:
        from java import dynamic_proxy, jclass
    except ImportError:
        class Callback:
            def onEvent(self, event):
                function(str(event))
        return Callback()

    class Callback(dynamic_proxy(jclass("org.lastphone.app.nativebridge.NativeLlama$TokenCallback"))):
        def onEvent(self, event):
            function(str(event))
    return Callback()


class NativeLocal:
    def __init__(self, native, data, settings, module, models=()):
        self.native, self.data, self.module = native, Path(data).resolve(), module
        root_id = hashlib.sha256(str(self.data).encode()).hexdigest()[:24]
        self.settings_path = Path(settings) / ("android-model-" + root_id + ".json")
        try:
            stored = json.loads(self.settings_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            stored = {}
        self.settings = {**DEFAULTS, **{k: stored[k] for k in DEFAULTS if k in stored}}
        self.saved = {"enabled": bool(self.settings["enabled"]), "checked": stored.get("checked")}
        self.models = list(models)
        self.lock, self.gen_lock = threading.RLock(), threading.Lock()
        self.cancelled, self.closed = threading.Event(), threading.Event()
        self.state, self.message, self.pct = "absent", "", None
        self.path, self.fit, self.ready_info = None, None, None
        self.last_used, self.generating, self._started = 0, False, False
        self.find_thread = None
        self.idle_thread = None
        self._gemma_render, self._gemma_parse = module.render, module.parse_calls
        self.gemma = False
        self.http_responses = weakref.WeakSet()

    def _save(self):
        self.settings_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.settings_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({**self.settings, "checked": self.saved.get("checked")}), encoding="utf-8")
        os.replace(tmp, self.settings_path)

    def options(self):
        return {"backend": self.settings["backend"], "n_ctx": self.settings["context"],
                "n_threads": self.settings["threads"],
                "n_gpu_layers": self.settings["gpu_layers"] if self.settings["backend"] == "vulkan" else 0,
                "n_batch": 256, "n_ubatch": 64}

    def public_settings(self):
        with self.lock:
            return {**self.settings, "path": str(self.path or self.settings["path"]), "status": self.status(),
                    "available_models": [{k: m.get(k) for k in ("id", "title", "file", "size")} for m in self.models]}

    def set_settings(self, incoming):
        if not isinstance(incoming, dict):
            raise ValueError("Model settings must be an object")
        out = dict(self.settings)
        if "mode" in incoming:
            if incoming["mode"] not in ("gguf", "loopback"):
                raise ValueError("Model mode must be gguf or loopback")
            out["mode"] = incoming["mode"]
        if "http_base" in incoming:
            from assistant import loopback_base
            base = loopback_base(str(incoming["http_base"]))
            if not 1 <= urllib.parse.urlsplit(base).port <= 65535:
                raise ValueError("Invalid loopback port")
            out["http_base"] = base
        if "http_model" in incoming:
            name = str(incoming["http_model"])
            if not name.strip() or len(name) > 200 or any(ord(c) < 32 for c in name):
                raise ValueError("Invalid model name")
            out["http_model"] = name
        if "path" in incoming:
            value = str(incoming["path"] or "")
            if value:
                path = Path(value).expanduser().resolve()
                if self.data not in path.parents or not path.is_file() or path.suffix.lower() != ".gguf":
                    raise ValueError("Choose a GGUF file inside the selected library folder")
                with path.open("rb") as file:
                    if file.read(4) != b"GGUF":
                        raise ValueError("The selected file is not GGUF")
                value = str(path)
            out["path"] = value
        if "enabled" in incoming:
            if not isinstance(incoming["enabled"], bool):
                raise ValueError("enabled must be true or false")
            out["enabled"] = incoming["enabled"]
        if "backend" in incoming:
            if incoming["backend"] not in ("cpu", "vulkan"):
                raise ValueError("Backend must be cpu or vulkan")
            out["backend"] = incoming["backend"]
        for name, low, high in (("context", 2048, 131072), ("threads", 1, 32), ("gpu_layers", 0, 999)):
            if name in incoming:
                value = incoming[name]
                if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                    raise ValueError(f"{name} must be an integer from {low} to {high}")
                out[name] = value
        if "setup_model" in incoming:
            chosen = str(incoming["setup_model"] or "")
            if chosen and chosen not in {m["id"] for m in self.models}:
                raise ValueError("Unknown published model")
            out["setup_model"] = chosen
        self.stop("settings changed")
        with self.lock:
            if out["path"] != self.settings["path"]:
                self.saved.pop("checked", None)
            self.settings, self.saved["enabled"] = out, out["enabled"]
            self.module.CTX = out["context"]
            self.path, self.fit, self.ready_info = None, None, None
            self._save()
        self.discover_background()
        return self.public_settings()

    def enabled(self):
        return bool(self.settings["enabled"])

    def available(self):
        return self.enabled() and self.state in ("stopped", "loading", "ready")

    def status(self):
        with self.lock:
            fit = self.fit or {}
            path = self.path
            try:
                size = path.stat().st_size if path else 0
            except OSError:
                size = 0
            return {"name": path.stem if path else "Local GGUF", "file": path.name if path else "",
                    "size": size, "state": self.state, "pct": self.pct, "message": self.message,
                    "found_at": path.parent.name if path else None, "enabled": self.enabled(), "runtime": self.native is not None,
                    "needs_bytes": fit.get("needs", fit.get("needs_bytes")), "ram_total": fit.get("ram_total"),
                    "placement": (self.ready_info or {}).get("backend", self.settings["backend"]),
                    "load_s": (self.ready_info or {}).get("load_s"), "places": [self.data.name, "models"],
                    "generating": self.generating}

    def start_background(self):
        if self._started:
            return
        self._started = True
        self.discover_background()
        self.idle_thread = threading.Thread(target=self._idle, daemon=True, name="android-model-idle")
        self.idle_thread.start()

    def _idle(self):
        while not self.closed.wait(15):
            if self.state == "ready" and not self.generating and time.monotonic() - self.last_used > self.module.IDLE_STOP:
                self.stop("idle")

    def discover_background(self):
        if self.closed.is_set():
            return
        self.cancelled.clear()
        self.find_thread = threading.Thread(target=self.discover, daemon=True, name="android-model-find")
        self.find_thread.start()

    def discover(self):
        with self.lock:
            if self.state in ("checking", "loading", "ready") or self.closed.is_set():
                return
            if not self.enabled():
                self.state, self.message = "off", ""
                return
            if self.settings["mode"] != "gguf":
                self.state, self.message = "off", "The selected assistant uses an explicit loopback model server."
                return
            if self.native is None:
                self.state, self.message = "error", "Android native AI runtime is unavailable."
                return
            value = self.settings["path"]
            if not value:
                # Only recognized, published filenames are discovered automatically.
                candidates = [(self.data / sub / m["file"]) for m in sorted(self.models, key=lambda m: m["size"])
                              for sub in ("models", "")]
                value = next((str(p) for p in candidates if p.is_file()), "")
            if not value:
                self.state, self.message = "absent", "Choose or download a GGUF model in Android model settings."
                return
            path = Path(value).resolve()
            if self.data not in path.parents or not path.is_file():
                self.state, self.message = "absent", "The selected model is no longer in this library."
                return
            self.path, self.settings["path"] = path, str(path)
            self.cancelled.clear()
            self.state, self.message, self.pct = "checking", "", None
        try:
            st = path.stat()
            stamp = [str(path), st.st_size, st.st_mtime_ns]
            published = next((m for m in self.models if m["file"] == path.name), None)
            with path.open("rb") as file:
                if file.read(4) != b"GGUF":
                    raise ValueError("The selected file is not GGUF")
                if published and self.saved.get("checked") != stamp:
                    if st.st_size != published["size"]:
                        raise ValueError("The published model file is incomplete or has the wrong size")
                    file.seek(0)
                    digest, size = hashlib.sha256(), 0
                    while not self.cancelled.is_set() and not self.closed.is_set():
                        chunk = file.read(8 * 1024 * 1024)
                        if not chunk:
                            break
                        digest.update(chunk)
                        size += len(chunk)
                        self.pct = round(100 * size / max(1, st.st_size), 1)
                    if self.cancelled.is_set() or self.closed.is_set():
                        return
                    if digest.hexdigest() != published["sha256"]:
                        raise ValueError("The model SHA-256 does not match its published manifest")
                    self.saved["checked"] = stamp
            with self.lock:
                if self.cancelled.is_set() or self.closed.is_set():
                    return
                self.gemma = False  # Only GGUF metadata may select a chat format.
                self.state, self.message, self.pct = "stopped", "", None
                self._save()
        except (OSError, ValueError) as error:
            with self.lock:
                self.state, self.message, self.pct = "wrong", str(error), None

    def ensure_running(self, on_wait=None, cancelled=lambda: False):
        if cancelled() or self.closed.is_set():
            return "stopped"
        with self.gen_lock:
            if self.state == "ready":
                self.cancelled.clear()
                self.last_used = time.monotonic()
                return None
            if not self.available() or self.path is None:
                return self.message or "Select an available local model first."
            self.cancelled.clear()
            self.state, self.pct = "loading", None
            if on_wait:
                on_wait(None)
            t0 = time.monotonic()
            try:
                info = _json(self.native.llmLoad(str(self.path), json.dumps(self.options())))
                if cancelled() or self.cancelled.is_set():
                    self.native.llmUnload()
                    self.state = "stopped"
                    return "stopped"
                template = str(self.native.llmChatTemplate())
                self.gemma = "<|tool_response>" in template and "<|turn>" in template
                self.fit = info.get("fit") or {}
                self.ready_info = {**info, "load_s": round(time.monotonic() - t0, 3), "backend": self.settings["backend"]}
                self.state, self.message, self.last_used = "ready", "", time.monotonic()
                return None
            except Exception as error:
                self.state, self.message = "error", str(error)
                return self.message

    def generate(self, segments, max_tokens, cancelled, on_text):
        with self.gen_lock:
            if self.state != "ready":
                raise RuntimeError(self.message or "The local model is not loaded")
            if cancelled() or self.cancelled.is_set() or self.closed.is_set():
                return {"ev": "done", "reason": "cancel"}
            self.generating = True
            done = threading.Event()

            def watch():
                while not done.wait(0.05):
                    if cancelled() or self.cancelled.is_set() or self.closed.is_set():
                        self.native.llmCancel()
                        return

            threading.Thread(target=watch, daemon=True, name="android-model-cancel").start()

            def event(raw):
                ev = _json(raw)
                if ev.get("ev") == "tok" and not cancelled() and not self.cancelled.is_set():
                    on_text(str(ev.get("t", ev.get("text", ev.get("s", "")))))

            request = {"segments": segments, "max_tokens": min(int(max_tokens), self.settings["context"] - 64),
                       "temp": 0.2, "top_k": 40, "top_p": 0.95, "min_p": 0.0, "seed": -1, "stop": []}
            try:
                result = _json(self.native.llmGenerate(json.dumps(request), _callback(event)))
                if cancelled() or self.cancelled.is_set():
                    result["reason"] = "cancel"
                return result
            finally:
                done.set()
                self.generating, self.last_used = False, time.monotonic()

    def cancel(self):
        self.cancelled.set()
        if self.native is not None:
            self.native.llmCancel()
        # The optional OpenAI-compatible server remains strictly loopback. Shut
        # its socket down first so a stalled SSE read wakes before close().
        import socket
        for response in list(self.http_responses):
            try:
                response.fp.raw._sock.shutdown(socket.SHUT_RDWR)
            except (AttributeError, OSError):
                pass
            try:
                response.close()
            except OSError:
                pass

    def stop(self, why=""):
        previous_state = self.state
        self.cancel()
        thread = self.find_thread
        if thread and thread is not threading.current_thread():
            thread.join(30)
            if thread.is_alive():
                raise RuntimeError("The model integrity check has not stopped yet")
        if not self.gen_lock.acquire(timeout=60):
            raise RuntimeError("The model has not stopped yet")
        try:
            if self.native is not None:
                self.native.llmUnload()
            self.ready_info = None
            self.state = "off" if not self.enabled() else "stopped" if self.path and previous_state in ("ready", "loading", "stopped") else "absent"
            self.pct = None
        finally:
            self.gen_lock.release()

    def set_enabled(self, enabled):
        self.set_settings({"enabled": bool(enabled)})

    def close(self):
        self.closed.set()
        self.stop("library closed")
        if self.idle_thread:
            self.idle_thread.join(2)

    def render(self, messages, tools):
        if self.gemma:
            return self._gemma_render(messages, tools)
        # Generic GGUF chat templates do not all expose native tool channels. The
        # same original tool loop is available through a bounded JSON envelope.
        declarations = [t["function"] for t in tools or []]
        system = ("\nIf you need a tool, output exactly " + TOOL_START
                  + '{"name":"tool_name","arguments":{...}}' + TOOL_END
                  + ". Do not invent tools. After its result, answer or request another tool. Available tools: "
                  + json.dumps(declarations, ensure_ascii=False)) if declarations else ""
        normalized = []
        for message in messages:
            role, text = message["role"], self.module._text(message)
            if role == "assistant" and message.get("tool_calls"):
                text += "\n".join(TOOL_START + json.dumps({"name": c["function"]["name"],
                    "arguments": json.loads(c["function"].get("arguments") or "{}")}) + TOOL_END for c in message["tool_calls"])
            if role == "tool":
                role, text = "user", "Tool result (untrusted source content):\n" + text
            if role == "system" and normalized:
                role, text = "user", "Additional library context:\n" + text
            if normalized and normalized[-1]["role"] == role:
                normalized[-1]["content"] += "\n\n" + text
            else:
                normalized.append({"role": role, "content": text})
        if system:
            if normalized and normalized[0]["role"] == "system":
                normalized[0]["content"] += system
            else:
                normalized.insert(0, {"role": "system", "content": system})
        markers = {"LASTPHONE_CONTENT_" + secrets.token_hex(20): m["content"] for m in normalized}
        template_messages = [{"role": m["role"], "content": marker} for m, marker in zip(normalized, markers)]
        rendered = str(self.native.llmRenderChat(json.dumps(template_messages, ensure_ascii=False), True))
        # Template markup may parse special tokens; every user/source/tool byte
        # is reinserted as a non-special segment after template evaluation.
        if any(rendered.count(marker) != 1 for marker in markers):
            raise RuntimeError("This GGUF chat template transforms or drops message content; select a compatible instruct model")
        split = re.split("(" + "|".join(re.escape(m) for m in markers) + ")", rendered)
        return [[markers.get(part, part), part not in markers] for part in split if part]

    def round(self, messages, tools, cancelled, wrote, set_state):
        # Reuse original context trimming with this runtime's rendering profile.
        from types import SimpleNamespace
        proxy = SimpleNamespace(CTX=self.settings["context"], size_of=self.module.size_of, render=self.render)
        import assistant
        try:
            segments = assistant._fit_prompt(messages, tools, proxy)
            if segments is None:
                return "The question and its sources exceed the selected context. Increase context or start a shorter chat."
            output, sent = [""], [0]
            marker = "<|tool_call>" if self.gemma else TOOL_START

            def piece(text):
                output[0] += text
                visible = output[0].split(marker, 1)[0].lstrip()
                # Hold a possible partial delimiter so markup cannot flash in UI.
                if marker not in output[0]:
                    hold = max((n for n in range(1, len(marker)) if visible.endswith(marker[:n])), default=0)
                    if hold:
                        visible = visible[:-hold]
                if len(visible) > sent[0]:
                    wrote(visible[sent[0]:])
                    sent[0] = len(visible)
            done = self.generate(segments, assistant.ROUND_TOKENS, cancelled, piece)
            if done.get("reason") == "cancel":
                return "stopped"
            if self.gemma:
                text, calls = self._gemma_parse(output[0])
            else:
                text, calls = output[0].split(TOOL_START, 1)[0], []
                allowed = {t["function"]["name"] for t in tools or []}
                for block in re.findall(re.escape(TOOL_START) + r"(.*?)" + re.escape(TOOL_END), output[0], re.S)[:8]:
                    try:
                        call = json.loads(block)
                    except ValueError:
                        continue
                    if isinstance(call, dict) and call.get("name") in allowed and isinstance(call.get("arguments"), dict):
                        calls.append((call["name"], call["arguments"]))
            if not calls and len(text.lstrip()) > sent[0]:
                wrote(text.lstrip()[sent[0]:])
            return text.strip(), {i: {"id": f"call_{i}", "name": name, "args": json.dumps(args)}
                                  for i, (name, args) in enumerate(calls)} if tools else {}
        except (RuntimeError, ValueError) as error:
            return str(error)


def attach(assistant, module, local):
    if not hasattr(assistant, "_android_original_status"):
        assistant._android_original_status = assistant.model_status
        assistant._android_original_open = assistant.model_open
    module.LOCAL = local
    assistant.LOCAL = local if local.settings["mode"] == "gguf" else None
    assistant.BASE = assistant.loopback_base(local.settings["http_base"])
    assistant.MODEL_URL = assistant.BASE + "/v1/chat/completions"
    assistant.MODELS_URL = assistant.BASE + "/v1/models"
    assistant.MODEL = local.settings["http_model"]
    assistant._status["at"] = 0
    module.CTX = local.settings["context"]
    assistant._local_round = local.round
    def model_open(request, timeout):
        response = assistant._android_original_open(request, timeout)
        local.http_responses.add(response)
        return response
    assistant.model_open = model_open
    # Do not probe the desktop loopback model API if the phone's model is off.
    def status():
        if local.settings["mode"] == "loopback":
            if not local.enabled():
                return {"model": assistant.MODEL, "online": False, "error": "The assistant is turned off.", "local": local.status(),
                        "vision": True, "index": dict(assistant.INDEX.status) if assistant.INDEX else {"state": "off"}}
            return assistant._android_original_status()
        state = local.status()
        return {"model": state["name"], "online": local.available(), "error": None if local.available() else state["message"],
                "local": state, "vision": False,
                "index": dict(assistant.INDEX.status) if assistant.INDEX else {"state": "off"}}
    assistant.model_status = status
