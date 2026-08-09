"""Image metadata capture, propagation, and FFmpeg metadata arguments."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from PIL import ExifTags, Image


SIDECAR_SUFFIX = ".metadata.json"
_BINARY_INFO_KEYS = {"exif", "icc_profile", "xmp", "photoshop", "iptc", "adobe"}


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        return {"encoding": "base64", "data": base64.b64encode(value).decode("ascii")}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return str(value)


def _tag_name(tag: int, gps: bool = False) -> str:
    table = ExifTags.GPSTAGS if gps else ExifTags.TAGS
    return table.get(tag, str(tag))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _rational(value: Any) -> float | None:
    try:
        if isinstance(value, (tuple, list)) and len(value) == 2:
            return float(value[0]) / float(value[1]) if float(value[1]) else None
        return float(value)
    except (TypeError, ValueError, ZeroDivisionError, OverflowError):
        return None


def _gps_coordinate(value: Any, ref: Any) -> float | None:
    if not isinstance(value, (tuple, list)):
        return None
    parts = [_rational(item) for item in value]
    if any(part is None for part in parts):
        return None
    result = parts[0] + parts[1] / 60 + parts[2] / 3600
    if str(ref).upper() in {"S", "W"}:
        result = -result
    return round(result, 8)


def _inspect_image(path: Path, include_raw: bool = True) -> dict[str, Any]:
    stat = path.stat()
    info: dict[str, Any] = {
        "filename": path.name,
        "suffix": path.suffix.lower(),
        "size_bytes": stat.st_size,
        "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
        "sha256": _sha256(path),
    }
    try:
        with Image.open(path) as image:
            info.update(
                {
                    "format": image.format,
                    "mode": image.mode,
                    "width": image.width,
                    "height": image.height,
                    "frames": getattr(image, "n_frames", 1),
                }
            )
            exif = image.getexif()
            exif_data = {
                _tag_name(tag): _json_value(value)
                for tag, value in exif.items()
            }
            gps_data: dict[str, Any] = {}
            try:
                gps_ifd = exif.get_ifd(ExifTags.IFD.GPSInfo)
                gps_data = {
                    _tag_name(tag, gps=True): _json_value(value)
                    for tag, value in gps_ifd.items()
                }
            except (AttributeError, KeyError, TypeError, ValueError):
                pass

            capture_time = exif_data.get("DateTimeOriginal") or exif_data.get("DateTimeDigitized")
            gps_lat = _gps_coordinate(
                gps_data.get("GPSLatitude"), gps_data.get("GPSLatitudeRef")
            )
            gps_lon = _gps_coordinate(
                gps_data.get("GPSLongitude"), gps_data.get("GPSLongitudeRef")
            )
            info["exif"] = exif_data
            info["gps"] = gps_data
            info["capture"] = {
                "datetime": capture_time,
                "timezone": exif_data.get("OffsetTimeOriginal") or exif_data.get("OffsetTime"),
                "latitude": gps_lat,
                "longitude": gps_lon,
                "altitude": _rational(gps_data.get("GPSAltitude")),
                "altitude_ref": gps_data.get("GPSAltitudeRef"),
                "gps_timestamp": gps_data.get("GPSTimeStamp"),
            }
            info["camera"] = {
                "make": exif_data.get("Make"),
                "model": exif_data.get("Model"),
                "lens_model": exif_data.get("LensModel"),
                "lens_make": exif_data.get("LensMake"),
                "camera_serial": exif_data.get("BodySerialNumber"),
                "lens_serial": exif_data.get("LensSerialNumber"),
            }
            info["shooting"] = {
                "exposure_time": _json_value(exif_data.get("ExposureTime")),
                "f_number": _json_value(exif_data.get("FNumber")),
                "iso": exif_data.get("ISOSpeedRatings") or exif_data.get("PhotographicSensitivity"),
                "focal_length": _json_value(exif_data.get("FocalLength")),
                "flash": exif_data.get("Flash"),
                "metering_mode": exif_data.get("MeteringMode"),
                "exposure_program": exif_data.get("ExposureProgram"),
                "exposure_bias": _json_value(exif_data.get("ExposureBiasValue")),
                "white_balance": exif_data.get("WhiteBalance"),
            }
            info["technical"] = {
                "orientation": exif_data.get("Orientation"),
                "color_space": exif_data.get("ColorSpace"),
                "pixel_x_dimension": exif_data.get("PixelXDimension"),
                "pixel_y_dimension": exif_data.get("PixelYDimension"),
                "resolution_unit": exif_data.get("ResolutionUnit"),
                "x_resolution": _json_value(exif_data.get("XResolution")),
                "y_resolution": _json_value(exif_data.get("YResolution")),
                "bits_per_sample": _json_value(exif_data.get("BitsPerSample")),
                "software": exif_data.get("Software"),
                "scene_type": exif_data.get("SceneType"),
                "sensing_method": exif_data.get("SensingMethod"),
            }
            info["copyright"] = {
                "artist": exif_data.get("Artist") or exif_data.get("XPAuthor"),
                "copyright": exif_data.get("Copyright"),
                "description": exif_data.get("ImageDescription") or exif_data.get("XPTitle"),
                "keywords": exif_data.get("XPKeywords"),
                "rating": exif_data.get("Rating"),
                "creator_tool": exif_data.get("CreatorTool"),
            }
            info["image_info"] = {
                str(key): _json_value(value)
                for key, value in image.info.items()
                if key not in _BINARY_INFO_KEYS
            }
            info_keys = {str(key).lower() for key in image.info}
            exif_keys = {str(key).lower() for key in exif_data}
            info["special"] = {
                "live_photo": bool(
                    {"mp4", "motionphoto", "motion_photo", "microvideo"} & info_keys
                ),
                "panorama": bool(
                    {"projectiontype", "usepanoramaviewer", "gpanorama", "panorama"}
                    & (info_keys | exif_keys)
                ),
                "is_360": bool(
                    {"equirectangular", "360", "fullpano"} & (info_keys | exif_keys)
                ),
                "depth_map": bool(
                    {"depth", "depth_data", "apple-depth", "auxiliary"} & info_keys
                ),
                "hdr": bool(
                    {"gainmap", "hdr", "hdrgm", "hdrgainmap"} & (info_keys | exif_keys)
                ),
                "computational_photography": bool(
                    {"makernote", "computationalphotography", "software"} & exif_keys
                ),
            }
            if include_raw:
                raw = {}
                exif_bytes = image.info.get("exif")
                if not exif_bytes and exif:
                    try:
                        exif_bytes = exif.tobytes()
                    except Exception:
                        exif_bytes = None
                if exif_bytes:
                    raw["exif"] = base64.b64encode(exif_bytes).decode("ascii")
                for key in ("icc_profile", "xmp", "photoshop", "iptc"):
                    value = image.info.get(key)
                    if isinstance(value, bytes):
                        raw[key] = base64.b64encode(value).decode("ascii")
                info["raw"] = raw
    except Exception as exc:
        info["read_error"] = str(exc)
    return info


def _sidecar_path(path: Path) -> Path:
    return path.with_name(path.name + SIDECAR_SUFFIX)


class ImageMetadataStore:
    """Persist complete metadata alongside generated images and expose common tags to FFmpeg."""

    def sidecar_path(self, image_path: Path) -> Path:
        return _sidecar_path(Path(image_path))

    def read(self, image_path: Path) -> dict[str, Any] | None:
        path = self.sidecar_path(Path(image_path))
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return None

    def write(self, image_path: Path, metadata: dict[str, Any]) -> None:
        path = self.sidecar_path(Path(image_path))
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(metadata, stream, ensure_ascii=False, indent=2, default=str)
            Path(temp_name).replace(path)
        finally:
            try:
                Path(temp_name).unlink(missing_ok=True)
            except OSError:
                pass

    def capture(self, image_path: Path, context: dict[str, Any] | None = None) -> dict[str, Any]:
        path = Path(image_path)
        inspected = _inspect_image(path)
        metadata = {
            "schema": "astrbot.image-metadata.v1",
            "captured_at": datetime.now().isoformat(timespec="seconds"),
            "original": inspected,
            "source": {},
            "processing": [],
            "output": None,
        }
        if context:
            self.merge_context(metadata, context)
        self.write(path, metadata)
        return metadata

    def ensure(self, image_path: Path, context: dict[str, Any] | None = None) -> dict[str, Any]:
        path = Path(image_path)
        metadata = self.read(path)
        if metadata is None:
            return self.capture(path, context)
        if context:
            self.merge_context(metadata, context)
            self.write(path, metadata)
        return metadata

    @staticmethod
    def merge_context(metadata: dict[str, Any], context: dict[str, Any]) -> None:
        source = metadata.setdefault("source", {})
        supplied_source = context.get("source")
        if isinstance(supplied_source, dict):
            source.update({key: _json_value(value) for key, value in supplied_source.items() if value is not None})
        for key in (
            "platform", "source_platform", "url", "source_url", "author", "title",
            "image_index", "image_count", "original_image_url", "paired_video_url",
            "live_photo_video_url", "post_id",
        ):
            if context.get(key) is not None:
                target_key = "platform" if key == "source_platform" else "url" if key == "source_url" else key
                source[target_key] = _json_value(context[key])
        for key in ("live_photo", "panorama", "is_360", "depth_map", "hdr", "computational_photography"):
            if context.get(key) is not None:
                metadata[key] = _json_value(context[key])

    def propagate(
        self,
        source_path: Path,
        target_path: Path,
        processing: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        source_metadata = self.ensure(Path(source_path))
        metadata = copy.deepcopy(source_metadata)
        if processing:
            metadata.setdefault("processing", []).append(_json_value(processing))
        self.write(Path(target_path), metadata)
        return metadata

    def finalize(
        self,
        input_path: Path,
        output_path: Path,
        processing: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        metadata = copy.deepcopy(self.ensure(Path(input_path)))
        if processing:
            metadata.setdefault("processing", []).append(_json_value(processing))
        try:
            metadata["output"] = _inspect_image(Path(output_path), include_raw=False)
        except OSError:
            metadata["output"] = {"filename": Path(output_path).name}
        self.write(Path(output_path), metadata)
        return metadata

    @staticmethod
    def _public_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
        def strip_binary(value: Any) -> Any:
            if isinstance(value, dict):
                if value.get("encoding") == "base64" and "data" in value:
                    return {"encoding": "base64", "bytes": len(str(value["data"])) * 3 // 4}
                return {key: strip_binary(item) for key, item in value.items() if key != "raw"}
            if isinstance(value, list):
                return [strip_binary(item) for item in value]
            return value

        return strip_binary(metadata)

    def ffmpeg_args(
        self,
        metadata: dict[str, Any],
        processing: dict[str, Any] | None = None,
    ) -> list[str]:
        public = self._public_metadata(metadata)
        if processing:
            public.setdefault("processing", []).append(_json_value(processing))
        source = metadata.get("source") or {}
        original = metadata.get("original") or {}
        capture = original.get("capture") or {}
        copyright_info = original.get("copyright") or {}
        values = {
            "title": source.get("title") or copyright_info.get("description") or original.get("filename"),
            "artist": source.get("author") or copyright_info.get("artist"),
            "copyright": copyright_info.get("copyright"),
            "description": copyright_info.get("description"),
            "creation_time": capture.get("datetime"),
            "source_platform": source.get("platform"),
            "source_url": source.get("url"),
            "original_filename": original.get("filename"),
            "original_format": original.get("format"),
            "original_width": (original.get("width")),
            "original_height": (original.get("height")),
            "image_index": source.get("image_index"),
            "image_count": source.get("image_count"),
        }
        latitude = capture.get("latitude")
        longitude = capture.get("longitude")
        if latitude is not None and longitude is not None:
            values["location"] = f"{latitude:.8f},{longitude:.8f}"
        payload = json.dumps(public, ensure_ascii=False, separators=(",", ":"), default=str)
        args = ["-map_metadata", "0"]
        for key, value in values.items():
            if value is not None and value != "":
                args.extend(["-metadata", f"{key}={value}"])
        args.extend(["-metadata", f"comment=astrbot-image-metadata:{payload}"])
        return args


__all__ = ["ImageMetadataStore", "SIDECAR_SUFFIX"]
