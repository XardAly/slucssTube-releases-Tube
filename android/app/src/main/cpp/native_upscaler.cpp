#include <jni.h>

#include <android/log.h>

#include <algorithm>
#include <atomic>
#include <cmath>
#include <cstdint>
#include <memory>
#include <mutex>
#include <new>
#include <string>

#include "gpu.h"
#include "mat.h"
#include "net.h"
#include "realcugan.h"

namespace {

constexpr const char* kLogTag = "SlucssUpscaleNative";
constexpr int kAnimeVideoV3 = 0;
constexpr int kRealCugan = 1;

std::mutex g_gpu_mutex;
int g_gpu_references = 0;

void log_error(const std::string& message) {
    __android_log_print(ANDROID_LOG_ERROR, kLogTag, "%s", message.c_str());
}

void acquire_gpu_instance() {
    std::lock_guard<std::mutex> lock(g_gpu_mutex);
    if (g_gpu_references++ == 0) ncnn::create_gpu_instance();
}

void release_gpu_instance() {
    std::lock_guard<std::mutex> lock(g_gpu_mutex);
    if (g_gpu_references > 0 && --g_gpu_references == 0) ncnn::destroy_gpu_instance();
}

std::string to_string(JNIEnv* env, jstring value) {
    if (!value) return {};
    const char* chars = env->GetStringUTFChars(value, nullptr);
    if (!chars) return {};
    std::string result(chars);
    env->ReleaseStringUTFChars(value, chars);
    return result;
}

void throw_java(JNIEnv* env, const char* type, const std::string& message) {
    jclass klass = env->FindClass(type);
    if (klass) env->ThrowNew(klass, message.c_str());
}

int reflected(int value, int length) {
    if (length <= 1) return 0;
    while (value < 0 || value >= length) {
        value = value < 0 ? -value : 2 * length - 2 - value;
    }
    return value;
}

uint8_t clamp_byte(float value) {
    return static_cast<uint8_t>(std::clamp(std::lround(value), 0L, 255L));
}

struct ColorMatrix {
    float kr;
    float kb;
};

ColorMatrix matrix_for(int color_standard) {
    // MediaFormat.COLOR_STANDARD_BT2020 = 6; BT601 PAL/NTSC = 2/4.
    if (color_standard == 6) return {0.2627f, 0.0593f};
    if (color_standard == 2 || color_standard == 4) return {0.2990f, 0.1140f};
    return {0.2126f, 0.0722f};  // BT.709 e metadado ausente em vídeo HD.
}

struct Plane {
    uint8_t* data;
    jlong capacity;
    int row_stride;
    int pixel_stride;
};

bool plane_fits(const Plane& plane, int width, int height, int left = 0, int top = 0) {
    if (!plane.data || plane.capacity <= 0 || plane.row_stride <= 0 ||
        plane.pixel_stride <= 0 || width <= 0 || height <= 0) return false;
    const int64_t last = static_cast<int64_t>(top + height - 1) * plane.row_stride +
        static_cast<int64_t>(left + width - 1) * plane.pixel_stride;
    return last >= 0 && last < plane.capacity;
}

class UpscaleEngine {
public:
    UpscaleEngine(
        int model,
        int scale,
        const std::string& param_path,
        const std::string& bin_path,
        bool prefer_vulkan,
        int threads,
        int tile_size
    ) : model_(model), scale_(scale), tile_size_(std::max(32, tile_size)),
        threads_(std::max(1, threads)) {
        if (model_ != kAnimeVideoV3 && model_ != kRealCugan) {
            error_ = "Modelo nativo desconhecido.";
            return;
        }
        if (scale_ < 2 || scale_ > 4) {
            error_ = "Escala nativa inválida.";
            return;
        }

        if (prefer_vulkan) {
            acquire_gpu_instance();
            gpu_instance_acquired_ = true;
        }
        using_vulkan_ = gpu_instance_acquired_ && ncnn::get_gpu_count() > 0;
        gpu_index_ = using_vulkan_ ? ncnn::get_default_gpu_index() : -1;

        if (model_ == kRealCugan) {
            realcugan_.reset(new (std::nothrow) RealCUGAN(gpu_index_, false, threads_));
            if (!realcugan_) {
                error_ = "Memória insuficiente para iniciar o Real-CUGAN.";
                return;
            }
            if (realcugan_->load(param_path, bin_path) != 0) {
                error_ = "Não foi possível carregar os pesos do Real-CUGAN.";
                return;
            }
            realcugan_->noise = -1;
            realcugan_->scale = scale_;
            realcugan_->tilesize = tile_size_;
            realcugan_->prepadding = scale_ == 2 ? 18 : (scale_ == 3 ? 14 : 19);
            realcugan_->syncgap = 3;
            realcugan_->set_cancel_flag(&cancelled_);
        } else {
            anime_net_.opt.num_threads = threads_;
            anime_net_.opt.use_vulkan_compute = using_vulkan_;
            anime_net_.opt.use_fp16_packed = using_vulkan_;
            anime_net_.opt.use_fp16_storage = using_vulkan_;
            anime_net_.opt.use_fp16_arithmetic = false;
            anime_net_.opt.use_int8_storage = false;
            if (using_vulkan_) anime_net_.set_vulkan_device(gpu_index_);
            if (anime_net_.load_param(param_path.c_str()) != 0 ||
                anime_net_.load_model(bin_path.c_str()) != 0) {
                error_ = "Não foi possível carregar os pesos do AnimeVideoV3.";
                return;
            }
        }
        initialized_ = true;
    }

