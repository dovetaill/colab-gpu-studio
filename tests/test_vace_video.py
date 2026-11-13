import json
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest
from app.common import APIError
from app.config import settings
from app.gpu.manifest import bind_parameters, resolve_parameters, validate_manifest
from app.gpu.runtime_worker import _mux_vace_audio
from app.gpu.vace_video import resolve_video_input, stage_video
from app.storage import asset_path
from app.video_assets import import_drive_video, inspect_video

from tests.gpu.test_routes_draft import gpu_client  # noqa: F401


@pytest.fixture
def sample_video(tmp_path):
    path = tmp_path / "fixture.mp4"
    subprocess.run([
        "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=16",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "5",
        "-c:v", "libx264", "-threads", "1", "-pix_fmt", "yuv420p", "-c:a", "aac", "-y", str(path),
    ], check=True, timeout=30)
    return path


def bundle():
    path = Path("api/app/gpu/templates/builtin/wan21-vace-v2v-1.0.0.json")
    return json.loads(path.read_text())


def test_upload_is_authenticated_streamed_and_inspected(gpu_client, sample_video):  # noqa: F811
    import time

    from app.security import sign_session

    client, store, user, project, _ = gpu_client
    token = client.headers.pop("Authorization")
    args = {"data": {"project_id": project["id"]}, "files": {"file": ("fixture.mp4", sample_video.read_bytes(), "video/mp4")}}
    assert client.post("/api/assets", **args).status_code == 401
    client.headers["Authorization"] = token
    response = client.post("/api/assets", **args)
    assert response.status_code == 201, response.text
    asset = response.json()["data"]
    meta = asset["video_metadata"]
    assert (meta["width"], meta["height"], meta["has_audio"]) == (320, 240, True)
    assert meta["fps"] == 16 and meta["duration_seconds"] == pytest.approx(5, abs=0.05)
    assert "storage_key" not in asset
    assert client.get(f"/api/assets/{asset['id']}/preview").headers["content-disposition"].startswith("inline")
    assert client.get(f"/api/assets/{asset['id']}/download").content == sample_video.read_bytes()
    assert client.get("/api/assets", params={"project_id": "foreign-project"}).status_code == 404
    assert store.get("assets", asset["id"], "another-owner") is None
    client.headers["Authorization"] = "Bearer " + sign_session({"uid": "another-owner", "role": "admin", "exp": time.time() + 60})
    assert client.get(f"/api/assets/{asset['id']}/download").status_code == 404
    client.headers["Authorization"] = token
    partial = client.get(f"/api/assets/{asset['id']}/preview", headers={"Range": "bytes=0-31"})
    assert partial.status_code == 206 and len(partial.content) == 32


def test_invalid_uploads_and_limits(gpu_client, sample_video, monkeypatch):  # noqa: F811
    client, _, _, project, _ = gpu_client
    def upload(data, name="bad.mp4", mime="video/mp4"):
        return client.post("/api/assets", data={"project_id": project["id"]}, files={"file": (name, data, mime)})
    assert upload(b"not video").status_code == 422
    assert upload(b"aaaaftyp" + b"x" * 30).status_code == 422
    assert upload(sample_video.read_bytes(), mime="image/png").status_code == 422
    monkeypatch.setattr(settings, "max_video_upload_bytes", 128)
    assert upload(sample_video.read_bytes()).status_code == 413
    monkeypatch.setattr(settings, "max_video_upload_bytes", 1024 * 1024)
    monkeypatch.setattr(settings, "max_video_duration_seconds", 2)
    assert upload(sample_video.read_bytes()).json()["error"]["code"] == "VIDEO_DURATION_LIMIT"
    monkeypatch.setattr(settings, "max_video_duration_seconds", 100)
    monkeypatch.setattr(settings, "max_video_dimension", 100)
    assert upload(sample_video.read_bytes()).json()["error"]["code"] == "VIDEO_RESOLUTION_LIMIT"
    assert not list((settings.data_dir / "video-staging").glob("*.part"))


