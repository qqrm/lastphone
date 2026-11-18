# The pinned upstream Vulkan generator caps each process at 16 glslc children,
# while Ninja may execute many generator processes at once. This build-only
# patch bounds both levels for CI machines with a small memory allocation.
# All source remains under the upstream MIT license.

set(LASTPHONE_GGML_SOURCE "${CMAKE_CURRENT_BINARY_DIR}/lastphone-ggml-src")
set(_lastphone_copy_marker "${LASTPHONE_GGML_SOURCE}/lastphone-source.txt")
set(_lastphone_copy_revision "${LASTPHONE_LLAMA_COMMIT}:bounded-shader-build-v1")
set(_lastphone_existing_revision "")
if(EXISTS "${_lastphone_copy_marker}")
    file(READ "${_lastphone_copy_marker}" _lastphone_existing_revision)
endif()
if(NOT _lastphone_existing_revision STREQUAL _lastphone_copy_revision)
    file(REMOVE_RECURSE "${LASTPHONE_GGML_SOURCE}")
    file(COPY "${LLAMA_SRC}/ggml/" DESTINATION "${LASTPHONE_GGML_SOURCE}")
    file(WRITE "${_lastphone_copy_marker}" "${_lastphone_copy_revision}")
endif()

set(_lastphone_vulkan "src/ggml-vulkan")
file(READ "${LLAMA_SRC}/ggml/${_lastphone_vulkan}/CMakeLists.txt" _lastphone_cmake)
string(REGEX MATCHALL "COMMENT \"Generate vulkan shaders" _lastphone_commands "${_lastphone_cmake}")
list(LENGTH _lastphone_commands _lastphone_command_count)
if(NOT _lastphone_command_count EQUAL 2)
    message(FATAL_ERROR "Pinned Vulkan custom commands changed; review the bounded-build patch.")
endif()
string(REPLACE "COMMENT \"Generate vulkan shaders"
    "JOB_POOL lastphone_shaders\n        COMMENT \"Generate vulkan shaders"
    _lastphone_cmake "${_lastphone_cmake}")
file(CONFIGURE OUTPUT "${LASTPHONE_GGML_SOURCE}/${_lastphone_vulkan}/CMakeLists.txt"
    CONTENT "${_lastphone_cmake}" @ONLY)

set(_lastphone_generator "${_lastphone_vulkan}/vulkan-shaders/vulkan-shaders-gen.cpp")
file(READ "${LLAMA_SRC}/ggml/${_lastphone_generator}" _lastphone_cpp)
set(_lastphone_limit_before "std::min(16u, std::thread::hardware_concurrency())")
string(FIND "${_lastphone_cpp}" "${_lastphone_limit_before}" _lastphone_limit_index)
if(_lastphone_limit_index EQUAL -1)
    message(FATAL_ERROR "Pinned Vulkan thread limit changed; review the bounded-build patch.")
endif()
string(REPLACE "${_lastphone_limit_before}"
    "std::min(${LASTPHONE_SHADER_JOBS}u, std::thread::hardware_concurrency())"
    _lastphone_cpp "${_lastphone_cpp}")
file(CONFIGURE OUTPUT "${LASTPHONE_GGML_SOURCE}/${_lastphone_generator}"
    CONTENT "${_lastphone_cpp}" @ONLY)
message(STATUS "Lastphone Vulkan build: 1 generator, ${LASTPHONE_SHADER_JOBS} glslc jobs, ${LASTPHONE_NATIVE_JOBS} C/C++ jobs")