    ~UpscaleEngine() {
        realcugan_.reset();
        anime_net_.clear();
        if (gpu_instance_acquired_) release_gpu_instance();
    }

    UpscaleEngine(const UpscaleEngine&) = delete;
    UpscaleEngine& operator=(const UpscaleEngine&) = delete;

    bool using_vulkan() const { return using_vulkan_; }
    bool initialized() const { return initialized_; }
    const std::string& error() const { return error_; }
    int scale() const { return scale_; }
    void cancel() { cancelled_.store(true, std::memory_order_relaxed); }

    int process(const ncnn::Mat& input, int width, int height, ncnn::Mat& output) {
        if (!initialized_) return -1;
        if (cancelled_.load(std::memory_order_relaxed)) return -20;
        if (model_ == kRealCugan) {
            output.create(
                width * scale_, height * scale_, static_cast<size_t>(3u), 3
            );
            if (output.empty()) return -12;
            const int result = realcugan_->process(input, output);
            return cancelled_.load(std::memory_order_relaxed) ? -20 : result;
        }
        return process_anime(input, width, height, output);
    }

private:
    int process_anime(
        const ncnn::Mat& input,
        int width,
        int height,
        ncnn::Mat& output
    ) {
        output.create(width * scale_, height * scale_, static_cast<size_t>(3u), 3);
        if (output.empty()) return -12;
        auto* destination = static_cast<uint8_t*>(output.data);
        const auto* source = static_cast<const uint8_t*>(input.data);
        constexpr int padding = 10;

        for (int tile_y = 0; tile_y < height; tile_y += tile_size_) {
            const int inner_h = std::min(tile_size_, height - tile_y);
            for (int tile_x = 0; tile_x < width; tile_x += tile_size_) {
                if (cancelled_.load(std::memory_order_relaxed)) return -20;
                const int inner_w = std::min(tile_size_, width - tile_x);
                const int padded_w = inner_w + padding * 2;
                const int padded_h = inner_h + padding * 2;
                ncnn::Mat tile;
                tile.create(padded_w, padded_h, 3, static_cast<size_t>(4u), nullptr);
                if (tile.empty()) return -12;

                for (int channel = 0; channel < 3; ++channel) {
                    float* dst = tile.channel(channel);
                    for (int y = 0; y < padded_h; ++y) {
                        const int source_y = reflected(tile_y + y - padding, height);
                        for (int x = 0; x < padded_w; ++x) {
                            const int source_x = reflected(tile_x + x - padding, width);
                            dst[y * padded_w + x] =
                                source[(source_y * width + source_x) * 3 + channel] / 255.f;
                        }
                    }
                }

                ncnn::Extractor extractor = anime_net_.create_extractor();
                extractor.set_light_mode(true);
                if (extractor.input("data", tile) != 0) return -30;
                ncnn::Mat enhanced;
                if (extractor.extract("output", enhanced) != 0 || enhanced.empty()) return -31;
                if (enhanced.elembits() != 32 || enhanced.c < 3) return -32;

                const int crop = padding * scale_;
                const int copy_w = inner_w * scale_;
                const int copy_h = inner_h * scale_;
                if (enhanced.w < crop + copy_w || enhanced.h < crop + copy_h) return -33;
                for (int channel = 0; channel < 3; ++channel) {
                    const float* src = enhanced.channel(channel);
                    for (int y = 0; y < copy_h; ++y) {
                        for (int x = 0; x < copy_w; ++x) {
                            const int out_x = tile_x * scale_ + x;
                            const int out_y = tile_y * scale_ + y;
                            destination[(out_y * width * scale_ + out_x) * 3 + channel] =
                                clamp_byte(src[(y + crop) * enhanced.w + x + crop] * 255.f);
                        }
                    }
                }
            }
        }
        return 0;
    }

