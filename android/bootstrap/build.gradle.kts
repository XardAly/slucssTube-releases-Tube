import com.android.build.gradle.internal.api.BaseVariantOutputImpl

plugins {
    id("com.android.application")
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

android {
    namespace = "com.xard.ytsystem.bootstrap"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.xard.ytsystem"
        minSdk = 26
        targetSdk = 35
        versionCode = 3
        versionName = "1.0.2-installer"
        buildConfigField("String", "API_BASE_URL", "\"${apiBaseUrl.get().trimEnd('/')}\"")
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
            applicationIdSuffix = ".bootstrap.debug"
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
    buildFeatures { buildConfig = true }
    packaging {
        resources.excludes += setOf("META-INF/DEPENDENCIES", "META-INF/LICENSE*", "META-INF/NOTICE*")
    }
}

android.applicationVariants.all {
    outputs.all {
        val output = this as BaseVariantOutputImpl
        output.outputFileName = if (buildType.name == "release") {
            "Slucss-System-Installer-1.0.2.apk"
        } else {
            "Slucss-System-Installer-1.0.2-debug.apk"
        }
    }
}

gradle.taskGraph.whenReady {
    if (allTasks.any { it.project == project && it.name.contains("Release") } && !releaseSigningReady) {
        throw GradleException(
            "Release exige SLUCSS_ANDROID_KEYSTORE, SLUCSS_ANDROID_STORE_PASSWORD, " +
                "SLUCSS_ANDROID_KEY_ALIAS e SLUCSS_ANDROID_KEY_PASSWORD.",
        )
    }
}

dependencies {
    implementation("androidx.core:core:1.15.0")
    implementation("com.google.code.gson:gson:2.11.0")
    implementation("net.i2p.crypto:eddsa:0.3.0")
    testImplementation("junit:junit:4.13.2")
}