def test_graph_binds_canny_video_and_optional_reference():
    from app.gpu.runtime_worker import _vace_saved_node_outputs

    b = bundle()
    manifest = validate_manifest(b["manifest"], b["api_graph"], b["object_info"])
    params = resolve_parameters(manifest, {"video_asset_id": "input_video.mp4", "length": 49, "seed": 42})
    graph = bind_parameters(manifest, b["api_graph"], params)
    assert graph["2"]["inputs"]["video"] == ["1", 0]
    assert graph["3"]["inputs"]["image"] == ["2", 0]
    assert graph["16"]["inputs"]["control_video"] == ["3", 0]
    assert graph["16"]["inputs"]["length"] == 49
    assert "reference_image" not in graph["16"]["inputs"]
    assert graph["17"]["inputs"]["positive"] == ["16", 0]
    assert graph["17"]["inputs"]["negative"] == ["16", 1]
    assert graph["17"]["inputs"]["latent_image"] == ["16", 2]
    assert graph["18"]["inputs"]["samples"] == ["17", 0]
    assert graph["19"]["inputs"]["samples"] == ["18", 0]
    assert graph["20"]["inputs"]["images"] == ["19", 0]
    assert graph["21"]["inputs"]["video"] == ["20", 0]
    params["reference_image"] = "input_reference_image.jpg"
    graph = bind_parameters(manifest, b["api_graph"], params)
    assert graph["16"]["inputs"]["reference_image"] == ["4", 0]
    history = {"1": {"videos": [{"filename": "input_video.mp4", "type": "input"}]},
               "21": {"videos": [{"filename": "generated.mp4", "type": "output"}]}}
    assert _vace_saved_node_outputs(graph, history) == [history["21"]]


def test_clip_is_trimmed_staged_and_original_audio_is_muxed(gpu_client, sample_video, tmp_path, monkeypatch):  # noqa: F811
    from app.gpu import drive
    from app.gpu.repository import Repository

    client, store, user, project, _ = gpu_client
    result = client.post("/api/assets", data={"project_id": project["id"]},
                         files={"file": ("fixture.mp4", sample_video.read_bytes(), "video/mp4")})
    asset = store.get("assets", result.json()["data"]["id"], user["id"])
    b = bundle()
    params = resolve_parameters(b["manifest"], {"video_asset_id": asset["id"], "start_seconds": 1.0})
    bound, spec = resolve_video_input(lambda _: asset, project["id"], b["manifest"], params)
    assert bound["video_asset_id"] == "input_video.mp4"
    assert spec["duration_seconds"] == pytest.approx(49 / 16)
    saved = tmp_path / "staged.mp4"

    class FakeDrive:
        def __init__(self, *_):
            pass

        def put_file(self, key, source, mime):
            import hashlib
            assert key.startswith("inputs/") and mime == "video/mp4"
            shutil.copyfile(source, saved)
            return {"id": "drivefile12345", "size": saved.stat().st_size,
                    "sha256": hashlib.sha256(saved.read_bytes()).hexdigest(), "verified": True}

        def close(self):
            pass

    monkeypatch.setattr(drive, "DriveStorage", FakeDrive)
    job = {"id": "job1", "owner_id": user["id"], "project_id": project["id"], "provider": "colab",
           "snapshot": {"input_video": spec}}
    staged = stage_video(job, store, Repository(store))
    assert len(staged["sha256"]) == 64
    assert inspect_video(saved, "video/mp4")["has_audio"] is True
    generated = tmp_path / "generated.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-i", str(saved), "-an", "-c:v", "copy", "-y", str(generated)], check=True)
    _mux_vace_audio(generated, saved, spec, 20 * 1024 * 1024)
    meta = inspect_video(generated, "video/mp4")
    assert meta["has_audio"] is True
    assert meta["duration_seconds"] == pytest.approx(49 / 16, abs=0.1)
    spec["keep_audio"] = False
    _mux_vace_audio(generated, saved, spec, 20 * 1024 * 1024)
    assert inspect_video(generated, "video/mp4")["has_audio"] is False


