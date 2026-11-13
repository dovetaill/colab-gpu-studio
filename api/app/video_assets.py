"""Private video ingestion. Media is streamed and inspected before it becomes an asset."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import tempfile
import threading
import time
import uuid
from fractions import Fraction
from pathlib import Path

import httpx

from .common import APIError
from .config import settings
from .gpu.google import GoogleError, GoogleService
from .gpu.repository import Repository
from .storage import asset_path

_VIDEO_SLOTS = threading.BoundedSemaphore(2)
_DRIVE_ID = re.compile(r"^[A-Za-z0-9_-]{10,200}$")
_VIDEO_TYPES = {".mp4": "video/mp4", ".webm": "video/webm"}


def _run_media(args: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(args, capture_output=True, timeout=timeout, check=False)
    except FileNotFoundError as exc:
        raise APIError("VIDEO_TOOLS_UNAVAILABLE", "服务器缺少 FFmpeg/ffprobe", 503) from exc
    except subprocess.TimeoutExpired as exc:
        raise APIError("VIDEO_PROBE_TIMEOUT", "视频检查超时", 422) from exc
    if result.returncode:
        raise APIError("VIDEO_INVALID", "视频损坏、无法解码或格式不受支持", 422)
    return result


def _fraction(value: str | None) -> float:
    try:
        result = float(Fraction(value or "0"))
    except (ValueError, ZeroDivisionError, OverflowError):
        return 0.0
    return result if math.isfinite(result) else 0.0


def inspect_video(path: Path, mime: str) -> dict:
    """Check container, streams and first decoded frame; never follow network protocols."""
    with path.open("rb") as source:
        header = source.read(16)
    if mime == "video/mp4" and header[4:8] != b"ftyp":
        raise APIError("VIDEO_INVALID", "MP4 扩展名与文件内容不符", 422)
    if mime == "video/webm" and not header.startswith(b"\x1a\x45\xdf\xa3"):
        raise APIError("VIDEO_INVALID", "WebM 扩展名与文件内容不符", 422)
    common = ["-v", "error", "-protocol_whitelist", "file,pipe", "-format_whitelist", "mov,matroska,webm"]
    result = _run_media([
        "ffprobe", *common, "-show_entries",
        "format=format_name,duration,size:stream=codec_type,codec_name,width,height,avg_frame_rate,"
        "r_frame_rate,time_base:stream_tags=rotate:stream_side_data=rotation", "-of", "json", str(path),
    ])
    try:
        value = json.loads(result.stdout)
        fmt = value["format"]
        streams = value["streams"]
        video = next(s for s in streams if s.get("codec_type") == "video")
        duration = float(fmt["duration"])
        width, height = int(video["width"]), int(video["height"])
        fps = _fraction(video.get("avg_frame_rate")) or _fraction(video.get("r_frame_rate"))
    except (KeyError, ValueError, TypeError, StopIteration, json.JSONDecodeError) as exc:
        raise APIError("VIDEO_INVALID", "无法读取视频元数据", 422) from exc
    containers = str(fmt.get("format_name", "")).split(",")
    if (mime == "video/mp4" and "mp4" not in containers) or (
        mime == "video/webm" and "webm" not in containers
    ):
        raise APIError("VIDEO_INVALID", "视频容器与扩展名不一致", 422)
    if not math.isfinite(duration) or duration <= 0 or duration > settings.max_video_duration_seconds:
        raise APIError("VIDEO_DURATION_LIMIT", "视频时长无效或超过限制", 422)
    if width < 1 or height < 1 or max(width, height) > settings.max_video_dimension:
        raise APIError("VIDEO_RESOLUTION_LIMIT", "视频分辨率无效或超过限制", 422)
    if fps <= 0 or fps > 120 or duration * fps > 216000:
        raise APIError("VIDEO_FRAME_LIMIT", "视频帧率或估算帧数超过限制", 422)
    _run_media(["ffmpeg", *common, "-i", str(path), "-map", "0:v:0", "-frames:v", "1", "-f", "null", "-"])
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    rotation = (video.get("tags") or {}).get("rotate")
    for side in video.get("side_data_list") or []:
        if "rotation" in side:
            rotation = side["rotation"]
    return {
        "duration_seconds": duration, "container": fmt.get("format_name"),
        "video_codec": video.get("codec_name"), "width": width, "height": height,
        "fps": fps, "fps_fraction": video.get("avg_frame_rate"), "time_base": video.get("time_base"),
        "has_audio": audio is not None, "audio_codec": audio.get("codec_name") if audio else None,
        "rotation": rotation,
    }


class VideoIngest:
    """Bound concurrent writes and remove every incomplete staging file on failure."""

    def __init__(self, filename: str):
        suffix = Path(filename).suffix.lower()
        if suffix not in _VIDEO_TYPES:
            raise APIError("VIDEO_FORMAT_UNSUPPORTED", "仅支持 MP4 或 WebM 视频", 422)
        self.mime = _VIDEO_TYPES[suffix]
        self.name = re.sub(r"[^\w.()\-\u4e00-\u9fff ]+", "_", Path(filename).name).strip(" .")[:140]
        self.name = self.name or "video" + suffix
        self.size = 0
        self.digest = hashlib.sha256()
        self.path: Path | None = None
        self.file = None

    def __enter__(self):
        if not _VIDEO_SLOTS.acquire(blocking=False):
            raise APIError("VIDEO_UPLOAD_BUSY", "视频处理繁忙，请稍后重试", 429, True)
        try:
            folder = settings.data_dir / "video-staging"
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd, name = tempfile.mkstemp(prefix="upload-", suffix=".part", dir=folder)
            self.path = Path(name)
            self.file = os.fdopen(fd, "wb")
            return self
        except BaseException:
            _VIDEO_SLOTS.release()
            raise

    def write(self, chunk: bytes):
        self.size += len(chunk)
        if self.size > settings.max_video_upload_bytes:
            raise APIError("PAYLOAD_TOO_LARGE", "视频超过大小限制", 413)
        self.digest.update(chunk)
        self.file.write(chunk)

    def finish(self, store, owner: str, project: str, extra_metadata: dict | None = None) -> dict:
        if not self.size:
            raise APIError("VIDEO_INVALID", "视频文件为空", 422)
        self.file.close()
        metadata = inspect_video(self.path, self.mime)
        key = f"{owner}/{project}/{uuid.uuid4().hex}-{self.name}"
        row = {
            "filename": self.name, "mime": self.mime, "storage_key": key, "size": self.size,
            "sha256": self.digest.hexdigest(), "width": metadata["width"], "height": metadata["height"],
            "video_metadata": metadata, "favorite": False, "source_job_id": None,
        }
        if extra_metadata:
            for key in ("source_job_id", "vace_assembly_key", "vace_batch_id", "vace_segment_jobs", "resolved_params", "mock"):
                if key in extra_metadata:
                    row[key] = extra_metadata[key]
        target = asset_path(row)
        target.parent.mkdir(parents=True, exist_ok=True)
        self.path.replace(target)
        try:
            return store.create("assets", owner, row, project_id=project)
        except BaseException:
            target.unlink(missing_ok=True)
            raise

    def __exit__(self, *_):
        if self.file and not self.file.closed:
            self.file.close()
        if self.path:
            self.path.unlink(missing_ok=True)
        _VIDEO_SLOTS.release()


def import_drive_video(store, owner: str, project: str, file_id: str, account_id: str | None = None) -> dict:
    """Import an owned/selected Drive file; URLs, redirects and shared links are never fetched."""
    if not isinstance(file_id, str) or not _DRIVE_ID.fullmatch(file_id):
        raise APIError("DRIVE_FILE_ID_INVALID", "请输入有效的 Google Drive File ID", 422)
    repo = Repository(store)
    if account_id:
        if not repo.get("accounts", account_id, owner):
            raise APIError("NOT_FOUND", "Google 账号不存在或不属于当前用户", 404)
        from .gpu.accounts import AccountService

        account_id = AccountService(repo, owner).resolve_drive_account_id(account_id)
    else:
        credentials = [row for row in repo.list("credentials", owner)
                       if row.get("purpose") == "drive" and not row.get("revoked")]
        if len(credentials) != 1:
            raise APIError("DRIVE_ACCOUNT_REQUIRED", "请选择一个已连接的 Drive 账号", 422)
        account_id = credentials[0].get("account_id")
    try:
        google = GoogleService(repo, owner, account_id=account_id)
        try:
            token = google.access_token("drive")
        finally:
            google.close()
        headers = {"Authorization": "Bearer " + token}
        url = "https://www.googleapis.com/drive/v3/files/" + file_id
        with httpx.Client(timeout=httpx.Timeout(30, connect=10), follow_redirects=False) as client:
            response = client.get(url, params={"fields": "id,name,mimeType,size,capabilities(canDownload)"}, headers=headers)
            _drive_status(response.status_code)
            data = response.json()
            if data.get("id") != file_id or data.get("mimeType") not in _VIDEO_TYPES.values():
                raise APIError("VIDEO_FORMAT_UNSUPPORTED", "Drive 文件不是 MP4 或 WebM 视频", 422)
            if (data.get("capabilities") or {}).get("canDownload") is False:
                raise APIError("DRIVE_FORBIDDEN", "当前 Drive 账号无权下载此文件", 403)
            try:
                size = int(data["size"])
            except (KeyError, ValueError, TypeError) as exc:
                raise APIError("DRIVE_METADATA_INVALID", "Drive 未提供有效文件大小", 422) from exc
            if size <= 0 or size > settings.max_video_upload_bytes:
                raise APIError("PAYLOAD_TOO_LARGE", "Drive 视频超过大小限制", 413)
            filename = data.get("name") or ("video.mp4" if data["mimeType"] == "video/mp4" else "video.webm")
            with VideoIngest(filename) as ingest:
                if ingest.mime != data["mimeType"]:
                    raise APIError("VIDEO_FORMAT_UNSUPPORTED", "Drive 文件扩展名与类型不一致", 422)
                start = time.monotonic()
                with client.stream("GET", url, params={"alt": "media"}, headers=headers) as media:
                    _drive_status(media.status_code)
                    for chunk in media.iter_bytes(1024 * 1024):
                        if time.monotonic() - start > 1800:
                            raise APIError("DRIVE_TIMEOUT", "Drive 下载超时", 504, True)
                        if ingest.size + len(chunk) > size:
                            raise APIError("DRIVE_DOWNLOAD_INCOMPLETE", "Drive 响应大小超过文件元数据", 502)
                        ingest.write(chunk)
                if ingest.size != size:
                    raise APIError("DRIVE_DOWNLOAD_INCOMPLETE", "Drive 文件下载不完整", 502, True)
                return ingest.finish(store, owner, project)
    except GoogleError as exc:
        raise APIError("DRIVE_AUTH_FAILED", "Drive 授权无效或已过期，请重新连接", 409) from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise APIError("DRIVE_UNAVAILABLE", "Drive 暂时不可用或返回无效响应", 502, True) from exc


def _drive_status(status: int):
    if status in (403, 404):
        raise APIError("DRIVE_FILE_INACCESSIBLE", f"Drive 文件不可访问（HTTP {status}）；请检查授权或使用本机上传", status)
    if status == 401:
        raise APIError("DRIVE_AUTH_FAILED", "Drive 授权已过期，请重新连接", 409)
    if status != 200:
        raise APIError("DRIVE_UNAVAILABLE", f"Drive 请求失败（HTTP {status}）", 502, True)
