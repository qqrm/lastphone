// Android in-process adapter for Lastbook's MIT-licensed local runner.
// The prompt-segment protocol, prefix reuse and fitting follow
// packaging/llm/lastbook_llm.cpp and mobile/ipad/llm/lastbook_llm_bridge.cpp.
// Copyright (c) 2026 Lastbook contributors; Lastphone contributors.

#include <jni.h>
#include <android/log.h>
#include "llama.h"
#include "ggml-backend.h"
#include "fit.h"
#include "log.h"
#include <nlohmann/json.hpp>
#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstdio>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>
#include <sys/sysinfo.h>

using json = nlohmann::ordered_json;
using Clock = std::chrono::steady_clock;
bool tty_can_use_colors() { return false; }
bool tty_enable_ansi() { return false; }

namespace {
std::once_flag initialized;
std::mutex engine_mutex;
std::atomic<uint64_t> cancel_epoch{0};

// JNI's Modified UTF-8 is not UTF-8 (notably for emoji and NUL). Use the Java
// UTF-8 codec at the boundary instead of GetStringUTFChars/NewStringUTF.
std::string utf8(JNIEnv * env, jstring value) {
    if (!value) throw std::invalid_argument("Missing string argument.");
    jclass cls = env->FindClass("java/lang/String");
    jmethodID method = env->GetMethodID(cls, "getBytes", "(Ljava/lang/String;)[B");
    jstring charset = env->NewStringUTF("UTF-8");
    auto bytes = static_cast<jbyteArray>(env->CallObjectMethod(value, method, charset));
    env->DeleteLocalRef(charset);
    env->DeleteLocalRef(cls);
    if (!bytes || env->ExceptionCheck()) throw std::runtime_error("Could not encode Java string.");
    const jsize n = env->GetArrayLength(bytes);
    std::string out(static_cast<size_t>(n), '\0');
    env->GetByteArrayRegion(bytes, 0, n, reinterpret_cast<jbyte *>(out.data()));
    env->DeleteLocalRef(bytes);
    return out;
}

jstring java_string(JNIEnv * env, const std::string & value) {
    jclass cls = env->FindClass("java/lang/String");
    jmethodID ctor = env->GetMethodID(cls, "<init>", "([BLjava/lang/String;)V");
    jbyteArray bytes = env->NewByteArray(static_cast<jsize>(value.size()));
    if (!bytes) { env->DeleteLocalRef(cls); return nullptr; }
    env->SetByteArrayRegion(bytes, 0, static_cast<jsize>(value.size()), reinterpret_cast<const jbyte *>(value.data()));
    jstring charset = env->NewStringUTF("UTF-8");
    auto result = static_cast<jstring>(env->NewObject(cls, ctor, bytes, charset));
    env->DeleteLocalRef(bytes);
    env->DeleteLocalRef(charset);
    env->DeleteLocalRef(cls);
    return result;
}

jstring result(JNIEnv * env, const json & value) {
    return java_string(env, value.dump(-1, ' ', false, json::error_handler_t::replace));
}

json failure(const std::string & code, const std::string & message) {
    return {{"ev", "error"}, {"code", code}, {"msg", message}};
}

void initialize() {
    std::call_once(initialized, [] {
        llama_log_set([](ggml_log_level level, const char * text, void *) {
            if (level >= GGML_LOG_LEVEL_WARN) __android_log_write(ANDROID_LOG_WARN, "LastphoneLlama", text);
        }, nullptr);
        common_log_set_verbosity_thold(LOG_LEVEL_WARN);
        ggml_backend_load_all();
        llama_backend_init();
    });
}

uint64_t total_ram() {
    struct sysinfo info{};
    return sysinfo(&info) == 0 ? uint64_t(info.totalram) * info.mem_unit : 0;
}

uint64_t available_ram() {
    std::unique_ptr<FILE, decltype(&fclose)> input(fopen("/proc/meminfo", "r"), fclose);
    char line[256];
    unsigned long long kb = 0;
    while (input && fgets(line, sizeof(line), input.get())) {
        if (sscanf(line, "MemAvailable: %llu kB", &kb) == 1) return kb * 1024;
    }
    return 0;
}

bool gpu_device(ggml_backend_dev_t device) {
    const auto type = ggml_backend_dev_type(device);
    return type == GGML_BACKEND_DEVICE_TYPE_GPU || type == GGML_BACKEND_DEVICE_TYPE_IGPU;
}

json capabilities() {
    initialize();
    json devices = json::array();
    bool vulkan = false;
    for (size_t i = 0; i < ggml_backend_dev_count(); ++i) {
        auto device = ggml_backend_dev_get(i);
        const std::string name = ggml_backend_dev_name(device);
        size_t free = 0, total = 0;
        ggml_backend_dev_memory(device, &free, &total);
        devices.push_back({{"name", name}, {"description", ggml_backend_dev_description(device)},
                           {"gpu", gpu_device(device)}, {"free", free}, {"total", total}});
        vulkan = vulkan || (gpu_device(device) && name.rfind("Vulkan", 0) == 0);
    }
    return {{"llama_commit", LASTPHONE_LLAMA_COMMIT}, {"cpu", true}, {"vulkan", vulkan},
            {"devices", devices}, {"ram_total", total_ram()}, {"ram_free", available_ram()}};
}

struct Params {
    llama_model_params model = llama_model_default_params();
    llama_context_params context = llama_context_default_params();
    std::vector<ggml_backend_dev_t> devices;
    std::vector<float> split;
    std::vector<llama_model_tensor_buft_override> overrides;
    std::string backend;
    bool unified_memory = true;
    uint64_t reserve = 768ULL * 1024 * 1024;

