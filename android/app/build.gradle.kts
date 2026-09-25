import com.android.build.gradle.internal.api.BaseVariantOutputImpl
import org.gradle.api.tasks.Sync
import java.util.zip.ZipFile
import java.util.zip.ZipInputStream
import java.util.zip.ZipOutputStream
import java.util.zip.ZipEntry
import java.security.MessageDigest
import java.io.ByteArrayOutputStream

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.kapt")
}

val apiBaseUrl = providers.gradleProperty("SLUCSS_API_URL")
    .orElse(providers.environmentVariable("SLUCSS_API_URL"))
    .orElse("https://slucssytapinevey.squareweb.app")
val signingValues = mapOf(
    "storeFile" to providers.environmentVariable("SLUCSS_ANDROID_KEYSTORE").orNull,
    "storePassword" to providers.environmentVariable("SLUCSS_ANDROID_STORE_PASSWORD").orNull,
    "keyAlias" to providers.environmentVariable("SLUCSS_ANDROID_KEY_ALIAS").orNull,
    "keyPassword" to providers.environmentVariable("SLUCSS_ANDROID_KEY_PASSWORD").orNull,
)
val releaseSigningReady = signingValues.values.all { !it.isNullOrBlank() }
// Upstream FFmpeg 0.18.1 embeds five WebP libraries with 4 KB ELF alignment.
// Replace only those ARM64 libraries with the reproducible NDK 29 build.
val ffmpegArchive by configurations.creating { isTransitive = false }
val patchedFfmpeg = layout.buildDirectory.file("verified-libs/ffmpeg-0.18.1-webp16k.aar")
val webpLibraries = rootProject.file("../vendor/android-webp16k")
val prepareFfmpeg16k by tasks.registering {
    inputs.files(ffmpegArchive)
    inputs.dir(webpLibraries)
    outputs.file(patchedFfmpeg)
    doLast {
        fun sha256(file: java.io.File): String = file.inputStream().use { input ->
            val digest = MessageDigest.getInstance("SHA-256")
            val block = ByteArray(65536)
            while (true) { val n = input.read(block); if (n < 0) break; digest.update(block, 0, n) }
            digest.digest().joinToString("") { "%02x".format(it) }
        }
        check(sha256(ffmpegArchive.singleFile) == "0a87ffa6cf912b0fe76c1a99b9107f543ee2f247935fae2c71f0822eb7bc5f49") { "Unexpected upstream FFmpeg AAR" }
        webpLibraries.resolve("SHA256SUMS.txt").readLines().filter { it.isNotBlank() }.forEach {
            val (hash, name) = it.trim().split(Regex("\\s+"), limit = 2)
            check(sha256(webpLibraries.resolve(name)) == hash) { "WebP integrity failure: $name" }
        }
        val output = patchedFfmpeg.get().asFile
        output.parentFile.mkdirs()
        val replacements = listOf("libsharpyuv.so", "libwebp.so", "libwebpdecoder.so", "libwebpdemux.so", "libwebpmux.so")
        ZipFile(ffmpegArchive.singleFile).use { original ->
            ZipOutputStream(output.outputStream().buffered()).use { aar ->
                original.entries().asSequence().forEach { entry ->
                    aar.putNextEntry(ZipEntry(entry.name).apply { time = 0 })
                    if (entry.name == "jni/arm64-v8a/libffmpeg.zip.so") {
                        val buffer = ByteArrayOutputStream()
                        val replaced = mutableSetOf<String>()
                        ZipOutputStream(buffer).use { bundle ->
                            ZipInputStream(original.getInputStream(entry)).use { input ->
                                var nested = input.nextEntry
                                while (nested != null) {
                                    val name = nested.name
                                    bundle.putNextEntry(ZipEntry(name).apply { time = 0 })
                                    val lib = name.removePrefix("usr/lib/")
                                    if (name == "usr/lib/$lib" && lib in replacements) {
                                        webpLibraries.resolve(lib).inputStream().use { it.copyTo(bundle) }
                                        replaced.add(lib)
                                    } else input.copyTo(bundle)
                                    bundle.closeEntry()
                                    nested = input.nextEntry
                                }
                            }
                        }
                        check(replaced == replacements.toSet()) { "FFmpeg archive layout changed" }
                        aar.write(buffer.toByteArray())
                    } else original.getInputStream(entry).use { it.copyTo(aar) }
                    aar.closeEntry()
                }
            }
        }
    }
}
val generatedUpscaleAssets = layout.buildDirectory.dir("generated/upscaleAssets")
val prepareUpscaleAssets by tasks.registering(Sync::class) {
    into(generatedUpscaleAssets)
    from(rootProject.file("../vendor/upscale/realesrgan/models")) {
        include("realesr-animevideov3-x2.param", "realesr-animevideov3-x2.bin")
        include("realesr-animevideov3-x3.param", "realesr-animevideov3-x4.param")
        into("upscale/animevideov3")
    }
    from(rootProject.file("../vendor/upscale/realcugan/models-se")) {
        include("up2x-conservative.param", "up2x-conservative.bin")
        include("up3x-conservative.param", "up3x-conservative.bin")
        include("up4x-conservative.param", "up4x-conservative.bin")
        into("upscale/realcugan-se")
    }
}

