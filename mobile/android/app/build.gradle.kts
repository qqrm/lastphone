plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("com.chaquo.python")
}

val upstreamRoot = rootDir.resolve("../..").canonicalFile
val enginePython = layout.buildDirectory.dir("generated/enginePython")
val engineAssets = layout.buildDirectory.dir("generated/engineAssets")

val prepareEnginePython by tasks.registering(Sync::class) {
    from(upstreamRoot) {
        include("server.py", "assistant.py", "retrieval.py", "llm_local.py")
        include("app/__init__.py", "app/bridge.py", "engine/**/*.py")
        exclude("engine/vendor-*/**", "**/__pycache__/**")
    }
    into(enginePython)
}
val prepareEngineAssets by tasks.registering(Sync::class) {
    from(upstreamRoot) {
        include("web/**", "content/**", "engine/setup/*.json", "engine/setup/*.pem")
        include("LICENSE", "THIRD_PARTY_NOTICES.md")
        exclude("web/home-area.json", "**/.DS_Store")
        into("engine")
    }
    from(rootDir.resolve("THIRD_PARTY_NOTICES.md")) { into("engine"); rename { "ANDROID_THIRD_PARTY_NOTICES.md" } }
    into(engineAssets)
}

android {
    namespace = "org.lastphone.app"
    compileSdk = 36
    ndkVersion = "29.0.14206865"
    defaultConfig {
        applicationId = "io.github.qqrm.lastphone"
        minSdk = 29
        targetSdk = 36
        versionCode = 1
        versionName = "0.1.0"
        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
        externalNativeBuild.cmake {
            arguments += listOf("-DANDROID_STL=c++_shared", "-DCMAKE_BUILD_TYPE=Release")
            cppFlags += listOf("-std=c++17")
        }
    }
    externalNativeBuild.cmake {
        path = file("src/main/cpp/CMakeLists.txt")
        version = "3.22.1"
    }
    // Chaquopy stores native extensions in assets, which ABI splits do not filter.
    // Separate variants keep emulator-only 4 KB libraries out of the phone APK.
    flavorDimensions += "device"
    productFlavors {
        create("phone") {
            dimension = "device"
            ndk { abiFilters += "arm64-v8a" }
        }
        create("emulator") {
            dimension = "device"
            ndk { abiFilters += "x86_64" }
        }
    }
    sourceSets["main"].assets.srcDir(engineAssets)
    buildFeatures { buildConfig = true }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
    packaging {
        jniLibs.useLegacyPackaging = false
        resources.excludes += setOf("META-INF/AL2.0", "META-INF/LGPL2.1")
    }
    buildTypes {
        debug { isDebuggable = true }
        release {
            isMinifyEnabled = false
            // Release signing is injected by CI or the owner. A debug key is never a release key.
            val keyFile = System.getenv("LASTPHONE_KEYSTORE")
            if (!keyFile.isNullOrBlank()) {
                signingConfig = signingConfigs.create("release") {
                    storeFile = file(keyFile)
                    storePassword = System.getenv("LASTPHONE_STORE_PASSWORD")
                    keyAlias = System.getenv("LASTPHONE_KEY_ALIAS")
                    keyPassword = System.getenv("LASTPHONE_KEY_PASSWORD")
                }
            }
        }
    }
    testOptions { unitTests.isReturnDefaultValues = true }
}

chaquopy {
    defaultConfig {
        version = "3.13"
        pip {
            install("beautifulsoup4==4.14.3")
            install("pypdf==6.7.5")
            // Chaquopy supplies this version for both Python 3.13 Android ABIs.
            install("numpy==1.26.2")
        }
    }
    sourceSets.getByName("main") { srcDir(enginePython) }
}

tasks.configureEach {
    if (name.startsWith("generate") && name.endsWith("PythonSources")) dependsOn(prepareEnginePython)
    if (name.contains("Python") && name != "prepareEnginePython") dependsOn(prepareEnginePython)
    if (name.startsWith("merge") && name.endsWith("Assets")) dependsOn(prepareEngineAssets)
}
tasks.named("preBuild") { dependsOn(prepareEnginePython, prepareEngineAssets) }

dependencies {
    implementation("androidx.activity:activity-ktx:1.10.1")
    implementation("androidx.webkit:webkit:1.14.0")
    implementation("androidx.core:core-ktx:1.16.0")
    implementation("org.kiwix:libkiwix:2.6.0")
    implementation("com.microsoft.onnxruntime:onnxruntime-android:1.23.2")
    testImplementation("junit:junit:4.13.2")
    androidTestImplementation("androidx.test.ext:junit:1.2.1")
    androidTestImplementation("androidx.test:runner:1.6.2")
    androidTestImplementation("androidx.test:rules:1.6.1")
    androidTestImplementation("androidx.test.espresso:espresso-core:3.6.1")
    androidTestImplementation("androidx.test.espresso:espresso-web:3.6.1")
}