def test_range_project_and_path_are_enforced(gpu_client, sample_video):  # noqa: F811
    client, store, user, project, _ = gpu_client
    result = client.post("/api/assets", data={"project_id": project["id"]},
                         files={"file": ("fixture.mp4", sample_video.read_bytes(), "video/mp4")})
    asset = store.get("assets", result.json()["data"]["id"], user["id"])
    b = bundle()
    params = resolve_parameters(b["manifest"], {"video_asset_id": asset["id"], "start_seconds": 4})
    with pytest.raises(APIError, match="片段超出"):
        resolve_video_input(lambda _: asset, project["id"], b["manifest"], params)
    with pytest.raises(APIError) as err:
        resolve_video_input(lambda _: asset, "other-project", b["manifest"], params)
    assert err.value.code == "ASSET_PROJECT_MISMATCH"
    with pytest.raises(ValueError):
        asset_path({"storage_key": "../../outside"})


@pytest.mark.parametrize("status,code", [(403, "DRIVE_FILE_INACCESSIBLE"), (404, "DRIVE_FILE_INACCESSIBLE"),
                                         (401, "DRIVE_AUTH_FAILED"), (503, "DRIVE_UNAVAILABLE")])
def test_drive_import_errors_are_real_and_safe(gpu_client, monkeypatch, status, code):  # noqa: F811
    from app import video_assets

    _, store, user, project, _ = gpu_client
    repo = video_assets.Repository(store)
    repo.create("credentials", user["id"], {"purpose": "drive"})
    class FakeGoogle:
        def __init__(self, *args, **kwargs):
            pass
        def access_token(self, purpose):
            return "private-test-token"
        def close(self):
            pass
    client_type = httpx.Client
    monkeypatch.setattr(video_assets, "GoogleService", FakeGoogle)
    monkeypatch.setattr(video_assets.httpx, "Client", lambda **kwargs: client_type(
        transport=httpx.MockTransport(lambda request: httpx.Response(status, request=request)), **kwargs))
    with pytest.raises(APIError) as err:
        import_drive_video(store, user["id"], project["id"], "validfileid123")
    assert err.value.code == code
    assert "private-test-token" not in str(err.value)


def test_drive_url_is_not_a_file_id(gpu_client):  # noqa: F811
    _, store, user, project, _ = gpu_client
    with pytest.raises(APIError) as err:
        import_drive_video(store, user["id"], project["id"], "http://127.0.0.1/private")
    assert err.value.code == "DRIVE_FILE_ID_INVALID"


def test_silent_source_and_real_audio_mux_contract(sample_video, tmp_path):
    silent = tmp_path / "silent.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-i", str(sample_video), "-c:v", "copy", "-an", "-y", str(silent)],
                   check=True, timeout=30)
    assert inspect_video(silent, "video/mp4")["has_audio"] is False
    _mux_vace_audio(silent, silent, {"duration_seconds": 3.0625, "length": 49, "fps": 16,
                                   "width": 320, "height": 240, "keep_audio": True, "has_audio": False}, 5000000)
    assert inspect_video(silent, "video/mp4")["has_audio"] is False


