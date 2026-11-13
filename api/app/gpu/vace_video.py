"""Validated short video clips for the reviewed Wan VACE workflow."""

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path

from ..common import APIError
from ..config import settings
from ..storage import asset_path
from ..video_assets import _run_media, inspect_video


def resolve_video_input(own_asset, project_id: str, manifest: dict, params: dict) -> tuple[dict, dict | None]:
    if manifest.get("workflow_key") != "wan21-vace-v2v":
        return dict(params), None
    asset_id = params.get("video_asset_id")
    if not isinstance(asset_id, str) or not asset_id:
        raise APIError("VIDEO_NOT_FOUND", "请选择原视频素材", 422)
    asset = own_asset(asset_id)
    if asset.get("project_id") != project_id:
        raise APIError("ASSET_PROJECT_MISMATCH", "视频素材不属于当前项目", 404)
    if asset.get("mime") not in {"video/mp4", "video/webm"}:
        raise APIError("VIDEO_FORMAT_UNSUPPORTED", "原视频必须是 MP4 或 WebM", 422)
    path = asset_path(asset)
    if not path.is_file():
        raise APIError("VIDEO_NOT_FOUND", "原视频文件不存在", 404)
    metadata = asset.get("video_metadata") or inspect_video(path, asset["mime"])
    start, fps = float(params["start_seconds"]), float(params["fps"])
    length, width, height = params["length"], params["width"], params["height"]
    if length % 4 != 1 or width % 16 or height % 16 or width * height > 832 * 480:
        raise APIError("VIDEO_PARAMETER_INVALID", "帧数必须为 4n+1，宽高为 16 的倍数且像素不超过 832×480", 422)
    duration = length / fps
    if start < 0 or start + duration > float(metadata["duration_seconds"]) + 0.001:
        raise APIError("VIDEO_RANGE_INVALID", "片段超出原视频时长，请调整起点、帧数或帧率", 422)
    descriptor = {
        "asset_id": asset_id, "source_sha256": asset.get("sha256"), "start_seconds": start,
        "duration_seconds": duration, "length": length, "fps": fps, "width": width, "height": height,
        "keep_audio": params["keep_audio"], "has_audio": bool(metadata.get("has_audio")),
    }
    resolved = dict(params)
    resolved["video_asset_id"] = "input_video.mp4"
    return resolved, descriptor


def stage_video(job: dict, store, repo) -> dict:
    """Before GPU allocation: recheck source, make a bounded CFR clip and upload it privately."""
    from .drive import DriveStorage

    spec = (job.get("snapshot") or {}).get("input_video")
    if not isinstance(spec, dict):
        raise APIError("VIDEO_NOT_FOUND", "任务缺少视频输入", 422)
    asset = store.get("assets", spec["asset_id"], job["owner_id"])
    if not asset or asset.get("project_id") != job.get("project_id"):
        raise APIError("VIDEO_NOT_FOUND", "视频已删除或不属于当前项目", 404)
    path = asset_path(asset)
    if not path.is_file():
        raise APIError("VIDEO_NOT_FOUND", "原视频文件不存在", 404)
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    if digest.hexdigest() != spec["source_sha256"]:
        raise APIError("VIDEO_CHANGED", "原视频内容已变化，请重新提交任务", 409)
    staging = settings.data_dir / "video-staging"
    staging.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix="vace-", dir=staging) as temp:
        clip = Path(temp) / "input_video.mp4"
        fps, width, height = spec["fps"], spec["width"], spec["height"]
        filters = (f"fps={fps},scale={width}:{height}:force_original_aspect_ratio=decrease,"
                   f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1")
        args = ["ffmpeg", "-v", "error", "-nostdin", "-threads", "2", "-filter_threads", "1",
                "-protocol_whitelist", "file,pipe", "-format_whitelist", "mov,matroska,webm",
                "-ss", str(spec["start_seconds"]), "-i", str(path),
                "-t", str(spec["duration_seconds"]), "-map", "0:v:0"]
        if spec["keep_audio"] and spec["has_audio"]:
            args += ["-map", "0:a:0", "-c:a", "aac"]
        else:
            args += ["-an"]
        args += ["-vf", filters, "-frames:v", str(spec["length"]), "-c:v", "libx264", "-pix_fmt", "yuv420p",
                 "-movflags", "+faststart", "-y", str(clip)]
        _run_media(args, timeout=120)
        result = _run_media(["ffprobe", "-v", "error", "-protocol_whitelist", "file,pipe", "-select_streams", "v:0",
                             "-count_frames", "-show_entries", "stream=nb_read_frames", "-of",
                             "default=noprint_wrappers=1:nokey=1", str(clip)], timeout=30)
        try:
            frames = int(result.stdout.strip())
        except ValueError as exc:
            raise APIError("VIDEO_DECODE_FAILED", "无法核对片段帧数", 422) from exc
        if frames != spec["length"]:
            raise APIError("VIDEO_DECODE_FAILED", "解码后片段帧数不足，请选择其他片段", 422)
        provider = job.get("provider") or (job.get("execution_policy") or {}).get("provider", "mock")
        if provider == "mock":
            # The mock executor does not receive real media or contact Drive.  Still
            # exercise decoding, range validation, and the full local staging step.
            digest = hashlib.sha256()
            with clip.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
            uploaded = {"id": None, "sha256": digest.hexdigest(), "size": clip.stat().st_size,
                        "verified": True, "mock": True}
        else:
            storage = DriveStorage(job, repo)
            try:
                uploaded = storage.put_file(f"inputs/{job['id']}/input_video.mp4", clip, "video/mp4")
            finally:
                storage.close()
    if uploaded.get("verified") is not True:
        raise APIError("DRIVE_UPLOAD_FAILED", "视频片段上传校验失败", 502, True)
    return {"id": uploaded["id"], "sha256": uploaded["sha256"], "size": uploaded["size"],
            "mock": uploaded.get("mock", False),
            "keep_audio": spec["keep_audio"], "has_audio": spec["has_audio"],
            "duration_seconds": spec["duration_seconds"]}