    int model_;
    int scale_;
    int tile_size_;
    int threads_;
    int gpu_index_ = -1;
    bool using_vulkan_ = false;
    bool initialized_ = false;
    bool gpu_instance_acquired_ = false;
    std::string error_;
    std::atomic<bool> cancelled_{false};
    ncnn::Net anime_net_;
    std::unique_ptr<RealCUGAN> realcugan_;
};

bool yuv_to_rgb(
    const Plane& y_plane,
    const Plane& u_plane,
    const Plane& v_plane,
    int crop_left,
    int crop_top,
    int width,
    int height,
    int color_standard,
    int color_range,
    ncnn::Mat& rgb
) {
    const int chroma_left = crop_left / 2;
    const int chroma_top = crop_top / 2;
    if (!plane_fits(y_plane, width, height, crop_left, crop_top) ||
        !plane_fits(u_plane, (width + 1) / 2, (height + 1) / 2, chroma_left, chroma_top) ||
        !plane_fits(v_plane, (width + 1) / 2, (height + 1) / 2, chroma_left, chroma_top)) {
        return false;
    }

    const ColorMatrix matrix = matrix_for(color_standard);
    const float kg = 1.f - matrix.kr - matrix.kb;
    const bool full = color_range == 1;  // MediaFormat.COLOR_RANGE_FULL.
    auto* destination = static_cast<uint8_t*>(rgb.data);
    for (int y = 0; y < height; ++y) {
        for (int x = 0; x < width; ++x) {
            const float y_code = y_plane.data[(crop_top + y) * y_plane.row_stride +
                (crop_left + x) * y_plane.pixel_stride];
            const float u_code = u_plane.data[(chroma_top + y / 2) * u_plane.row_stride +
                (chroma_left + x / 2) * u_plane.pixel_stride];
            const float v_code = v_plane.data[(chroma_top + y / 2) * v_plane.row_stride +
                (chroma_left + x / 2) * v_plane.pixel_stride];
            const float luminance = full ? y_code : (y_code - 16.f) * (255.f / 219.f);
            const float cb = (u_code - 128.f) * (full ? 1.f : 255.f / 224.f);
            const float cr = (v_code - 128.f) * (full ? 1.f : 255.f / 224.f);
            const float red = luminance + 2.f * (1.f - matrix.kr) * cr;
            const float blue = luminance + 2.f * (1.f - matrix.kb) * cb;
            const float green = (luminance - matrix.kr * red - matrix.kb * blue) / kg;
            const size_t index = (static_cast<size_t>(y) * width + x) * 3;
            destination[index] = clamp_byte(red);
            destination[index + 1] = clamp_byte(green);
            destination[index + 2] = clamp_byte(blue);
        }
    }
    return true;
}

bool rgb_to_yuv(
    const ncnn::Mat& rgb,
    const Plane& y_plane,
    const Plane& u_plane,
    const Plane& v_plane,
    int width,
    int height,
    int color_standard,
    int color_range
) {
    if (!plane_fits(y_plane, width, height) ||
        !plane_fits(u_plane, (width + 1) / 2, (height + 1) / 2) ||
        !plane_fits(v_plane, (width + 1) / 2, (height + 1) / 2) ||
        rgb.w != width || rgb.h != height || rgb.elempack != 3) return false;

    const auto* source = static_cast<const uint8_t*>(rgb.data);
    const ColorMatrix matrix = matrix_for(color_standard);
    const float kg = 1.f - matrix.kr - matrix.kb;
    const bool full = color_range == 1;

    for (int y = 0; y < height; ++y) {
        for (int x = 0; x < width; ++x) {
            const size_t index = (static_cast<size_t>(y) * width + x) * 3;
            const float luminance = matrix.kr * source[index] +
                kg * source[index + 1] + matrix.kb * source[index + 2];
            const float code = full ? luminance : 16.f + luminance * (219.f / 255.f);
            y_plane.data[y * y_plane.row_stride + x * y_plane.pixel_stride] = clamp_byte(code);
        }
    }

    for (int y = 0; y < height; y += 2) {
        for (int x = 0; x < width; x += 2) {
            float cb_sum = 0.f;
            float cr_sum = 0.f;
            int samples = 0;
            for (int dy = 0; dy < 2 && y + dy < height; ++dy) {
                for (int dx = 0; dx < 2 && x + dx < width; ++dx) {
                    const size_t index = (static_cast<size_t>(y + dy) * width + x + dx) * 3;
                    const float red = source[index];
                    const float green = source[index + 1];
                    const float blue = source[index + 2];
                    const float luminance = matrix.kr * red + kg * green + matrix.kb * blue;
                    cb_sum += (blue - luminance) / (2.f * (1.f - matrix.kb));
                    cr_sum += (red - luminance) / (2.f * (1.f - matrix.kr));
                    ++samples;
                }
            }
            const float chroma_scale = full ? 1.f : 224.f / 255.f;
            const uint8_t u = clamp_byte(128.f + cb_sum / samples * chroma_scale);
            const uint8_t v = clamp_byte(128.f + cr_sum / samples * chroma_scale);
            const int chroma_x = x / 2;
            const int chroma_y = y / 2;
            u_plane.data[chroma_y * u_plane.row_stride + chroma_x * u_plane.pixel_stride] = u;
            v_plane.data[chroma_y * v_plane.row_stride + chroma_x * v_plane.pixel_stride] = v;
        }
    }
    return true;
}

Plane direct_plane(JNIEnv* env, jobject buffer, jint row_stride, jint pixel_stride) {
    return {
        static_cast<uint8_t*>(env->GetDirectBufferAddress(buffer)),
        env->GetDirectBufferCapacity(buffer),
        row_stride,
        pixel_stride,
    };
}

}  // namespace