def test_mock_e2e_upload_graph_queue_and_gallery(gpu_client, sample_video, monkeypatch):  # noqa: F811
    """The normal mock executor returns synthetic video without Drive or model inference."""
    from app.gpu import drive
    from app.gpu.compute import MockColabProvider, MockGPUExecutor
    from app.gpu.repository import Repository
    from app.gpu.scheduler import Scheduler

    client, store, user, project, _ = gpu_client
    response = client.post("/api/assets", data={"project_id": project["id"]},
                           files={"file": ("fixture.mp4", sample_video.read_bytes(), "video/mp4")})
    asset = response.json()["data"]
    workflows = client.get("/api/gpu/workflows").json()["data"]
    workflow = next(item for item in workflows if item["workflow_key"] == "wan21-vace-v2v")
    repo = Repository(store)
    model = next(item for item in repo.list("models", user["id"]) if item["model_id"] == workflow["model_id"])
    # Test-only activation in a temporary in-memory database; production review remains mandatory.
    repo.update("workflows", workflow["id"], {"state": "published", "active": True}, user["id"])
    repo.update("models", model["id"], {"enabled": True, "is_mock": True}, user["id"])

    class ForbiddenDrive:
        def __init__(self, *_):
            raise AssertionError("mock must not connect to Drive")

    monkeypatch.setattr(drive, "DriveStorage", ForbiddenDrive)
    monkeypatch.setenv("GPU_STORAGE", "drive")
    body = {"project_id": project["id"], "model_id": workflow["model_id"],
            "workflow_key": workflow["workflow_key"], "workflow_version": workflow["workflow_version"],
            "params": {"video_asset_id": asset["id"]}, "asset_ids": [asset["id"]],
            "execution_policy": {"provider": "mock", "shutdown_after_job": True, "max_runtime_minutes": 5}}
    response = client.post("/api/gpu/jobs", json=body, headers={"Idempotency-Key": "vace-mock"})
    assert response.status_code == 202, response.text
    job_id = response.json()["data"]["id"]
    graph = repo.get("jobs", job_id, user["id"])["snapshot"]["bound_graph"]
    assert graph["1"]["inputs"]["file"] == "input_video.mp4"
    assert graph["3"]["inputs"]["image"] == ["2", 0]
    assert graph["16"]["inputs"]["control_video"] == ["3", 0]
    assert client.post("/api/gpu/jobs", json=body, headers={"Idempotency-Key": "vace-mock"}).json()["data"]["id"] == job_id
    scheduler = Scheduler(store, provider_factory=lambda _: MockColabProvider(), executor_factory=lambda _: executor)
    executor = MockGPUExecutor()
    for _ in range(50):
        scheduler.tick()
        job = repo.get("jobs", job_id, user["id"])
        if job["state"] in {"failed", "cancelled", "completed"}:
            break
    assert job["state"] == "completed", job
    assert all(lease["state"] == "deleted" for lease in repo.list("leases", user["id"]))
    result = client.get(f"/api/gpu/jobs/{job_id}/outputs").json()["data"][0]
    assert result["mock"] is True and result["mime"] == "video/mp4"
    assert client.get(f"/api/assets/{result['id']}/preview").status_code == 200
    downloaded = client.get(f"/api/assets/{result['id']}/download").content
    assert downloaded != sample_video.read_bytes() and downloaded[4:8] == b"ftyp"
    assert result["filename"] == "MOCK-vace-test-pattern.mp4"
    assert any(item["id"] == result["id"] for item in client.get("/api/gpu/gallery", params={"project_id": project["id"]}).json()["data"])


def test_vace_gpu_and_node_mismatch_fail_closed(monkeypatch, tmp_path):
    from app.gpu import runtime_worker

    payload = {"snapshot": {"input_video": {"asset_id": "video"},
                            "manifest": {"state": "published", "reviewed": True}, "bound_graph": {}}}
    monkeypatch.setattr(runtime_worker, "_probe_gpu", lambda: {"devices": [{"name": "T4", "memory_mib": 15000}]})
    runtime_worker.run_job(payload, tmp_path)
    status = json.loads((tmp_path / "status.json").read_text())
    assert status["state"] == "failed" and status["error_code"] == "gpu_memory_insufficient"
    monkeypatch.setattr(runtime_worker, "_http_json", lambda *args, **kwargs: {})
    with pytest.raises(runtime_worker.WorkerError, match="COMFY_NODE_MISMATCH"):
        runtime_worker._verify_vace_nodes("http://127.0.0.1:8188", {"1": {"class_type": "Canny", "inputs": {}}})
    monkeypatch.setattr(runtime_worker, "_probe_gpu", lambda: {"devices": [{"name": "L4", "memory_mib": 23034}]})
    monkeypatch.setattr(runtime_worker, "_require_vace_resources", lambda _: None)
    def oom(*args, **kwargs):
        raise runtime_worker.WorkerError("CUDA out of memory")
    monkeypatch.setattr(runtime_worker, "_run_comfy", oom)
    runtime_worker.run_job(payload, tmp_path)
    assert json.loads((tmp_path / "status.json").read_text())["error_code"] == "gpu_out_of_memory"