android {
    namespace = "com.xard.ytsystem"
    compileSdk = 35
    ndkVersion = "29.0.14206865"

    defaultConfig {
        applicationId = "com.xard.ytsystem"
        minSdk = 26
        targetSdk = 35
        versionCode = 24
        versionName = "1.3.1"
        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
        buildConfigField("String", "API_BASE_URL", "\"${apiBaseUrl.get().trimEnd('/')}\"")
        // x86/x86_64 só existem em emulador e ChromeOS e custavam ~97 MB no APK:
        // o binário do FFmpeg/Python é repetido por arquitetura. Todas as
        // variantes seguem a política ARM do aplicativo para não anunciar uma
        // ABI sem o engine nativo de Upscale correspondente.
        ndk {
            abiFilters += listOf("arm64-v8a", "armeabi-v7a")
        }
        externalNativeBuild {
            cmake {
                cppFlags += listOf("-std=c++17", "-fvisibility=hidden")
                arguments += listOf("-DANDROID_STL=c++_shared")
            }
        }
    }

    // Um APK por arquitetura: o binário do FFmpeg/Python pesa ~45 MB por ABI e
    // quem tem arm64 não precisa carregar a fatia de 32 bits. O universal
    // continua sendo gerado para quem baixa manualmente e para clientes antigos,
    // que não sabem pedir a fatia certa.
    splits {
        abi {
            isEnable = true
            reset()
            include("arm64-v8a", "armeabi-v7a")
            isUniversalApk = true
        }
    }

    signingConfigs {
        if (releaseSigningReady) {
            create("release") {
                storeFile = file(signingValues.getValue("storeFile")!!)
                storePassword = signingValues.getValue("storePassword")
                keyAlias = signingValues.getValue("keyAlias")
                keyPassword = signingValues.getValue("keyPassword")
                enableV1Signing = true
                enableV2Signing = true
                enableV3Signing = true
                enableV4Signing = true
            }
        }
    }

    buildTypes {
        debug {
            applicationIdSuffix = ".debug"
            versionNameSuffix = "-debug"
        }
        release {
            isMinifyEnabled = true
            isShrinkResources = true
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro",
            )
            if (releaseSigningReady) signingConfig = signingConfigs.getByName("release")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
    externalNativeBuild {
        cmake {
            path = file("src/main/cpp/CMakeLists.txt")
            version = "3.22.1"
        }
    }
    buildFeatures {
        viewBinding = true
        buildConfig = true
    }
    packaging {
        jniLibs.useLegacyPackaging = true
        resources.excludes += setOf("META-INF/DEPENDENCIES", "META-INF/LICENSE*", "META-INF/NOTICE*")
    }
    androidResources {
        noCompress += setOf("param", "bin")
    }
    sourceSets.named("main") {
        assets.srcDir(generatedUpscaleAssets)
    }
    testOptions.unitTests.isIncludeAndroidResources = true
}

tasks.named("preBuild").configure { dependsOn(prepareUpscaleAssets) }

android.applicationVariants.all {
    outputs.all {
        val output = this as BaseVariantOutputImpl
        // O universal não tem filtro de ABI e mantém o nome histórico.
        val abi = output.getFilter(com.android.build.OutputFile.ABI)
        val sufixo = abi?.let { "-$it" }.orEmpty()
        output.outputFileName = if (buildType.name == "release") {
            "Slucss-System$sufixo.apk"
        } else {
            "Slucss-System-debug$sufixo.apk"
        }
    }
}

kapt {
    arguments {
        arg("room.schemaLocation", "$projectDir/schemas")
    }
}

gradle.taskGraph.whenReady {
    if (allTasks.any { it.name.contains("Release") } && !releaseSigningReady) {
        throw GradleException(
            "Release exige SLUCSS_ANDROID_KEYSTORE, SLUCSS_ANDROID_STORE_PASSWORD, " +
                "SLUCSS_ANDROID_KEY_ALIAS e SLUCSS_ANDROID_KEY_PASSWORD.",
        )
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.15.0")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("androidx.activity:activity-ktx:1.10.0")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.8.7")
    implementation("androidx.recyclerview:recyclerview:1.4.0")
    implementation("androidx.room:room-runtime:2.6.1")
    implementation("androidx.room:room-ktx:2.6.1")
    kapt("androidx.room:room-compiler:2.6.1")
    implementation("com.google.android.material:material:1.12.0")
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    implementation("com.google.code.gson:gson:2.11.0")
    implementation("com.fasterxml.jackson.core:jackson-databind:2.18.2")
    implementation("net.i2p.crypto:eddsa:0.3.0")
    implementation("io.github.junkfood02.youtubedl-android:library:0.18.1")
    ffmpegArchive("io.github.junkfood02.youtubedl-android:ffmpeg:0.18.1@aar")
    implementation(files(patchedFfmpeg).builtBy(prepareFfmpeg16k))

    testImplementation("junit:junit:4.13.2")
    testImplementation("androidx.room:room-testing:2.6.1")
    testImplementation("org.jetbrains.kotlinx:kotlinx-coroutines-test:1.9.0")
    androidTestImplementation("androidx.test.ext:junit:1.2.1")
    androidTestImplementation("androidx.test.espresso:espresso-core:3.6.1")
}
