cmake_minimum_required(VERSION 3.22.1)
project(lastphone_native LANGUAGES C CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

# CI hosts may report far more CPUs than their RAM allocation can sustain.
# Native translation units and generated Vulkan shaders are memory intensive.
set(LASTPHONE_NATIVE_JOBS 2 CACHE STRING "Concurrent native compile jobs")
set(LASTPHONE_SHADER_JOBS 2 CACHE STRING "Concurrent glslc processes per shader generator")
if(NOT LASTPHONE_NATIVE_JOBS MATCHES "^[1-9][0-9]*$" OR NOT LASTPHONE_SHADER_JOBS MATCHES "^[1-9][0-9]*$")
    message(FATAL_ERROR "Native and shader job counts must be positive integers.")
endif()
set_property(GLOBAL PROPERTY JOB_POOLS "lastphone_compile=${LASTPHONE_NATIVE_JOBS};lastphone_link=1;lastphone_shaders=1")
set(CMAKE_JOB_POOL_COMPILE lastphone_compile)
set(CMAKE_JOB_POOL_LINK lastphone_link)

get_filename_component(LASTPHONE_ROOT "${CMAKE_CURRENT_LIST_DIR}/../../../../../.." ABSOLUTE)
set(LASTPHONE_LLAMA_COMMIT "dde879bc4f954b809892fd0d660515f42536394a")
set(LLAMA_SRC "${LASTPHONE_ROOT}/mobile/ipad/build/deps/llama.cpp" CACHE PATH "Pinned llama.cpp source checkout")
if(NOT EXISTS "${LLAMA_SRC}/CMakeLists.txt")
    message(FATAL_ERROR "Run mobile/ipad/scripts/prepare_dependencies.sh before building Android.")
endif()
find_package(Git REQUIRED)
execute_process(COMMAND "${GIT_EXECUTABLE}" -C "${LLAMA_SRC}" rev-parse HEAD
    OUTPUT_VARIABLE LLAMA_ACTUAL_COMMIT OUTPUT_STRIP_TRAILING_WHITESPACE COMMAND_ERROR_IS_FATAL ANY)
if(NOT LLAMA_ACTUAL_COMMIT STREQUAL LASTPHONE_LLAMA_COMMIT)
    message(FATAL_ERROR "llama.cpp must be ${LASTPHONE_LLAMA_COMMIT}; got ${LLAMA_ACTUAL_COMMIT}")
endif()
execute_process(COMMAND "${GIT_EXECUTABLE}" -C "${LLAMA_SRC}" diff --quiet HEAD --
    RESULT_VARIABLE LLAMA_MODIFIED)
if(NOT LLAMA_MODIFIED EQUAL 0)
    message(FATAL_ERROR "The pinned llama.cpp checkout has local changes.")
endif()

option(LASTPHONE_VULKAN "Compile the real Vulkan inference backend" ON)
set(BUILD_SHARED_LIBS OFF CACHE BOOL "" FORCE)
set(GGML_BACKEND_DL OFF CACHE BOOL "" FORCE)
set(GGML_NATIVE OFF CACHE BOOL "" FORCE)
set(GGML_OPENMP OFF CACHE BOOL "" FORCE)
set(GGML_RPC OFF CACHE BOOL "" FORCE)
set(GGML_VULKAN ${LASTPHONE_VULKAN} CACHE BOOL "" FORCE)
set(LLAMA_BUILD_COMMON OFF CACHE BOOL "" FORCE)
set(LLAMA_BUILD_APP OFF CACHE BOOL "" FORCE)
set(LLAMA_BUILD_TOOLS OFF CACHE BOOL "" FORCE)
set(LLAMA_BUILD_TESTS OFF CACHE BOOL "" FORCE)
set(LLAMA_BUILD_EXAMPLES OFF CACHE BOOL "" FORCE)
set(LLAMA_BUILD_SERVER OFF CACHE BOOL "" FORCE)
set(LLAMA_BUILD_MTMD OFF CACHE BOOL "" FORCE)
set(LLAMA_CURL OFF CACHE BOOL "" FORCE)
set(LLAMA_OPENSSL OFF CACHE BOOL "" FORCE)

if(LASTPHONE_VULKAN)
    # NDK's C headers lag its shader compiler and omit Vulkan-Hpp. Pin the complete
    # Khronos headers instead of accidentally using the build host's installation.
    include(FetchContent)
    FetchContent_Declare(lastphone_vulkan_headers
        GIT_REPOSITORY https://github.com/KhronosGroup/Vulkan-Headers.git
        GIT_TAG 952f776f6573aafbb62ea717d871cd1d6816c387)
    FetchContent_Declare(lastphone_spirv_headers
        GIT_REPOSITORY https://github.com/KhronosGroup/SPIRV-Headers.git
        GIT_TAG 09913f088a1197aba4aefd300a876b2ebbaa3391)
    FetchContent_GetProperties(lastphone_vulkan_headers)
    if(NOT lastphone_vulkan_headers_POPULATED)
        FetchContent_Populate(lastphone_vulkan_headers)
    endif()
    FetchContent_GetProperties(lastphone_spirv_headers)
    if(NOT lastphone_spirv_headers_POPULATED)
        FetchContent_Populate(lastphone_spirv_headers)
    endif()
    set(Vulkan_INCLUDE_DIR "${lastphone_vulkan_headers_SOURCE_DIR}/include" CACHE PATH "" FORCE)
    set(SPIRV_HEADERS_SOURCE_DIR "${lastphone_spirv_headers_SOURCE_DIR}")
    set(SPIRV-Headers_DIR "${CMAKE_CURRENT_LIST_DIR}/cmake" CACHE PATH "" FORCE)
    if(CMAKE_HOST_SYSTEM_NAME STREQUAL "Linux")
        set(LASTPHONE_NDK_HOST "linux-x86_64")
    elseif(CMAKE_HOST_SYSTEM_NAME STREQUAL "Darwin")
        set(LASTPHONE_NDK_HOST "darwin-x86_64")
    elseif(CMAKE_HOST_SYSTEM_NAME STREQUAL "Windows")
        set(LASTPHONE_NDK_HOST "windows-x86_64")
    endif()
    find_program(Vulkan_GLSLC_EXECUTABLE NAMES glslc
        HINTS "${ANDROID_NDK}/shader-tools/${LASTPHONE_NDK_HOST}" NO_CMAKE_FIND_ROOT_PATH REQUIRED)
endif()

# Release optimization is essential for usable on-device inference, including debug APKs.
add_compile_options(-O3 -DNDEBUG)
if(LASTPHONE_VULKAN)
    # llama explicitly supports a parent-provided ggml target. The build-local
    # copy changes build concurrency only; the verified source checkout remains
    # pristine, and no kernels or optional Vulkan features are removed.
    include(cmake/prepare_bounded_ggml.cmake)
    add_subdirectory("${LASTPHONE_GGML_SOURCE}" "${CMAKE_CURRENT_BINARY_DIR}/llama/ggml")
endif()
add_subdirectory("${LLAMA_SRC}" "${CMAKE_CURRENT_BINARY_DIR}/llama")
if(LASTPHONE_VULKAN)
    target_link_libraries(ggml-vulkan PRIVATE SPIRV-Headers::SPIRV-Headers)
endif()

add_library(lastphone_llm SHARED llama_jni.cpp
    "${LLAMA_SRC}/common/fit.cpp"
    "${LLAMA_SRC}/common/log.cpp"
    "${LLAMA_SRC}/common/json.cpp"
    "${LASTPHONE_ROOT}/packaging/llm/fit_unused_win.cpp")
target_include_directories(lastphone_llm PRIVATE
    "${LLAMA_SRC}/include" "${LLAMA_SRC}/ggml/include" "${LLAMA_SRC}/common"
    "${LLAMA_SRC}/vendor" "${LLAMA_SRC}/src")
target_compile_definitions(lastphone_llm PRIVATE LASTPHONE_LLAMA_COMMIT="${LASTPHONE_LLAMA_COMMIT}")
target_link_libraries(lastphone_llm PRIVATE llama android log)
target_link_options(lastphone_llm PRIVATE "-Wl,-z,max-page-size=16384")