def test_successful_drive_import_creates_a_studio_asset(gpu_client, sample_video, monkeypatch):  # noqa: F811
    from app import video_assets

    _, store, user, project, _ = gpu_client
    video_assets.Repository(store).create("credentials", user["id"], {"purpose": "drive"})
    data = sample_video.read_bytes()

    class FakeGoogle:
        def __init__(self, *args, **kwargs):
            pass
        def access_token(self, purpose):
            return "temporary-access-token"
        def close(self):
            pass

    def handler(request):
        assert request.url.host == "www.googleapis.com"
        if request.url.params.get("alt") == "media":
            return httpx.Response(200, content=data)
        return httpx.Response(200, json={"id": "validfileid123", "name": "selected.mp4", "size": str(len(data)),
                                       "mimeType": "video/mp4", "capabilities": {"canDownload": True}})

    client_type = httpx.Client
    monkeypatch.setattr(video_assets, "GoogleService", FakeGoogle)
    monkeypatch.setattr(video_assets.httpx, "Client", lambda **kwargs: client_type(transport=httpx.MockTransport(handler), **kwargs))
    asset = import_drive_video(store, user["id"], project["id"], "validfileid123")
    assert asset["id"] != "validfileid123"
    assert asset["project_id"] == project["id"] and asset["owner_id"] == user["id"]
    assert asset_path(asset).read_bytes() == data
    assert "temporary-access-token" not in str(asset)


def test_staged_clip_cleanup_targets_only_this_jobs_verified_copy(gpu_client, monkeypatch):  # noqa: F811
    import hashlib

    from app.gpu import drive
    from app.gpu.repository import Repository
    from app.gpu.scheduler import Scheduler

    _, store, user, project, _ = gpu_client
    repo = Repository(store)
    key = "inputs/job1/input_video.mp4"
    sha = "a" * 64
    item = repo.create("files", user["id"], {"kind": "drive_file", "state": "verified", "storage_key": key,
                                             "key_hash": hashlib.sha256(key.encode()).hexdigest(),
                                             "remote_id": "stagedfile12345", "sha256": sha})
    calls = []

    class FakeDrive:
        base_url = "https://www.googleapis.com/drive/v3"
        def __init__(self, *_):
            pass
        def _request(self, method, url, **kwargs):
            calls.append((method, url))
            return httpx.Response(204)
        def close(self):
            pass

    monkeypatch.setattr(drive, "DriveStorage", FakeDrive)
    job = {"id": "job1", "owner_id": user["id"], "project_id": project["id"], "provider": "colab",
           "staged_video": {"id": "stagedfile12345", "sha256": sha}}
    Scheduler(store)._discard_staged_video({**job, "id": "another-job"})
    assert calls == []
    Scheduler(store)._discard_staged_video(job)
    assert calls == [("DELETE", "https://www.googleapis.com/drive/v3/files/stagedfile12345")]
    assert repo.get("files", item["id"], user["id"]) is None


