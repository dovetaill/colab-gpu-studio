"""Assemble completed VACE batch segments, reusing successful retry jobs."""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path

from ..common import APIError
from ..config import settings
from ..storage import asset_path
from ..video_assets import VideoIngest, _run_media, inspect_video

_MERGE_SLOTS = threading.BoundedSemaphore(1)


@contextmanager
def _merge_slot():
    if not _MERGE_SLOTS.acquire(blocking=False):
        raise APIError("VIDEO_ASSEMBLY_BUSY", "视频合成繁忙，请稍后重试", 429, True)
    try:
        yield
    finally:
        _MERGE_SLOTS.release()


def plan_segments(start: float, end: float, fps: float) -> list[dict]:
    if not all(math.isfinite(x) for x in (start, end, fps)) or start < 0 or end <= start or not 8 <= fps <= 24:
        raise APIError("VIDEO_RANGE_INVALID", "片段范围或帧率无效", 422)
    exact = (end - start) * fps
    total = round(exact)
    if abs(exact - total) > 0.001:
        raise APIError("VIDEO_RANGE_INVALID", "范围必须对齐到完整视频帧", 422)
    counts = [n for n in range(1, 21) if 17 * n <= total <= 81 * n and (total - n) % 4 == 0]
    if not counts:
        raise APIError("VIDEO_SEGMENT_LIMIT", "此范围无法分成 20 段以内的 4n+1 帧片段", 422)
    count = min(counts, key=lambda n: abs(n - total / (3 * fps)))
    units, extra = divmod((total - count) // 4, count)
    offset = 0
    segments = []
    for index in range(count):
        length = 1 + 4 * (units + (index < extra))
        segments.append({"index": index, "start_seconds": start + offset / fps,
                         "end_seconds": start + (offset + length) / fps, "length": length})
        offset += length
    return segments


def _sha(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            result.update(chunk)
    return result.hexdigest()


def _finished_attempt(original: dict, jobs: list[dict]) -> dict:
    queue, seen = [original], set()
    while queue:
        job = queue.pop(0)
        if job["id"] in seen:
            continue
        seen.add(job["id"])
        if job.get("state") == "completed":
            return job
        if job.get("state") in {"failed", "cancelled"}:
            children = [row for row in jobs if (row.get("request_body") or {}).get("retry_of") == job["id"]]
            queue.extend(reversed(children))
    raise APIError("VIDEO_SEGMENT_INCOMPLETE", "有片段未完成，请在任务页重试失败片段", 409)


def assemble_batch(store, repo, owner: str, batch_id: str) -> dict:
    batch = repo.get("batches", batch_id, owner)
    if not batch:
        raise APIError("NOT_FOUND", "批次不存在或不属于当前用户", 404)
    job_ids = batch.get("job_ids") or []
    if not isinstance(job_ids, list) or not 2 <= len(job_ids) <= 20:
        raise APIError("VIDEO_SEGMENT_LIMIT", "批次必须包含 2–20 个视频片段", 422)
    jobs = repo.list("jobs", owner)
    by_id = {job["id"]: job for job in jobs}
    with store.upload_lock("vace-assembly:" + batch_id) as acquired:
        if not acquired:
            raise APIError("VIDEO_ASSEMBLY_BUSY", "该批次正在合成", 409, True)
        specs, outputs, attempts, providers, fingerprints = [], [], [], set(), []
        for job_id in job_ids:
            original = by_id.get(job_id)
            if not original or original.get("batch_id") != batch_id or original.get("workflow_key") != "wan21-vace-v2v":
                raise APIError("VIDEO_SEGMENT_INVALID", "批次包含不属于此工作流的任务", 422)
            attempt = _finished_attempt(original, jobs)
            base, current = original.get("snapshot") or {}, attempt.get("snapshot") or {}
            if (original.get("project_id") != attempt.get("project_id") or
                    original.get("workflow_version") != attempt.get("workflow_version") or
                    base.get("input_video") != current.get("input_video") or
                    base.get("bound_graph") != current.get("bound_graph")):
                raise APIError("VIDEO_SEGMENT_INVALID", "重试片段参数或原视频发生变化", 409)
            spec = base.get("input_video")
            if not isinstance(spec, dict):
                raise APIError("VIDEO_SEGMENT_INVALID", "片段缺少原视频信息", 422)
            ids = attempt.get("output_asset_ids") or []
            if len(ids) != 1:
                raise APIError("VIDEO_SEGMENT_INVALID", "片段输出不存在或数量不符", 409)
            output = store.get("assets", ids[0], owner)
            if (not output or output.get("project_id") != original.get("project_id")
                    or output.get("mime") != "video/mp4" or output.get("source_job_id") != attempt["id"]):
                raise APIError("VIDEO_SEGMENT_INVALID", "片段输出不存在或不属于当前项目", 404)
            path = asset_path(output)
            if not path.is_file() or _sha(path) != output.get("sha256"):
                raise APIError("VIDEO_SEGMENT_INVALID", "片段输出缺失或校验失败", 409)
            specs.append(spec)
            outputs.append((output, path))
            attempts.append(attempt["id"])
            providers.add(attempt.get("provider"))
            params = dict(base.get("resolved_params") or {})
            for key in ("start_seconds", "length", "seed"):
                params.pop(key, None)
            fingerprints.append((base.get("workflow_sha256"), params))
        if len(providers) != 1 or not providers <= {"mock", "colab"}:
            raise APIError("VIDEO_SEGMENT_INVALID", "不能混合模拟和真实视频片段", 422)
        if any(value != fingerprints[0] for value in fingerprints[1:]):
            raise APIError("VIDEO_SEGMENT_INVALID", "片段工作流版本或生成参数不一致", 422)
        first = specs[0]
        for before, after in zip(specs, specs[1:]):
            if any(before.get(k) != after.get(k) for k in ("asset_id", "source_sha256", "fps", "width", "height", "keep_audio", "has_audio")):
                raise APIError("VIDEO_SEGMENT_INVALID", "片段来源或输出设置不一致", 422)
            if abs(before["start_seconds"] + before["duration_seconds"] - after["start_seconds"]) > 0.001:
                raise APIError("VIDEO_SEGMENT_INVALID", "片段时间不连续或顺序不正确", 422)
        source = store.get("assets", first["asset_id"], owner)
        if not source or source.get("project_id") != batch.get("project_id"):
            raise APIError("VIDEO_NOT_FOUND", "原视频不存在或不属于当前项目", 404)
        source_path = asset_path(source)
        if not source_path.is_file() or _sha(source_path) != first["source_sha256"]:
            raise APIError("VIDEO_CHANGED", "原视频缺失或内容已变化", 409)
        fingerprint = hashlib.sha256((batch_id + ":" + ":".join(attempts)).encode()).hexdigest()
        existing = next((row for row in store.list("assets", owner, batch.get("project_id"))
                         if row.get("vace_assembly_key") == fingerprint), None)
        if existing and asset_path(existing).is_file() and _sha(asset_path(existing)) == existing.get("sha256"):
            return existing
        folder = settings.data_dir / "video-staging"
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        with _merge_slot(), tempfile.TemporaryDirectory(prefix="merge-", dir=folder) as directory:
            temp = Path(directory)
            for i, (_, path) in enumerate(outputs):
                os.symlink(path, temp / f"segment-{i:03}.mp4")
            listing = temp / "segments.txt"
            listing.write_text("".join(f"file 'segment-{i:03}.mp4'\n" for i in range(len(outputs))))
            duration = sum(spec["duration_seconds"] for spec in specs)
            target = temp / "assembled.mp4"
            args = ["ffmpeg", "-v", "error", "-nostdin", "-threads", "2", "-filter_threads", "1", "-protocol_whitelist", "file,pipe",
                    "-f", "concat", "-safe", "1", "-i", str(listing)]
            if first["keep_audio"] and first["has_audio"]:
                args += ["-ss", str(first["start_seconds"]), "-i", str(source_path), "-map", "0:v:0", "-map", "1:a:0",
                         "-c:a", "aac"]
            else:
                args += ["-map", "0:v:0", "-an"]
            args += ["-t", str(duration), "-vf", f"fps={first['fps']},scale={first['width']}:{first['height']},setsar=1",
                     "-c:v", "libx264", "-threads", "2", "-pix_fmt", "yuv420p", "-fs",
                     str(settings.max_video_upload_bytes + 1), "-movflags", "+faststart", "-y", str(target)]
            try:
                _run_media(args, timeout=300)
            except APIError as exc:
                raise APIError("VIDEO_ASSEMBLY_FAILED", "拼接失败，原视频和已完成片段均已保留", 422) from exc
            probe = _run_media(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames", "-show_entries",
                                "stream=nb_read_frames", "-of", "json", str(target)], timeout=60)
            try:
                frame_count = int(json.loads(probe.stdout)["streams"][0]["nb_read_frames"])
            except (KeyError, ValueError, IndexError, TypeError) as exc:
                raise APIError("VIDEO_ASSEMBLY_FAILED", "无法核对拼接结果帧数", 422) from exc
            if frame_count != sum(item["length"] for item in specs):
                raise APIError("VIDEO_ASSEMBLY_FAILED", "拼接结果的帧数不符", 422)
            meta = inspect_video(target, "video/mp4")
            if (abs(meta["duration_seconds"] - duration) > max(0.15, 2 / first["fps"])
                    or abs(meta["fps"] - first["fps"]) > 0.01
                    or meta["width"] != first["width"] or meta["height"] != first["height"]
                    or meta["has_audio"] != bool(first["keep_audio"] and first["has_audio"])):
                raise APIError("VIDEO_ASSEMBLY_FAILED", "拼接结果时间轴或音轨校验失败", 422)
            extra = {"source_job_id": attempts[0], "vace_assembly_key": fingerprint, "vace_batch_id": batch_id,
                     "vace_segment_jobs": attempts, "mock": providers == {"mock"},
                     "resolved_params": (by_id[job_ids[0]].get("snapshot") or {}).get("resolved_params", {})}
            with VideoIngest("wan-vace-assembled.mp4") as ingest, target.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    ingest.write(chunk)
                asset = ingest.finish(store, owner, batch["project_id"], extra)
        return asset
