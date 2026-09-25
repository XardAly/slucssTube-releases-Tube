cmake_minimum_required(VERSION 3.22.1)
project(slucss_upscale LANGUAGES C CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

include(FetchContent)

# Pacote Android/Vulkan oficial do ncnn. O hash impede que um artefato
# substituído no servidor entre silenciosamente no APK.
FetchContent_Declare(
    ncnn_prebuilt
    URL https://github.com/Tencent/ncnn/releases/download/20260526/ncnn-20260526-android-vulkan.zip
    URL_HASH SHA256=26909c92eed35afed4a966b5e9e503fcb0a529691ea3f910ec2c94a4fff52804
)
FetchContent_GetProperties(ncnn_prebuilt)
if(NOT ncnn_prebuilt_POPULATED)
    FetchContent_Populate(ncnn_prebuilt)
endif()
set(ncnn_DIR "${ncnn_prebuilt_SOURCE_DIR}/${ANDROID_ABI}/lib/cmake/ncnn")
find_package(ncnn CONFIG REQUIRED)

# A classe Real-CUGAN oficial contém o tratamento de SE/sync-gap necessário
# para evitar emendas entre tiles. O commit fica fixo e não acompanha o branch.
FetchContent_Declare(
    realcugan_upstream
    GIT_REPOSITORY https://github.com/nihui/realcugan-ncnn-vulkan.git
    GIT_TAG 395302c5c70f1bff604c974e92e0a87e45c9f9ee
    GIT_SHALLOW FALSE
)
FetchContent_GetProperties(realcugan_upstream)
if(NOT realcugan_upstream_POPULATED)
    FetchContent_Populate(realcugan_upstream)
endif()

set(REALCUGAN_SRC "${realcugan_upstream_SOURCE_DIR}/src")

# O destrutor upstream não libera realcugan_4x_postproc e também assume que
# load() terminou antes de destruir os layers bicúbicos. Geramos uma cópia
# pontualmente corrigida para evitar vazamento Vulkan no 4x e crash se os
# pesos estiverem ausentes/corrompidos.
file(READ "${REALCUGAN_SRC}/realcugan.cpp" REALCUGAN_SOURCE)
file(READ "${REALCUGAN_SRC}/realcugan.h" REALCUGAN_HEADER)
string(REPLACE
    "#include <string>"
    "#include <atomic>\n#include <string>"
    REALCUGAN_HEADER
    "${REALCUGAN_HEADER}"
)
set(REALCUGAN_HEADER_PUBLIC_UPSTREAM [=[
public:
    // realcugan parameters
]=])
set(REALCUGAN_HEADER_PUBLIC_PATCHED [=[
public:
    void set_cancel_flag(const std::atomic<bool>* flag) { cancel_flag = flag; }
    bool cancelled() const
    {
        return cancel_flag && cancel_flag->load(std::memory_order_relaxed);
    }

    // realcugan parameters
]=])
string(FIND
    "${REALCUGAN_HEADER}"
    "${REALCUGAN_HEADER_PUBLIC_UPSTREAM}"
    REALCUGAN_HEADER_PUBLIC_OFFSET
)
if(REALCUGAN_HEADER_PUBLIC_OFFSET EQUAL -1)
    message(FATAL_ERROR "A API Real-CUGAN mudou; revise o patch de cancelamento.")
endif()
string(REPLACE
    "${REALCUGAN_HEADER_PUBLIC_UPSTREAM}"
    "${REALCUGAN_HEADER_PUBLIC_PATCHED}"
    REALCUGAN_HEADER
    "${REALCUGAN_HEADER}"
)
string(REPLACE
    "    bool tta_mode;"
    "    const std::atomic<bool>* cancel_flag = 0;\n    bool tta_mode;"
    REALCUGAN_HEADER
    "${REALCUGAN_HEADER}"
)
set(REALCUGAN_PATCHED_HEADER "${CMAKE_CURRENT_BINARY_DIR}/realcugan.h")
file(WRITE "${REALCUGAN_PATCHED_HEADER}" "${REALCUGAN_HEADER}")

set(REALCUGAN_DESTRUCTOR_UPSTREAM [=[
RealCUGAN::~RealCUGAN()
{
    // cleanup preprocess and postprocess pipeline
    {
        delete realcugan_preproc;
        delete realcugan_postproc;
    }

    bicubic_2x->destroy_pipeline(net.opt);
    delete bicubic_2x;

    bicubic_3x->destroy_pipeline(net.opt);
    delete bicubic_3x;

    bicubic_4x->destroy_pipeline(net.opt);
    delete bicubic_4x;
}
]=])
set(REALCUGAN_DESTRUCTOR_PATCHED [=[
RealCUGAN::~RealCUGAN()
{
    // cleanup preprocess and postprocess pipeline
    {
        delete realcugan_preproc;
        delete realcugan_postproc;
        delete realcugan_4x_postproc;
    }

    if (bicubic_2x)
    {
        bicubic_2x->destroy_pipeline(net.opt);
        delete bicubic_2x;
    }

    if (bicubic_3x)
    {
        bicubic_3x->destroy_pipeline(net.opt);
        delete bicubic_3x;
    }

    if (bicubic_4x)
    {
        bicubic_4x->destroy_pipeline(net.opt);
        delete bicubic_4x;
    }
}
]=])
string(FIND "${REALCUGAN_SOURCE}" "${REALCUGAN_DESTRUCTOR_UPSTREAM}" REALCUGAN_PATCH_OFFSET)
if(REALCUGAN_PATCH_OFFSET EQUAL -1)
    message(FATAL_ERROR "O destrutor Real-CUGAN mudou; revise o patch de ciclo de vida.")
endif()
string(REPLACE
    "${REALCUGAN_DESTRUCTOR_UPSTREAM}"
    "${REALCUGAN_DESTRUCTOR_PATCHED}"
    REALCUGAN_SOURCE
    "${REALCUGAN_SOURCE}"
)

# O runtime SE padrão (sync-gap 3) processa grupos 3x3. As verificações abaixo
# permitem cancelar entre esses grupos; os wrappers fazem a devolução dos
# allocators antes de propagar -20 para o JNI.
set(REALCUGAN_VERY_ROUGH_Y_LOOP [=[
    for (int yi = 0; yi + 2 < ytiles; yi += 3)
    {
]=])
set(REALCUGAN_VERY_ROUGH_Y_LOOP_CANCEL [=[
    for (int yi = 0; yi + 2 < ytiles; yi += 3)
    {
        if (cancelled()) return -20;
]=])
set(REALCUGAN_VERY_ROUGH_X_LOOP [=[
        for (int xi = 0; xi + 2 < xtiles; xi += 3)
        {
]=])
set(REALCUGAN_VERY_ROUGH_X_LOOP_CANCEL [=[
        for (int xi = 0; xi + 2 < xtiles; xi += 3)
        {
            if (cancelled()) return -20;
]=])
foreach(loop_name IN ITEMS Y X)
    string(FIND
        "${REALCUGAN_SOURCE}"
        "${REALCUGAN_VERY_ROUGH_${loop_name}_LOOP}"
        REALCUGAN_LOOP_OFFSET
    )
    if(REALCUGAN_LOOP_OFFSET EQUAL -1)
        message(FATAL_ERROR "Os loops Real-CUGAN mudaram; revise o patch de cancelamento.")
    endif()
    string(REPLACE
        "${REALCUGAN_VERY_ROUGH_${loop_name}_LOOP}"
        "${REALCUGAN_VERY_ROUGH_${loop_name}_LOOP_CANCEL}"
        REALCUGAN_SOURCE
        "${REALCUGAN_SOURCE}"
    )
endforeach()

set(REALCUGAN_VK_STAGE2_SIGNATURE [=[
int RealCUGAN::process_se_stage2(const ncnn::Mat& inimage, const std::vector<std::string>& names, ncnn::Mat& outimage, const ncnn::Option& opt, FeatureCache& cache) const
{
]=])
set(REALCUGAN_VK_STAGE2_CANCEL [=[
int RealCUGAN::process_se_stage2(const ncnn::Mat& inimage, const std::vector<std::string>& names, ncnn::Mat& outimage, const ncnn::Option& opt, FeatureCache& cache) const
{
    if (cancelled()) return -20;
]=])
set(REALCUGAN_CPU_STAGE2_SIGNATURE [=[
int RealCUGAN::process_cpu_se_stage2(const ncnn::Mat& inimage, const std::vector<std::string>& names, ncnn::Mat& outimage, FeatureCache& cache) const
{
]=])
set(REALCUGAN_CPU_STAGE2_CANCEL [=[
int RealCUGAN::process_cpu_se_stage2(const ncnn::Mat& inimage, const std::vector<std::string>& names, ncnn::Mat& outimage, FeatureCache& cache) const
{
    if (cancelled()) return -20;
]=])
foreach(stage IN ITEMS VK CPU)
    string(FIND
        "${REALCUGAN_SOURCE}"
        "${REALCUGAN_${stage}_STAGE2_SIGNATURE}"
        REALCUGAN_STAGE2_OFFSET
    )
    if(REALCUGAN_STAGE2_OFFSET EQUAL -1)
        message(FATAL_ERROR "O stage2 Real-CUGAN mudou; revise o cancelamento.")
    endif()
    string(REPLACE
        "${REALCUGAN_${stage}_STAGE2_SIGNATURE}"
        "${REALCUGAN_${stage}_STAGE2_CANCEL}"
        REALCUGAN_SOURCE
        "${REALCUGAN_SOURCE}"
    )
endforeach()

# Restringe a substituição ao corpo de cada stage2. Assim o cancelamento
# também é observado entre tiles da etapa final sem alterar os outros modos
# do runtime upstream, que possuem ciclos de vida de allocators diferentes.
function(patch_stage2_tile_cancellation SOURCE_VAR METHOD_SIGNATURE NEXT_SIGNATURE)
    set(SOURCE_TEXT "${${SOURCE_VAR}}")
    string(FIND "${SOURCE_TEXT}" "${METHOD_SIGNATURE}" METHOD_START)
    string(FIND "${SOURCE_TEXT}" "${NEXT_SIGNATURE}" METHOD_END)
    if(METHOD_START EQUAL -1 OR METHOD_END EQUAL -1 OR METHOD_END LESS_EQUAL METHOD_START)
        message(FATAL_ERROR "O corpo stage2 Real-CUGAN mudou; revise o cancelamento por tile.")
    endif()

    math(EXPR METHOD_LENGTH "${METHOD_END} - ${METHOD_START}")
    string(SUBSTRING "${SOURCE_TEXT}" 0 ${METHOD_START} SOURCE_PREFIX)
    string(SUBSTRING "${SOURCE_TEXT}" ${METHOD_START} ${METHOD_LENGTH} METHOD_TEXT)
    string(SUBSTRING "${SOURCE_TEXT}" ${METHOD_END} -1 SOURCE_SUFFIX)

    set(Y_LOOP "    for (int yi = 0; yi < ytiles; yi++)\n    {")
    set(Y_LOOP_CANCEL "    for (int yi = 0; yi < ytiles; yi++)\n    {\n        if (cancelled()) return -20;")
    set(X_LOOP "        for (int xi = 0; xi < xtiles; xi++)\n        {")
    set(X_LOOP_CANCEL "        for (int xi = 0; xi < xtiles; xi++)\n        {\n            if (cancelled()) return -20;")
    foreach(LOOP_NAME IN ITEMS Y X)
        string(FIND "${METHOD_TEXT}" "${${LOOP_NAME}_LOOP}" LOOP_OFFSET)
        if(LOOP_OFFSET EQUAL -1)
            message(FATAL_ERROR "Os tiles stage2 Real-CUGAN mudaram; revise o cancelamento.")
        endif()
        string(REPLACE
            "${${LOOP_NAME}_LOOP}"
            "${${LOOP_NAME}_LOOP_CANCEL}"
            METHOD_TEXT
            "${METHOD_TEXT}"
        )
    endforeach()

    set(${SOURCE_VAR} "${SOURCE_PREFIX}${METHOD_TEXT}${SOURCE_SUFFIX}" PARENT_SCOPE)
endfunction()

patch_stage2_tile_cancellation(
    REALCUGAN_SOURCE
    "int RealCUGAN::process_se_stage2("
    "int RealCUGAN::process_se_sync_gap("
)
patch_stage2_tile_cancellation(
    REALCUGAN_SOURCE
    "int RealCUGAN::process_cpu_se_stage2("
    "int RealCUGAN::process_cpu_se_sync_gap("
)

set(REALCUGAN_VK_CANCEL_CLEANUP [=[
    {
        cache.clear();
        vkdev->reclaim_blob_allocator(blob_vkallocator);
        vkdev->reclaim_staging_allocator(staging_vkallocator);
        return -20;
    }
]=])
set(REALCUGAN_CPU_CANCEL_CLEANUP [=[
    {
        cache.clear();
        return -20;
    }
]=])
set(REALCUGAN_VK_CALLS
    "process_se_very_rough_stage0(inimage, in0, out0, opt, cache)"
    "process_se_very_rough_sync_gap(inimage, gap0, opt, cache)"
    "process_se_stage2(inimage, in4, outimage, opt, cache)"
)
foreach(call IN LISTS REALCUGAN_VK_CALLS)
    string(FIND "${REALCUGAN_SOURCE}" "    ${call};" REALCUGAN_CALL_OFFSET)
    if(REALCUGAN_CALL_OFFSET EQUAL -1)
        message(FATAL_ERROR "O fluxo Vulkan Real-CUGAN mudou; revise o cancelamento.")
    endif()
    string(REPLACE
        "    ${call};"
        "    if (${call} != 0)\n${REALCUGAN_VK_CANCEL_CLEANUP}"
        REALCUGAN_SOURCE
        "${REALCUGAN_SOURCE}"
    )
endforeach()
set(REALCUGAN_CPU_CALLS
    "process_cpu_se_very_rough_stage0(inimage, in0, out0, cache)"
    "process_cpu_se_very_rough_sync_gap(inimage, gap0, cache)"
    "process_cpu_se_stage2(inimage, in4, outimage, cache)"
)
foreach(call IN LISTS REALCUGAN_CPU_CALLS)
    string(FIND "${REALCUGAN_SOURCE}" "    ${call};" REALCUGAN_CALL_OFFSET)
    if(REALCUGAN_CALL_OFFSET EQUAL -1)
        message(FATAL_ERROR "O fluxo CPU Real-CUGAN mudou; revise o cancelamento.")
    endif()
    string(REPLACE
        "    ${call};"
        "    if (${call} != 0)\n${REALCUGAN_CPU_CANCEL_CLEANUP}"
        REALCUGAN_SOURCE
        "${REALCUGAN_SOURCE}"
    )
endforeach()

set(REALCUGAN_PATCHED_SOURCE "${CMAKE_CURRENT_BINARY_DIR}/realcugan_patched.cpp")
file(WRITE "${REALCUGAN_PATCHED_SOURCE}" "${REALCUGAN_SOURCE}")

set(REALCUGAN_SHADERS
    realcugan_preproc
    realcugan_postproc
    realcugan_4x_postproc
    realcugan_preproc_tta
    realcugan_postproc_tta
    realcugan_4x_postproc_tta
)
set(REALCUGAN_SHADER_HEADERS)
foreach(shader IN LISTS REALCUGAN_SHADERS)
    set(header "${CMAKE_CURRENT_BINARY_DIR}/${shader}.comp.hex.h")
    add_custom_command(
        OUTPUT "${header}"
        COMMAND ${CMAKE_COMMAND}
            -DSHADER_SRC=${REALCUGAN_SRC}/${shader}.comp
            -DSHADER_COMP_HEADER=${header}
            -P ${REALCUGAN_SRC}/generate_shader_comp_header.cmake
        DEPENDS
            "${REALCUGAN_SRC}/${shader}.comp"
            "${REALCUGAN_SRC}/generate_shader_comp_header.cmake"
        VERBATIM
    )
    list(APPEND REALCUGAN_SHADER_HEADERS "${header}")
endforeach()
add_custom_target(realcugan_shaders DEPENDS ${REALCUGAN_SHADER_HEADERS})

add_library(
    slucss_upscale
    SHARED
    native_upscaler.cpp
    "${REALCUGAN_PATCHED_SOURCE}"
    ${REALCUGAN_SHADER_HEADERS}
)
add_dependencies(slucss_upscale realcugan_shaders)
target_include_directories(slucss_upscale PRIVATE
    "${CMAKE_CURRENT_BINARY_DIR}"
    "${REALCUGAN_SRC}"
)

find_library(android-lib android REQUIRED)
find_library(log-lib log REQUIRED)
find_library(vulkan-lib vulkan REQUIRED)
target_link_libraries(slucss_upscale PRIVATE
    ncnn
    ${android-lib}
    ${log-lib}
    ${vulkan-lib}
)

target_compile_options(slucss_upscale PRIVATE
    -Wall
    -Wextra
    -Wno-unused-parameter
)