def test_runtime_staged_video_checks_hash_size_and_cleans_partial(monkeypatch, tmp_path):
    import hashlib
    import io

    from app.gpu import runtime_worker

    data = b"video-control-data"
    class FakeCache:
        def _json(self, *args, **kwargs):
            return {"id": "stagedfile12345", "size": str(len(data)), "trashed": False,
                    "md5Checksum": hashlib.md5(data, usedforsecurity=False).hexdigest()}
        def _open(self, *args, **kwargs):
            return io.BytesIO(data)

    monkeypatch.setattr(runtime_worker, "_drive_cache", lambda payload: FakeCache())
    descriptor = {"id": "stagedfile12345", "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    payload = {"snapshot": {"input_video": {"asset_id": "a"}}, "staged_video": descriptor}
    target = runtime_worker._restore_vace_video(tmp_path, payload)
    assert target.read_bytes() == data
    descriptor["sha256"] = "0" * 64
    with pytest.raises(runtime_worker.WorkerError, match="sha256 mismatch"):
        runtime_worker._restore_vace_video(tmp_path, payload)
    assert not (tmp_path / "input" / "input_video.mp4.part").exists()


def test_vace_cached_weights_reject_sha_mismatch_before_sampling(tmp_path):
    import hashlib

    from app.gpu import runtime_worker

    good, bad = b"expected", b"corrupt!"
    class FakeCache:
        def lookup(self, *args):
            return {"id": "syntheticfile123"}
        def fetch(self, entry, target):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(bad)

    weight = {"repo_id": "example/model", "filename": "model.safetensors", "revision": "a" * 40,
              "sha256": hashlib.sha256(good).hexdigest(), "target": "diffusion_models/model.safetensors", "size": len(good)}
    with pytest.raises(runtime_worker.WorkerError, match="sha256 mismatch"):
        runtime_worker._restore_weights(tmp_path, {"workflow_key": "wan21-vace-v2v", "weight_files": [weight]},
                                        timeout=10, cache=FakeCache(), prep=False)
    assert not (tmp_path / "models/diffusion_models/model.safetensors").exists()


def test_clip_preserves_audio_at_selected_start(gpu_client, tmp_path, monkeypatch):  # noqa: F811
    from app.gpu import drive
    from app.gpu.repository import Repository

    source = tmp_path / "changing-tone.mp4"
    subprocess.run([
        "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=16:duration=6",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=2:sample_rate=48000",
        "-f", "lavfi", "-i", "sine=frequency=880:duration=4:sample_rate=48000",
        "-filter_complex", "[1:a][2:a]concat=n=2:v=0:a=1[a]", "-map", "0:v:0", "-map", "[a]",
        "-c:v", "libx264", "-threads", "1", "-pix_fmt", "yuv420p", "-c:a", "aac", "-y", str(source),
    ], check=True, timeout=30)
    client, store, user, project, _ = gpu_client
    response = client.post("/api/assets", data={"project_id": project["id"]},
                           files={"file": ("tone.mp4", source.read_bytes(), "video/mp4")})
    asset = store.get("assets", response.json()["data"]["id"], user["id"])
    b = bundle()
    params = resolve_parameters(b["manifest"], {"video_asset_id": asset["id"], "start_seconds": 2.0})
    _, spec = resolve_video_input(lambda _: asset, project["id"], b["manifest"], params)
    clip = tmp_path / "clip.mp4"

    class FakeDrive:
        def __init__(self, *_):
            pass
        def put_file(self, key, path, mime):
            shutil.copyfile(path, clip)
            return {"id": "stagedfile12345", "size": path.stat().st_size, "sha256": "a" * 64, "verified": True}
        def close(self):
            pass

    monkeypatch.setattr(drive, "DriveStorage", FakeDrive)
    job = {"id": "job", "owner_id": user["id"], "project_id": project["id"], "provider": "colab",
           "snapshot": {"input_video": spec}}
    stage_video(job, store, Repository(store))
    generated = tmp_path / "result.mp4"
    shutil.copyfile(clip, generated)
    _mux_vace_audio(generated, clip, spec, 5000000)
    pcm = subprocess.run(["ffmpeg", "-v", "error", "-ss", "0.2", "-i", str(generated), "-t", "0.3",
                          "-map", "0:a:0", "-ac", "1", "-ar", "8000", "-f", "s16le", "-"],
                         capture_output=True, check=True, timeout=30).stdout
    import array
    samples = array.array("h", pcm)
    crossings = sum((left < 0 <= right) or (right < 0 <= left) for left, right in zip(samples, samples[1:]))
    assert 480 < crossings < 580  # 880 Hz over 0.3s; the earlier 440 Hz tone would have ~264 crossings.


def test_segment_planner_covers_full_range_without_hidden_truncation():
    from app.gpu.vace_segments import plan_segments

    for duration in (15, 30):
        segments = plan_segments(4, 4 + duration, 16)
        assert segments[0]["start_seconds"] == 4
        assert segments[-1]["end_seconds"] == 4 + duration
        assert sum(item["length"] for item in segments) == duration * 16
        assert all(17 <= item["length"] <= 81 and item["length"] % 4 == 1 for item in segments)
        assert all(left["end_seconds"] == right["start_seconds"] for left, right in zip(segments, segments[1:]))
    with pytest.raises(APIError):
        plan_segments(0, 3.1, 16)


def test_assemble_reuses_completed_segments_and_only_uses_original_audio(gpu_client, sample_video, tmp_path, monkeypatch):  # noqa: F811
    from app.gpu.repository import Repository
    from app.gpu.vace_segments import assemble_batch
    from app.storage import save_asset

    client, store, user, project, _ = gpu_client
    uploaded = client.post("/api/assets", data={"project_id": project["id"]},
                           files={"file": ("fixture.mp4", sample_video.read_bytes(), "video/mp4")}).json()["data"]
    source = store.get("assets", uploaded["id"], user["id"])
    repo = Repository(store)
    batch, _ = repo.create_batch(user["id"], [{"project_id": project["id"], "execution_policy": {"provider": "mock"}}] * 2,
                                 "batch-test", {"mode": "save"})
    batch_id = batch["id"]
    first_id, second_id = batch["job_ids"]
    specs = [{"asset_id": source["id"], "source_sha256": source["sha256"], "start_seconds": start,
              "duration_seconds": 33 / 16, "length": 33, "fps": 16.0, "width": 320, "height": 240,
              "keep_audio": True, "has_audio": True} for start in (0.0, 33 / 16)]
    for job_id, spec in zip((first_id, second_id), specs):
        snapshot = {"input_video": spec, "bound_graph": {"seed": 1}}
        repo.update("jobs", job_id, {"project_id": project["id"], "batch_id": batch_id,
                    "workflow_key": "wan21-vace-v2v", "workflow_version": "1.0.0", "provider": "mock",
                    "state": "failed" if job_id == second_id else "completed", "snapshot": snapshot}, user["id"])
    repo.create("jobs", user["id"], {"project_id": project["id"], "workflow_key": "wan21-vace-v2v",
                "workflow_version": "1.0.0", "provider": "mock", "state": "completed",
                "request_body": {"retry_of": second_id}, "snapshot": {"input_video": specs[1], "bound_graph": {"seed": 1}}},
                record_id="retry2")
    with pytest.raises(APIError, match="片段输出不存在"):
        assemble_batch(store, repo, user["id"], batch_id)
    for job_id, color, tone in ((first_id, "red", 1000), ("retry2", "blue", 1500)):
        path = tmp_path / f"{job_id}.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                        f"color=c={color}:s=320x240:r=16:d={33 / 16}", "-f", "lavfi", "-i",
                        f"sine=frequency={tone}:duration={33 / 16}:sample_rate=48000", "-t", str(33 / 16),
                        "-c:a", "aac", "-c:v", "libx264", "-threads", "1", "-pix_fmt", "yuv420p", "-y", str(path)],
                       check=True, timeout=30)
        output = save_asset(store, user["id"], project["id"], path.read_bytes(), f"{job_id}.mp4", job_id, {"mock": True})
        repo.update("jobs", job_id, {"output_asset_ids": [output["id"]]}, user["id"])
    from app.gpu import vace_segments

    with monkeypatch.context() as failed:
        def refuse(*args, **kwargs):
            raise APIError("VIDEO_INVALID", "simulated failure", 422)
        failed.setattr(vace_segments, "_run_media", refuse)
        with pytest.raises(APIError) as error:
            assemble_batch(store, repo, user["id"], batch_id)
        assert error.value.code == "VIDEO_ASSEMBLY_FAILED"
    assert asset_path(source).is_file()
    assert len([row for row in store.list("assets", user["id"], project["id"]) if row.get("vace_assembly_key")]) == 0
    result = assemble_batch(store, repo, user["id"], batch_id)
    assert result["vace_segment_jobs"] == [first_id, "retry2"]
    assert result["mock"] is True and result["video_metadata"]["has_audio"] is True
    assert result["video_metadata"]["duration_seconds"] == pytest.approx(66 / 16, abs=0.1)
    result_path = asset_path(result)
    def pixel(time):
        return subprocess.run(["ffmpeg", "-v", "error", "-ss", str(time), "-i", str(result_path), "-frames:v", "1",
                               "-vf", "scale=1:1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                              capture_output=True, check=True).stdout
    red, blue = pixel(0.5), pixel(3)
    assert red[0] > red[2] + 100 and blue[2] > blue[0] + 100
    colors = subprocess.run(["ffmpeg", "-v", "error", "-i", str(result_path), "-vf", "scale=1:1", "-f", "rawvideo",
                             "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout
    frames = [colors[index:index + 3] for index in range(0, len(colors), 3)]
    assert len(frames) == 66
    assert all(frame[0] > frame[2] + 100 for frame in frames[:33])
    assert all(frame[2] > frame[0] + 100 for frame in frames[33:])
    import array
    audio = subprocess.run(["ffmpeg", "-v", "error", "-ss", "2.4", "-i", str(result_path), "-t", "0.25",
                            "-map", "0:a:0", "-ac", "1", "-ar", "8000", "-f", "s16le", "-"],
                           capture_output=True, check=True).stdout
    samples = array.array("h", audio)
    crossings = sum((a < 0 <= b) or (b < 0 <= a) for a, b in zip(samples, samples[1:]))
    assert 190 < crossings < 250  # Original 440 Hz audio, not the generated segments' 1000/1500 Hz.
    assert assemble_batch(store, repo, user["id"], batch_id)["id"] == result["id"]
    assert client.post(f"/api/gpu/vace/batches/{batch_id}/assemble").json()["data"]["id"] == result["id"]
    assert client.post("/api/gpu/vace/batches/nonexistent/assemble").status_code == 404
    assert asset_path(source).is_file()
    assert repo.get("jobs", first_id, user["id"])["state"] == "completed"
    assert repo.get("jobs", second_id, user["id"])["state"] == "failed"
    assert client.get(f"/api/assets/{result['id']}/preview").status_code == 200


def test_segment_plan_api_requires_authentication(gpu_client):  # noqa: F811
    client, _, _, _, _ = gpu_client
    data = {"start_seconds": 0, "end_seconds": 30, "fps": 16}
    response = client.post("/api/gpu/vace/segment-plan", json=data)
    assert response.status_code == 200
    assert response.json()["data"][-1]["end_seconds"] == 30
    client.headers.pop("Authorization")
    assert client.post("/api/gpu/vace/segment-plan", json=data).status_code == 401


def test_video_rotation_metadata_is_read(gpu_client, sample_video, tmp_path):  # noqa: F811
    rotated = tmp_path / "rotated.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-display_rotation", "90", "-i", str(sample_video),
                    "-map", "0", "-c", "copy", "-y", str(rotated)], check=True, timeout=30)
    client, _, _, project, _ = gpu_client
    response = client.post("/api/assets", data={"project_id": project["id"]},
                           files={"file": ("rotated.mp4", rotated.read_bytes(), "video/mp4")})
    assert response.status_code == 201, response.text
    assert abs(int(response.json()["data"]["video_metadata"]["rotation"])) == 90


def test_webm_silent_upload_is_supported(gpu_client, tmp_path):  # noqa: F811
    path = tmp_path / "fixture.webm"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=16",
                    "-t", "2", "-an", "-c:v", "libvpx", "-threads", "1", "-y", str(path)],
                   check=True, timeout=30)
    client, _, _, project, _ = gpu_client
    response = client.post("/api/assets", data={"project_id": project["id"]},
                           files={"file": ("fixture.webm", path.read_bytes(), "video/webm")})
    assert response.status_code == 201, response.text
    assert response.json()["data"]["video_metadata"]["has_audio"] is False


