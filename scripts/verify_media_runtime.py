"""Exercise OpenScope's real image and video output codecs without sonar hardware."""
from pathlib import Path
import argparse
import importlib.util
import json
import os
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


def verify_media(directory):
    import cv2
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont, features
    from live_viewer.recording import make_video_writer

    height, width, count = 96, 128, 12
    y, x = np.indices((height, width))
    frames = [np.stack(((x + i * 9) % 256, (y * 2) % 256,
                        (x + y + i * 5) % 256), axis=-1).astype(np.uint8)
              for i in range(count)]
    results = {"opencv": cv2.__version__, "videos": [], "images": []}
    for suffix in (".avi", ".mkv", ".mp4", ".m4v"):
        target = directory / ("recording" + suffix)
        writer = make_video_writer(target, 12.0, (width, height))
        backend = writer.getBackendName()
        try:
            for frame in frames:
                writer.write(frame)
        finally:
            writer.release()
        reader = cv2.VideoCapture(str(target))
        decoded = []
        try:
            if not reader.isOpened():
                raise RuntimeError(f"Cannot reopen {suffix} recording")
            fourcc = int(reader.get(cv2.CAP_PROP_FOURCC))
            codec = "".join(chr((fourcc >> (8 * i)) & 255) for i in range(4))
            while True:
                ok, frame = reader.read()
                if not ok:
                    break
                decoded.append(frame)
        finally:
            reader.release()
        if len(decoded) != count or any(frame.shape != frames[0].shape for frame in decoded):
            raise RuntimeError(f"Incomplete/wrong-sized {suffix} recording: {len(decoded)} frames")
        lossless = all(np.array_equal(expected, actual) for expected, actual in zip(frames, decoded))
        if suffix in (".avi", ".mkv") and not lossless:
            raise RuntimeError(f"Lossless {suffix} recording was not preserved (codec {codec})")
        results["videos"].append({"extension": suffix, "backend": backend, "codec": codec,
                                  "frames": len(decoded), "lossless": lossless,
                                  "bytes": target.stat().st_size})
    for suffix in (".png", ".jpg"):
        ok, encoded = cv2.imencode(suffix, frames[0])
        if not ok:
            raise RuntimeError(f"Cannot encode {suffix}")
        decoded = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
        if decoded is None or decoded.shape != frames[0].shape:
            raise RuntimeError(f"Cannot decode {suffix}")
        if suffix == ".png" and not np.array_equal(decoded, frames[0]):
            raise RuntimeError("PNG pixels changed during round trip")
        results["images"].append({"extension": suffix, "bytes": int(encoded.size)})
    picture = Image.fromarray(frames[0])
    ImageDraw.Draw(picture).text((4, 4), "OpenScope", font=ImageFont.load_default(), fill="white")
    results["pillow"] = {"tiff": features.version("libtiff"), "freetype": features.version("freetype2")}
    for suffix in (".png", ".jpg", ".tiff"):
        target = directory / ("pillow" + suffix)
        picture.save(target)
        with Image.open(target) as decoded:
            decoded.load()
            if decoded.size != picture.size:
                raise RuntimeError(f"Pillow {suffix} dimensions changed")
            if suffix != ".jpg" and decoded.tobytes() != picture.tobytes():
                raise RuntimeError(f"Pillow {suffix} pixels changed")
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--frozen-internal", type=Path,
                        help="Test the actual frozen cv2 extension using a matching Python/NumPy environment")
    parser.add_argument("--check-gui", action="store_true", help="Also create, draw and close an OpenCV window")
    args = parser.parse_args()
    dll_directories = []
    if args.frozen_internal:
        if sys.platform == "win32":
            dll_directories.append(os.add_dll_directory(str(args.frozen_internal.resolve())))
        candidates = list((args.frozen_internal / "cv2").rglob("cv2*.so"))
        candidates += list((args.frozen_internal / "cv2").rglob("cv2*.pyd"))
        if len(candidates) != 1:
            parser.error(f"Expected one frozen cv2 extension, found {len(candidates)}")
        spec = importlib.util.spec_from_file_location("cv2", candidates[0])
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        sys.modules["cv2"] = module
    with tempfile.TemporaryDirectory(prefix="openscope-media-check-") as temporary:
        result = verify_media(Path(temporary))
    if args.check_gui:
        import cv2
        import numpy as np
        cv2.namedWindow("OpenScope release check", cv2.WINDOW_NORMAL)
        cv2.imshow("OpenScope release check", np.zeros((96, 128, 3), dtype=np.uint8))
        cv2.waitKey(1)
        cv2.destroyAllWindows()
        result["gui"] = "window creation, drawing and cleanup passed"
    if args.frozen_internal and sys.platform == "win32":
        # Prove the frozen extension used packaged FFmpeg DLLs, rather than
        # accidentally succeeding against the build environment's copies.
        import ctypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
        kernel.GetModuleHandleW.restype = ctypes.c_void_p
        kernel.GetModuleFileNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint]
        kernel.GetModuleFileNameW.restype = ctypes.c_uint
        result["frozen_libraries"] = {}
        for pattern in ("avutil-*.dll", "avcodec-*.dll", "avformat-*.dll"):
            library, = list(args.frozen_internal.rglob(pattern))
            handle = kernel.GetModuleHandleW(library.name)
            buffer = ctypes.create_unicode_buffer(32768)
            if not handle or not kernel.GetModuleFileNameW(handle, buffer, len(buffer)):
                raise RuntimeError(f"Packaged FFmpeg library is not loaded: {library.name}")
            actual = Path(buffer.value).resolve()
            if actual != library.resolve():
                raise RuntimeError(f"Loaded build-machine library instead of packaged library: {actual}")
            result["frozen_libraries"][library.name] = str(actual)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, indent=2) + "\n")
    print(f"Verified lossless AVI/MKV, MP4/M4V, JPEG and PNG: {args.report}")
    for directory in dll_directories:
        directory.close()


if __name__ == "__main__":
    main()
