import org.gradle.api.artifacts.transform.CacheableTransform
import org.gradle.api.artifacts.transform.InputArtifact
import org.gradle.api.artifacts.transform.TransformAction
import org.gradle.api.artifacts.transform.TransformOutputs
import org.gradle.api.artifacts.transform.TransformParameters
import org.gradle.api.artifacts.type.ArtifactTypeDefinition
import org.gradle.api.attributes.Attribute
import org.gradle.api.file.FileSystemLocation
import org.gradle.api.provider.Provider
import org.gradle.api.tasks.PathSensitive
import org.gradle.api.tasks.PathSensitivity
import java.util.zip.ZipEntry
import java.util.zip.ZipFile
import java.util.zip.ZipOutputStream

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("com.chaquo.python")
}

/** Keep dependency metadata intact while selecting the NDK's one shared C++ runtime. */
@CacheableTransform
abstract class RemoveKiwixCppRuntime : TransformAction<TransformParameters.None> {
    @get:InputArtifact
    @get:PathSensitive(PathSensitivity.NAME_ONLY)
    abstract val inputArtifact: Provider<FileSystemLocation>

    override fun transform(outputs: TransformOutputs) {
        val input = inputArtifact.get().asFile
        if (input.name != "libkiwix-2.6.0.aar") {
            outputs.file(input)
            return
        }
        val removed = mutableSetOf<String>()
        val runtime = Regex("jni/[^/]+/libc\\+\\+_shared\\.so")
        val output = outputs.file("libkiwix-2.6.0-ndk-runtime.aar")
        ZipFile(input).use { source ->
            ZipOutputStream(output.outputStream().buffered()).use { destination ->
                source.entries().asSequence().sortedBy { it.name }.forEach { entry ->
                    if (runtime.matches(entry.name)) {
                        removed += entry.name
                    } else {
                        destination.putNextEntry(ZipEntry(entry.name).apply { time = 0L })
                        if (!entry.isDirectory) source.getInputStream(entry).use { it.copyTo(destination) }
                        destination.closeEntry()
                    }
                }
            }
        }
        check(removed.containsAll(setOf("jni/arm64-v8a/libc++_shared.so", "jni/x86_64/libc++_shared.so"))) {
            "The pinned libkiwix AAR no longer has the expected C++ runtime entries: $removed"
        }
    }
}

val ndkCppRuntime = Attribute.of("org.lastphone.ndk-cpp-runtime", Boolean::class.javaObjectType)
dependencies {
    attributesSchema { attribute(ndkCppRuntime) }
    artifactTypes.maybeCreate("aar").attributes.attribute(ndkCppRuntime, false)
    registerTransform(RemoveKiwixCppRuntime::class) {
        from.attribute(ArtifactTypeDefinition.ARTIFACT_TYPE_ATTRIBUTE, "aar").attribute(ndkCppRuntime, false)
        to.attribute(ArtifactTypeDefinition.ARTIFACT_TYPE_ATTRIBUTE, "aar").attribute(ndkCppRuntime, true)
    }
}
configurations.configureEach { attributes.attribute(ndkCppRuntime, true) }

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
    sourceSets["androidTest"].assets.srcDir(rootDir.resolve("tests/fixtures"))
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