    explicit Params(const json & options) {
        backend = options.value("backend", "cpu");
        context.n_ctx = options.value("n_ctx", 4096);
        if (context.n_ctx < 128 || context.n_ctx > 131072) throw std::invalid_argument("n_ctx must be 128–131072.");
        context.n_batch = std::min<uint32_t>(context.n_ctx, options.value("n_batch", 512));
        context.n_ubatch = std::min<uint32_t>(context.n_batch, options.value("n_ubatch", 128));
        if (!context.n_ubatch) throw std::invalid_argument("Batch sizes must be positive.");
        context.n_seq_max = 1;
        const int threads = options.value("n_threads", std::min(8, std::max(2, int(std::thread::hardware_concurrency()) / 2)));
        if (threads < 1 || threads > 64) throw std::invalid_argument("n_threads must be 1–64.");
        context.n_threads = context.n_threads_batch = threads;
        context.no_perf = true;
        context.swa_full = false;
        model.load_mode = LLAMA_LOAD_MODE_MMAP;
        if (backend == "cpu") {
            model.n_gpu_layers = 0;
            context.offload_kqv = false;
            context.op_offload = false;
        } else if (backend == "vulkan") {
            for (size_t i = 0; i < ggml_backend_dev_count(); ++i) {
                auto device = ggml_backend_dev_get(i);
                if (gpu_device(device) && std::string(ggml_backend_dev_name(device)).rfind("Vulkan", 0) == 0) {
                    devices.push_back(device);
                    unified_memory = ggml_backend_dev_type(device) == GGML_BACKEND_DEVICE_TYPE_IGPU;
                    break;
                }
            }
            if (devices.empty()) throw std::invalid_argument("Vulkan inference is unavailable on this device; choose CPU.");
            model.n_gpu_layers = options.value("n_gpu_layers", -1);
        } else {
            throw std::invalid_argument("backend must be cpu or vulkan.");
        }
        devices.push_back(nullptr);
        model.devices = devices.data();
        split.assign(llama_max_devices(), 0.0f);
        overrides.assign(llama_max_tensor_buft_overrides(), {});
    }
};

json fit(const std::string & path, Params & params) {
    const auto start = Clock::now();
    std::vector<size_t> margins(llama_max_devices(), static_cast<size_t>(params.reserve));
    auto state = COMMON_PARAMS_FIT_STATUS_SUCCESS;
    if (params.backend != "cpu") {
        state = common_fit_params(path.c_str(), &params.model, &params.context, params.split.data(),
            params.overrides.data(), margins.data(), params.context.n_ctx, nullptr, GGML_LOG_LEVEL_ERROR);
    }
    if (state == COMMON_PARAMS_FIT_STATUS_ERROR) return failure("model_file", "llama.cpp could not read the model.");
    uint32_t train = 0, context = 0;
    uint64_t host = 0, device = 0;
    json rows = json::array();
    for (const auto & row : common_fit_measure(path.c_str(), &params.model, &params.context, &train, &context, GGML_LOG_LEVEL_ERROR)) {
        if (row.kind == common_memory_breakdown_row::BUFFER_TYPE) continue;
        const bool on_host = row.kind == common_memory_breakdown_row::HOST;
        const uint64_t bytes = row.model + row.context + row.compute;
        if (on_host) host += bytes; else device += bytes;
        rows.push_back({{"kind", on_host ? "host" : "device"}, {"name", row.name}, {"desc", row.description},
                        {"model", row.model}, {"context", row.context}, {"compute", row.compute},
                        {"total", row.total}, {"free", row.free}});
    }
    const uint64_t available = available_ram();
    // Mobile integrated GPUs share physical RAM with the application. A Vulkan
    // driver without VK_EXT_memory_budget reports the whole heap as free, so
    // its own fitter alone cannot protect Android's remaining system memory.
    const uint64_t required_ram = host + (params.unified_memory ? device : 0) + params.reserve;
    if (available && required_ram > available) {
        state = COMMON_PARAMS_FIT_STATUS_FAILURE;
    }
    return {{"ev", "fit"}, {"status", state == COMMON_PARAMS_FIT_STATUS_SUCCESS ? "fits" : "does_not_fit"},
            {"backend", params.backend}, {"cpu_only", params.backend == "cpu"}, {"n_gpu_layers", params.model.n_gpu_layers},
            {"n_ctx", params.context.n_ctx}, {"memory", rows}, {"host_bytes", host}, {"device_bytes", device},
            {"ram_required", required_ram}, {"reserve_bytes", params.reserve}, {"unified_memory", params.unified_memory},
            {"ram_total", total_ram()}, {"ram_free", available_ram()},
            {"fit_ms", std::chrono::duration_cast<std::chrono::milliseconds>(Clock::now() - start).count()}};
}

struct Engine {
    llama_model * model = nullptr;
    llama_context * context = nullptr;
    const llama_vocab * vocab = nullptr;
    std::vector<llama_token> cached;
    uint64_t epoch = 0;
    std::string backend;
    ~Engine() { if (context) llama_free(context); if (model) llama_model_free(model); }
    bool cancelled() const { return cancel_epoch.load(std::memory_order_relaxed) != epoch; }
};
std::unique_ptr<Engine> engine;

std::vector<llama_token> tokenize(const llama_vocab * vocab, const std::string & text, bool special) {
    std::vector<llama_token> out(text.size() + 8);
    int count = llama_tokenize(vocab, text.data(), static_cast<int32_t>(text.size()), out.data(), static_cast<int32_t>(out.size()), false, special);
    if (count < 0) {
        out.resize(-count);
        count = llama_tokenize(vocab, text.data(), static_cast<int32_t>(text.size()), out.data(), static_cast<int32_t>(out.size()), false, special);
    }
    if (count < 0) throw std::runtime_error("Tokenization failed.");
    out.resize(count);
    return out;
}

std::string piece(const llama_vocab * vocab, llama_token token) {
    std::string out(256, '\0');
    int count = llama_token_to_piece(vocab, token, out.data(), static_cast<int32_t>(out.size()), 0, true);
    if (count < 0) {
        out.resize(-count);
        count = llama_token_to_piece(vocab, token, out.data(), static_cast<int32_t>(out.size()), 0, true);
    }
    if (count < 0) throw std::runtime_error("Token decoding failed.");
    out.resize(count);
    return out;
}

size_t complete_utf8(const std::string & text) {
    const size_t size = text.size();
    for (size_t back = 1; back <= 4 && back <= size; ++back) {
        const auto c = static_cast<unsigned char>(text[size - back]);
        if ((c & 0xc0) == 0x80) continue;
        const size_t need = c < 0x80 ? 1 : (c >> 5) == 6 ? 2 : (c >> 4) == 14 ? 3 : (c >> 3) == 30 ? 4 : 1;
        return back >= need ? size : size - back;
    }
    return size;
}

bool emit(JNIEnv * env, jobject callback, jmethodID method, const json & event) {
    jstring value = result(env, event);
    if (!value) return false;
    env->CallVoidMethod(callback, method, value);
    env->DeleteLocalRef(value);
    return !env->ExceptionCheck();
}

json generate(JNIEnv * env, jobject callback, const json & request, uint64_t epoch) {
    if (!engine) return failure("not_loaded", "The local model is not loaded.");
    Engine & e = *engine;
    e.epoch = epoch;
    const json id = request.value("id", json());
    if (e.cancelled()) return {{"ev", "done"}, {"id", id}, {"reason", "cancel"}, {"n_gen", 0}};
    jclass callback_class = env->GetObjectClass(callback);
    jmethodID on_event = env->GetMethodID(callback_class, "onEvent", "(Ljava/lang/String;)V");
    env->DeleteLocalRef(callback_class);
    if (!on_event) throw std::invalid_argument("Callback must implement onEvent(String).");
    std::vector<llama_token> tokens;
    const auto start = Clock::now();
    for (const auto & segment : request.at("segments")) {
        if (!segment.is_array() || segment.size() != 2 || !segment[0].is_string() || !segment[1].is_boolean()) {
            return failure("bad_request", "segments must contain [text, special] pairs.");
        }
        auto part = tokenize(e.vocab, segment[0].get<std::string>(), segment[1].get<bool>());
        tokens.insert(tokens.end(), part.begin(), part.end());
    }
    // Lastbook's Gemma renderer omits BOS, while some model-owned templates
    // include it. Add the vocabulary-required token exactly once in either case.
    if (llama_vocab_get_add_bos(e.vocab)) {
        const llama_token bos = llama_vocab_bos(e.vocab);
        if (tokens.empty() || tokens.front() != bos) tokens.insert(tokens.begin(), bos);
    }
    if (tokens.empty()) return failure("bad_request", "The prompt is empty.");
    const int context_size = static_cast<int>(llama_n_ctx(e.context));
    if (tokens.size() + 16 >= static_cast<size_t>(context_size)) {
        return failure("too_long", "The prompt and sources exceed the configured context.");
    }
    const int max_tokens = std::min(std::max(1, request.value("max_tokens", 1800)), context_size - static_cast<int>(tokens.size()));
    size_t keep = 0;
    while (keep < e.cached.size() && keep < tokens.size() && e.cached[keep] == tokens[keep]) ++keep;
    if (keep == tokens.size()) --keep;
    llama_memory_t memory = llama_get_memory(e.context);
    if (keep < e.cached.size()) {
        if (!llama_memory_seq_rm(memory, 0, static_cast<llama_pos>(keep), -1)) {
            llama_memory_clear(memory, true);
            keep = 0;
        }
        e.cached.resize(keep);
    }
    auto clear = [&] { llama_memory_clear(memory, true); e.cached.clear(); };
    for (size_t offset = keep; offset < tokens.size(); offset += llama_n_batch(e.context)) {
        if (e.cancelled()) { clear(); return {{"ev", "done"}, {"id", id}, {"reason", "cancel"}, {"n_gen", 0}}; }
        const int count = static_cast<int>(std::min<size_t>(llama_n_batch(e.context), tokens.size() - offset));
        const int status = llama_decode(e.context, llama_batch_get_one(tokens.data() + offset, count));
        if (status != 0) {
            clear();
            if (e.cancelled() || status == 2) return {{"ev", "done"}, {"id", id}, {"reason", "cancel"}, {"n_gen", 0}};
            return failure("decode", "The model could not process the prompt.");
        }
        e.cached.insert(e.cached.end(), tokens.begin() + offset, tokens.begin() + offset + count);
    }
    const auto prompt_done = Clock::now();
    std::unique_ptr<llama_sampler, decltype(&llama_sampler_free)> sampler(llama_sampler_chain_init(llama_sampler_chain_default_params()), llama_sampler_free);
    llama_sampler_chain_add(sampler.get(), llama_sampler_init_top_k(request.value("top_k", 64)));
    llama_sampler_chain_add(sampler.get(), llama_sampler_init_top_p(request.value("top_p", 0.95f), 1));
    llama_sampler_chain_add(sampler.get(), llama_sampler_init_min_p(request.value("min_p", 0.05f), 1));
    llama_sampler_chain_add(sampler.get(), llama_sampler_init_temp(request.value("temp", 0.2f)));
    llama_sampler_chain_add(sampler.get(), llama_sampler_init_dist(request.value("seed", uint32_t(1))));
    std::vector<std::string> stops;
    for (const auto & stop : request.value("stop", json::array())) {
        if (stop.is_string() && !stop.get<std::string>().empty()) stops.push_back(stop.get<std::string>());
    }
    std::string pending, reason = "length", stopped_by;
    int generated = 0;
    auto output = [&](const std::string & text) {
        return text.empty() || emit(env, callback, on_event, {{"ev", "tok"}, {"id", id}, {"t", text}});
    };
    for (; generated < max_tokens;) {
        if (e.cancelled()) { reason = "cancel"; break; }
        const llama_token token = llama_sampler_sample(sampler.get(), e.context, -1);
        if (llama_vocab_is_eog(e.vocab, token)) { reason = "eog"; break; }
        ++generated;
        pending += piece(e.vocab, token);
        size_t stop_at = std::string::npos;
        for (const auto & stop : stops) {
            const size_t position = pending.find(stop);
            if (position < stop_at) { stop_at = position; stopped_by = stop; }
        }
        if (stop_at != std::string::npos) {
            if (!output(pending.substr(0, stop_at))) { clear(); return failure("callback", "Generation callback failed."); }
            pending.clear(); reason = "stop"; break;
        }
        size_t hold = 0;
        for (const auto & stop : stops) {
            const size_t maximum = std::min(pending.size(), stop.size() - 1);
            for (size_t n = 1; n <= maximum; ++n) {
                if (pending.compare(pending.size() - n, n, stop, 0, n) == 0) hold = std::max(hold, n);
            }
        }
        const size_t safe = complete_utf8(pending.substr(0, pending.size() - hold));
        if (safe) {
            if (!output(pending.substr(0, safe))) { clear(); return failure("callback", "Generation callback failed."); }
            pending.erase(0, safe);
        }
        if (e.cancelled()) { reason = "cancel"; break; }
        llama_token one = token;
        const int status = llama_decode(e.context, llama_batch_get_one(&one, 1));
        if (status != 0) {
            if (e.cancelled() || status == 2) { reason = "cancel"; clear(); break; }
            clear(); return failure("decode", "The model stopped while generating.");
        }
        e.cached.push_back(token);
    }
    if (!pending.empty() && reason != "cancel") {
        if (!output(pending)) { clear(); return failure("callback", "Generation callback failed."); }
    }
    const auto end = Clock::now();
    json done = {{"ev", "done"}, {"id", id}, {"reason", reason}, {"n_prompt", tokens.size()},
                 {"n_reused", keep}, {"n_gen", generated}, {"backend", e.backend},
                 {"prompt_ms", std::chrono::duration_cast<std::chrono::milliseconds>(prompt_done - start).count()},
                 {"gen_ms", std::chrono::duration_cast<std::chrono::milliseconds>(end - prompt_done).count()}};
    if (!stopped_by.empty()) done["stop"] = stopped_by;
    return done;
}
} // namespace