extern "C" JNIEXPORT jlong JNICALL
Java_com_xard_ytsystem_upscale_NativeUpscaler_nativeCreate(
    JNIEnv* env,
    jobject,
    jint model,
    jint scale,
    jstring param_path,
    jstring bin_path,
    jboolean prefer_vulkan,
    jint threads,
    jint tile_size
) {
    auto* engine = new (std::nothrow) UpscaleEngine(
        model,
        scale,
        to_string(env, param_path),
        to_string(env, bin_path),
        prefer_vulkan == JNI_TRUE,
        threads,
        tile_size
    );
    if (!engine) {
        const std::string message = "Memória insuficiente para iniciar o upscale nativo.";
        log_error(message);
        throw_java(env, "java/lang/IllegalStateException", message);
        return 0;
    }
    if (!engine->initialized()) {
        log_error(engine->error());
        throw_java(env, "java/lang/IllegalStateException", engine->error());
        delete engine;
        return 0;
    }
    return reinterpret_cast<jlong>(engine);
}

extern "C" JNIEXPORT jboolean JNICALL
Java_com_xard_ytsystem_upscale_NativeUpscaler_nativeUsesVulkan(
    JNIEnv*, jobject, jlong handle
) {
    auto* engine = reinterpret_cast<UpscaleEngine*>(handle);
    return engine && engine->using_vulkan() ? JNI_TRUE : JNI_FALSE;
}

