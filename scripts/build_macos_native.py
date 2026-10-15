"""Build the macOS arm64 media/Python dependencies from pinned source archives.

Run stages in order: bootstrap, images, ffmpeg, openssl, python, opencv, wheel.
No system-wide packages are installed. Build logs and sources are retained.
"""
from pathlib import Path
import argparse
import hashlib
import json
import os
import platform
import shlex
import shutil
import subprocess
import sys
import tarfile

from prepare_license_sources import fetch_source

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ["cmake==4.4.4", "ninja==1.13.2", "scikit-build==0.19.1",
         "wheel==0.48.0", "delocate==0.13.0", "numpy==2.0.2", "setuptools==69.5.1"]


class Builder:
    def __init__(self, root, jobs):
        self.root = root.resolve()
        self.work = self.root / "work"
        self.prefix = self.root / "prefix"
        self.tools = self.root / "tools/bin"
        self.logs = self.root / "logs"
        self.jobs = str(jobs)
        for path in (self.root, self.work, self.prefix, self.logs):
            path.mkdir(parents=True, exist_ok=True)
        self.sdk = Path(subprocess.check_output(["xcrun", "--show-sdk-path"], text=True).strip())
        self.env = dict(os.environ, PATH=f"{self.tools}:/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin",
                        SDKROOT=str(self.sdk), MACOSX_DEPLOYMENT_TARGET="13.0",
                        PKG_CONFIG_PATH=str(self.prefix / "lib/pkgconfig"),
                        PKG_CONFIG_LIBDIR=str(self.prefix / "lib/pkgconfig"),
                        CMAKE_BUILD_PARALLEL_LEVEL=self.jobs)
        # Some CLT installations keep libc++ headers only inside the SDK.
        self.env["CXXFLAGS"] = "-isystem" + str(self.sdk / "usr/include/c++/v1")
        self.commands = []

    def run(self, command, cwd, label):
        if shutil.disk_usage(self.root).free < 650 * 1024**2:
            raise RuntimeError("Stopped before the next stage: less than 650 MiB free")
        print(label, flush=True)
        self.commands.append({"label": label, "cwd": str(cwd), "args": list(map(str, command))})
        with (self.logs / (label + ".log")).open("w") as stream:
            result = subprocess.run(list(map(str, command)), cwd=cwd, env=self.env,
                                    stdout=stream, stderr=subprocess.STDOUT)
        if result.returncode:
            raise RuntimeError(f"{label} failed; see {self.logs / (label + '.log')}")

    def bootstrap(self):
        subprocess.run([sys.executable, "-m", "venv", str(self.root / "tools")], check=True)
        self.run([self.tools / "python", "-m", "pip", "install", "--no-cache-dir", *TOOLS],
                 ROOT, "tools-install")
        sources = self.root / "sources"
        sources.mkdir(exist_ok=True)
        manifest = json.loads((ROOT / "third_party/macos-native-sources.json").read_text())
        for entry in manifest["sources"]:
            fetch_source(entry, sources)
            archive = sources / entry["filename"]
            # Recheck before extraction even when resuming a previous build.
            digest = hashlib.file_digest(archive.open("rb"), "sha256").hexdigest()
            if digest != entry["sha256"]:
                raise ValueError(f"Wrong source hash: {archive}")
            if not (self.work / entry["directory"]).exists():
                with tarfile.open(archive) as stream:
                    stream.extractall(self.work, filter="data")

    def cmake(self, name, directory, options):
        build = self.work / (name + "-build")
        self.run([self.tools / "cmake", "-S", self.work / directory, "-B", build,
                  "-G", "Ninja", "-DCMAKE_BUILD_TYPE=Release",
                  f"-DCMAKE_INSTALL_PREFIX={self.prefix}", "-DCMAKE_OSX_DEPLOYMENT_TARGET=13.0",
                  "-DCMAKE_OSX_ARCHITECTURES=arm64", f"-DCMAKE_OSX_SYSROOT={self.sdk}",
                  f"-DCMAKE_CXX_FLAGS={self.env['CXXFLAGS']}", "-DCMAKE_POLICY_VERSION_MINIMUM=3.5",
                  f"-DCMAKE_PREFIX_PATH={self.prefix}",
                  "-DCMAKE_IGNORE_PREFIX_PATH=/opt/homebrew;/usr/local", *options],
                 self.work, name + "-configure")
        self.run([self.tools / "cmake", "--build", build, "--parallel", self.jobs], self.work, name + "-build")
        self.run([self.tools / "cmake", "--install", build], self.work, name + "-install")

    def relocate_prefix(self):
        # Make dependencies in the private prefix discoverable during inspection
        # and after delocate copies them into the wheel's .dylibs directory.
        for library in (self.prefix / "lib").glob("*.dylib"):
            if library.is_symlink():
                continue
            # Resolve every private dependency explicitly before delocate. An
            # @rpath basename can otherwise pick up an unrelated /usr/local lib.
            links = subprocess.check_output(["otool", "-L", str(library)], text=True)
            for line in links.splitlines()[2:]:
                dependency = line.strip().split(" (", 1)[0]
                if dependency.startswith("@rpath/"):
                    local = self.prefix / "lib" / Path(dependency).name
                    if not local.is_file():
                        raise RuntimeError(f"Unresolved private dependency: {dependency}")
                    self.run(["install_name_tool", "-change", dependency, local.resolve(), library],
                             self.root, library.name + "-dependency")
            info = subprocess.check_output(["otool", "-l", str(library)], text=True)
            if "path @loader_path " not in info:
                self.run(["install_name_tool", "-add_rpath", "@loader_path", library],
                         self.root, library.name + "-rpath")
            self.run(["codesign", "--force", "--sign", "-", library],
                     self.root, library.name + "-adhoc-sign")

    def images(self):
        self.cmake("zlib", "zlib-1.3.2", ["-DZLIB_BUILD_TESTING=OFF"])
        self.cmake("jpeg", "libjpeg-turbo-3.2.0", ["-DENABLE_SHARED=ON", "-DENABLE_STATIC=OFF",
                   "-DWITH_JPEG8=ON", "-DWITH_TURBOJPEG=OFF", "-DWITH_TOOLS=OFF", "-DWITH_TESTS=OFF"])
        self.cmake("png", "libpng-1.6.59", ["-DPNG_SHARED=ON", "-DPNG_STATIC=OFF",
                   "-DPNG_TESTS=OFF", "-DPNG_TOOLS=OFF", f"-DZLIB_ROOT={self.prefix}"])
        self.cmake("tiff", "tiff-4.7.2", ["-DBUILD_SHARED_LIBS=ON", "-Dtiff-tools=OFF",
                   "-Dtiff-tests=OFF", "-Dtiff-contrib=OFF", "-Dtiff-docs=OFF", "-Djpeg=ON",
                   "-Dzlib=ON", "-Dwebp=OFF", "-Djbig=OFF", "-Dzstd=OFF", "-Dlzma=OFF",
                   "-Dlerc=OFF", "-Djxl=OFF"])

    def ffmpeg(self):
        source = self.work / "ffmpeg-8.1.3"
        self.run([source / "configure", f"--prefix={self.prefix}", "--enable-shared",
                  "--disable-static", "--disable-autodetect", "--disable-gpl", "--disable-nonfree",
                  "--disable-network", "--disable-programs", "--disable-doc", "--disable-debug",
                  "--disable-avdevice", "--disable-avfilter", "--enable-pthreads",
                  "--enable-videotoolbox", "--enable-audiotoolbox", "--enable-zlib",
                  f"--extra-cflags=-I{self.prefix}/include -mmacosx-version-min=13.0",
                  f"--extra-ldflags=-L{self.prefix}/lib -mmacosx-version-min=13.0",
                  "--install-name-dir=@rpath"], source, "ffmpeg-configure")
        self.run(["/usr/bin/make", "-j" + self.jobs], source, "ffmpeg-build")
        self.run(["/usr/bin/make", "install"], source, "ffmpeg-install")

    def openssl(self):
        source = self.work / "openssl-3.6.5"
        self.run(["/usr/bin/perl", source / "Configure", "darwin64-arm64-cc",
                  f"--prefix={self.prefix}", f"--openssldir={self.prefix}/ssl",
                  "shared", "no-tests", "-mmacosx-version-min=13.0"], source, "openssl-configure")
        self.run(["/usr/bin/make", "-j" + self.jobs], source, "openssl-build")
        self.run(["/usr/bin/make", "install_sw"], source, "openssl-install")

    def python(self):
        source = self.work / "Python-3.12.15"
        self.env.update(CPPFLAGS=f"-I{self.prefix}/include",
                        LDFLAGS=f"-L{self.prefix}/lib -Wl,-rpath,{self.prefix}/lib")
        self.run([source / "configure", f"--prefix={self.root}/python", "--enable-shared",
                  f"--with-openssl={self.prefix}", "--with-openssl-rpath=auto",
                  "--with-ensurepip=install"], source, "python-configure")
        self.run(["/usr/bin/make", "-j" + self.jobs], source, "python-build")
        self.run(["/usr/bin/make", "install"], source, "python-install")

    def opencv(self):
        source = self.work / "opencv_python-5.0.0.93"
        # Upstream's full-wheel typing aliases require omitted optional modules.
        # Do not advertise partial stubs; runtime bindings are unaffected.
        if "OPENCV_PYTHON_SKIP_TYPING_STUBS" not in (source / "setup.py").read_text():
            self.run(["patch", "-p1", "--input", ROOT / "third_party/patches/opencv-5.0-minimal-typing.patch"],
                     source, "opencv-typing-patch")
        self.env.update(CMAKE_GENERATOR="Ninja", OPENCV_PYTHON_SKIP_GIT_COMMANDS="1",
                        OPENCV_PYTHON_SKIP_TYPING_STUBS="1")
        options = ["-DCMAKE_POLICY_VERSION_MINIMUM=3.5", f"-DCMAKE_PREFIX_PATH={self.prefix}",
                   "-DCMAKE_IGNORE_PREFIX_PATH=/opt/homebrew;/usr/local",
                   "-DCMAKE_OSX_DEPLOYMENT_TARGET=13.0", "-DCMAKE_OSX_ARCHITECTURES=arm64",
                   f"-DCMAKE_OSX_SYSROOT={self.sdk}", f"-DCMAKE_CXX_FLAGS={self.env['CXXFLAGS']}",
                   "-DBUILD_LIST=core,imgproc,imgcodecs,videoio,highgui,python3",
                   "-DWITH_FFMPEG=ON", "-DOPENCV_FFMPEG_ENABLE_LIBAVDEVICE=OFF",
                   "-DWITH_AVFOUNDATION=ON", "-DWITH_QT=OFF", "-DWITH_GSTREAMER=OFF",
                   "-DWITH_OBSENSOR=OFF", "-DWITH_OPENEXR=OFF", "-DWITH_AVIF=OFF",
                   "-DWITH_WEBP=OFF", "-DWITH_OPENJPEG=OFF", "-DWITH_JASPER=OFF",
                   "-DWITH_ITT=OFF", "-DWITH_PROTOBUF=OFF", "-DBUILD_JPEG=OFF",
                   "-DBUILD_PNG=OFF", "-DBUILD_TIFF=OFF", "-DBUILD_ZLIB=OFF",
                   f"-DZLIB_ROOT={self.prefix}"]
        for package, header, library in [("PNG", "PNG_INCLUDE_DIR", "libpng16"),
                                          ("JPEG", "INCLUDE_DIR", "libjpeg"),
                                          ("TIFF", "INCLUDE_DIR", "libtiff")]:
            options += [f"-D{package}_{header}={self.prefix}/include",
                        f"-D{package}_LIBRARY={self.prefix}/lib/{library}.dylib"]
        self.env["CMAKE_ARGS"] = shlex.join(options)
        self.run([self.tools / "python", "setup.py", "bdist_wheel", "--dist-dir", self.root / "wheels-raw"],
                 self.work / "opencv_python-5.0.0.93", "opencv-build")

    def wheel(self):
        wheels = list((self.root / "wheels-raw").glob("opencv_python-*.whl"))
        if len(wheels) != 1:
            raise ValueError("Expected exactly one freshly built OpenCV wheel")
        self.run([self.tools / "delocate-wheel", "--require-archs=arm64", "-w", self.root / "wheels",
                  wheels[0]], self.root, "wheel-delocate")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    stages = ("bootstrap", "images", "ffmpeg", "openssl", "python", "opencv", "wheel")
    parser.add_argument("stage", choices=(*stages, "all"))
    parser.add_argument("--work-root", type=Path, default=ROOT / "build/native-security")
    parser.add_argument("--jobs", type=int, default=4)
    args = parser.parse_args()
    if sys.platform != "darwin" or platform.machine() != "arm64":
        parser.error("This recipe covers macOS arm64 only")
    if sys.version_info[:2] != (3, 12) or args.jobs < 1:
        parser.error("Run the bootstrap with Python 3.12 and a positive job count")
    builder = Builder(args.work_root, args.jobs)
    try:
        for stage in stages if args.stage == "all" else (args.stage,):
            getattr(builder, stage)()
            if stage in ("images", "ffmpeg", "openssl"):
                builder.relocate_prefix()
    finally:
        (builder.root / ("commands-" + args.stage + ".json")).write_text(json.dumps(builder.commands, indent=2) + "\n")


if __name__ == "__main__":
    main()