extern "C" JNIEXPORT jstring JNICALL Java_org_lastphone_app_nativebridge_NativeLlama_capabilities(JNIEnv * env, jclass) {
    try { return result(env, capabilities()); } catch (const std::exception & error) { return result(env, failure("runtime", error.what())); }
}

extern "C" JNIEXPORT jstring JNICALL Java_org_lastphone_app_nativebridge_NativeLlama_measure(JNIEnv * env, jclass, jstring path, jstring options) {
    try {
        initialize();
        std::lock_guard<std::mutex> lock(engine_mutex);
        Params params(json::parse(utf8(env, options)));
        return result(env, fit(utf8(env, path), params));
    } catch (const std::exception & error) { return result(env, failure("model_file", error.what())); }
}

extern "C" JNIEXPORT jstring JNICALL Java_org_lastphone_app_nativebridge_NativeLlama_load(JNIEnv * env, jclass, jstring path_value, jstring options) {
    const uint64_t epoch = cancel_epoch.load();
    try {
        initialize();
        std::lock_guard<std::mutex> lock(engine_mutex);
        const std::string path = utf8(env, path_value);
        Params params(json::parse(utf8(env, options)));
        if (cancel_epoch.load() != epoch) return result(env, failure("cancel", "Model loading was cancelled."));
        engine.reset(); // release the old model before reserving memory for its replacement
        json measured = fit(path, params);
        if (measured.value("ev", "") == "error") return result(env, measured);
        if (measured.value("status", "") != "fits") {
            json error = failure("too_big", "The configured model and context do not fit available memory.");
            error["fit"] = measured;
            return result(env, error);
        }
        auto next = std::make_unique<Engine>();
        next->epoch = epoch;
        next->backend = params.backend;
        params.model.progress_callback = [](float, void * state) { return !static_cast<Engine *>(state)->cancelled(); };
        params.model.progress_callback_user_data = next.get();
        params.context.abort_callback = [](void * state) { return static_cast<Engine *>(state)->cancelled(); };
        params.context.abort_callback_data = next.get();
        const auto start = Clock::now();
        next->model = llama_model_load_from_file(path.c_str(), params.model);
        if (!next->model) return result(env, failure(next->cancelled() ? "cancel" : "load", "The model could not be loaded."));
        next->context = llama_init_from_model(next->model, params.context);
        if (!next->context) return result(env, failure("context", "There is not enough memory for the configured context."));
        if (next->cancelled()) return result(env, failure("cancel", "Model loading was cancelled."));
        next->vocab = llama_model_get_vocab(next->model);
        char description[256]{};
        llama_model_desc(next->model, description, sizeof(description));
        json ready = {{"ev", "ready"}, {"desc", description}, {"n_ctx", llama_n_ctx(next->context)},
                      {"threads", llama_n_threads(next->context)}, {"backend", params.backend},
                      {"n_gpu_layers", params.model.n_gpu_layers}, {"fit", measured},
                      {"chat_template_available", llama_model_chat_template(next->model, nullptr) != nullptr},
                      {"load_ms", std::chrono::duration_cast<std::chrono::milliseconds>(Clock::now() - start).count()},
                      {"ram_free", available_ram()}};
        engine = std::move(next);
        return result(env, ready);
    } catch (const std::exception & error) { return result(env, failure("load", error.what())); }
}