extern "C" JNIEXPORT void JNICALL
Java_com_xard_ytsystem_upscale_NativeUpscaler_nativeCancel(
    JNIEnv*, jobject, jlong handle
) {
    auto* engine = reinterpret_cast<UpscaleEngine*>(handle);
    if (engine) engine->cancel();
}

extern "C" JNIEXPORT void JNICALL
Java_com_xard_ytsystem_upscale_NativeUpscaler_nativeDestroy(
    JNIEnv*, jobject, jlong handle
) {
    delete reinterpret_cast<UpscaleEngine*>(handle);
}

extern "C" JNIEXPORT jint JNICALL
Java_com_xard_ytsystem_upscale_NativeUpscaler_nativeProcess(
    JNIEnv* env,
    jobject,
    jlong handle,
    jobject source_y,
    jobject source_u,
    jobject source_v,
    jint source_y_row,
    jint source_u_row,
    jint source_v_row,
    jint source_y_pixel,
    jint source_u_pixel,
    jint source_v_pixel,
    jint crop_left,
    jint crop_top,
    jint width,
    jint height,
    jobject target_y,
    jobject target_u,
    jobject target_v,
    jint target_y_row,
    jint target_u_row,
    jint target_v_row,
    jint target_y_pixel,
    jint target_u_pixel,
    jint target_v_pixel,
    jint color_standard,
    jint color_range
) {
    auto* engine = reinterpret_cast<UpscaleEngine*>(handle);
    if (!engine) return -1;
    const Plane source_planes[] = {
        direct_plane(env, source_y, source_y_row, source_y_pixel),
        direct_plane(env, source_u, source_u_row, source_u_pixel),
        direct_plane(env, source_v, source_v_row, source_v_pixel),
    };
    const Plane target_planes[] = {
        direct_plane(env, target_y, target_y_row, target_y_pixel),
        direct_plane(env, target_u, target_u_row, target_u_pixel),
        direct_plane(env, target_v, target_v_row, target_v_pixel),
    };
    ncnn::Mat rgb;
    rgb.create(width, height, static_cast<size_t>(3u), 3);
    if (rgb.empty()) return -12;
    if (!yuv_to_rgb(
        source_planes[0], source_planes[1], source_planes[2],
        crop_left, crop_top, width, height, color_standard, color_range, rgb
    )) return -2;

    ncnn::Mat enhanced;
    const int process_result = engine->process(rgb, width, height, enhanced);
    if (process_result != 0) return process_result;
    if (!rgb_to_yuv(
        enhanced, target_planes[0], target_planes[1], target_planes[2],
        width * engine->scale(), height * engine->scale(), color_standard, color_range
    )) return -3;
    return 0;
}

extern "C" JNIEXPORT jstring JNICALL
Java_com_xard_ytsystem_upscale_NativeUpscaler_nativeGpuInfo(JNIEnv* env, jobject) {
    std::string result = "0||0";
    acquire_gpu_instance();
    const int count = ncnn::get_gpu_count();
    if (count > 0) {
        const int index = ncnn::get_default_gpu_index();
        const ncnn::GpuInfo& info = ncnn::get_gpu_info(index);
        result = std::to_string(count) + "|" + info.device_name() + "|" +
            std::to_string(ncnn::get_gpu_device(index)->get_heap_budget());
    }
    release_gpu_instance();
    return env->NewStringUTF(result.c_str());
}
