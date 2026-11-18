# Source-tree package adapter for the pinned Khronos SPIRV-Headers checkout.
# The upstream project only installs a package config; Android builds need no install step.
if(NOT EXISTS "${SPIRV_HEADERS_SOURCE_DIR}/include/spirv/unified1/spirv.hpp")
    message(FATAL_ERROR "The pinned SPIRV-Headers source is missing.")
endif()
if(NOT TARGET SPIRV-Headers::SPIRV-Headers)
    add_library(SPIRV-Headers::SPIRV-Headers INTERFACE IMPORTED GLOBAL)
    set_target_properties(SPIRV-Headers::SPIRV-Headers PROPERTIES
        INTERFACE_INCLUDE_DIRECTORIES "${SPIRV_HEADERS_SOURCE_DIR}/include")
endif()
set(SPIRV-Headers_FOUND TRUE)