extern "C" JNIEXPORT jstring JNICALL Java_org_lastphone_app_nativebridge_NativeLlama_generate(JNIEnv * env, jclass, jstring request, jobject callback) {
    const uint64_t epoch = cancel_epoch.load();
    try {
        std::lock_guard<std::mutex> lock(engine_mutex);
        if (!callback) throw std::invalid_argument("Generation callback is required.");
        const json generated = generate(env, callback, json::parse(utf8(env, request)), epoch);
        if (env->ExceptionCheck()) return nullptr;
        return result(env, generated);
    } catch (const std::exception & error) {
        if (env->ExceptionCheck()) return nullptr;
        return result(env, failure("generation", error.what()));
    }
}

extern "C" JNIEXPORT jstring JNICALL Java_org_lastphone_app_nativebridge_NativeLlama_chatTemplate(JNIEnv * env, jclass) {
    std::lock_guard<std::mutex> lock(engine_mutex);
    if (!engine) return java_string(env, "");
    const char * value = llama_model_chat_template(engine->model, nullptr);
    return java_string(env, value ? value : "");
}

extern "C" JNIEXPORT jstring JNICALL Java_org_lastphone_app_nativebridge_NativeLlama_renderChat(JNIEnv * env, jclass, jstring messages_value, jboolean add_assistant) {
    try {
        std::lock_guard<std::mutex> lock(engine_mutex);
        if (!engine) throw std::runtime_error("The local model is not loaded.");
        const char * format = llama_model_chat_template(engine->model, nullptr);
        if (!format) throw std::runtime_error("This GGUF has no chat template; select a compatible instruct model.");
        const auto input = json::parse(utf8(env, messages_value));
        std::vector<std::string> roles, contents;
        for (const auto & message : input) { roles.push_back(message.at("role")); contents.push_back(message.at("content")); }
        std::vector<llama_chat_message> messages;
        for (size_t i = 0; i < roles.size(); ++i) messages.push_back({roles[i].c_str(), contents[i].c_str()});
        int size = llama_chat_apply_template(format, messages.data(), messages.size(), add_assistant, nullptr, 0);
        if (size < 0) throw std::runtime_error("The GGUF chat template needs the Python Jinja renderer.");
        std::string output(static_cast<size_t>(size) + 1, '\0');
        size = llama_chat_apply_template(format, messages.data(), messages.size(), add_assistant, output.data(), static_cast<int32_t>(output.size()));
        if (size < 0) throw std::runtime_error("The GGUF chat template could not be applied.");
        output.resize(size);
        return java_string(env, output);
    } catch (const std::exception & error) {
        jclass exception = env->FindClass("java/lang/IllegalStateException");
        env->ThrowNew(exception, error.what());
        env->DeleteLocalRef(exception);
        return nullptr;
    }
}

extern "C" JNIEXPORT void JNICALL Java_org_lastphone_app_nativebridge_NativeLlama_cancel(JNIEnv *, jclass) {
    cancel_epoch.fetch_add(1);
}

extern "C" JNIEXPORT void JNICALL Java_org_lastphone_app_nativebridge_NativeLlama_unload(JNIEnv *, jclass) {
    cancel_epoch.fetch_add(1);
    std::lock_guard<std::mutex> lock(engine_mutex);
    engine.reset();
}