def test_vace_retry_refuses_live_lease_or_existing_retry(gpu_client):  # noqa: F811
    from app.gpu.repository import Repository

    client, store, user, project, _ = gpu_client
    repo = Repository(store)
    original = repo.create("jobs", user["id"], {"project_id": project["id"], "workflow_key": "wan21-vace-v2v",
                           "provider": "colab", "state": "failed", "request_body": {}}, record_id="failed-segment")
    lease = repo.create("leases", user["id"], {"owner_job_id": original["id"], "state": "cleanup_pending"})
    endpoint = f"/api/gpu/jobs/{original['id']}/retry"
    result = client.post(endpoint, headers={"Idempotency-Key": "retry-a"}, json={})
    assert result.status_code == 409 and result.json()["error"]["code"] == "GPU_NOT_RELEASED"
    repo.update("leases", lease["id"], {"state": "deleted"}, user["id"])
    repo.create("jobs", user["id"], {"project_id": project["id"], "state": "queued",
                "request_body": {"retry_of": original["id"]}}, record_id="existing-retry")
    result = client.post(endpoint, headers={"Idempotency-Key": "retry-b"}, json={})
    assert result.status_code == 409 and result.json()["error"]["code"] == "RETRY_EXISTS"
    repo.update("jobs", "existing-retry", {"state": "failed", "workflow_key": "wan21-vace-v2v"}, user["id"])
    repo.create("jobs", user["id"], {"project_id": project["id"], "state": "queued",
                "request_body": {"retry_of": "existing-retry"}}, record_id="active-grandchild")
    result = client.post(endpoint, headers={"Idempotency-Key": "retry-c"}, json={})
    assert result.status_code == 409 and result.json()["error"]["code"] == "RETRY_EXISTS"
    assert not repo.list("executions", user["id"])
