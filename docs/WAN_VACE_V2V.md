# Wan2.1 VACE 视频重绘（开发版）

**状态：真实 GPU 冒烟已生成短视频，待人工验收。** 已有一次 Colab L4 推理生成带音轨的 MP4；画质、动作保真和声音同步仍需用户播放确认，显存峰值和长视频效果尚未验收。工作流初始为 Draft，不会自动发布。长片段按固定帧数规划，完成后需要用户手动点击合成；尚未实现镜头检测和边界帧一致性。未完成真实验收前，不应把本功能当作已完成的生产能力。

## 原片与 Drive 授权

本次用户指定的**手动验收素材**是 `huaqiang.mp4`，Google Drive File ID 为 `1tPysFirjaoPnl8YrfUyCGQd7shgnilmJ`，分享链接为 <https://drive.google.com/file/d/1tPysFirjaoPnl8YrfUyCGQd7shgnilmJ/view?usp=sharing>。用户提供的相对路径为 `videos/2026/11/12/huaqiang.mp4`，其存储归属尚未核实，不能当作容器绝对路径。此文件不是系统默认素材，禁止把原视频提交到 Git，也不要用于 CI、构建或自动运行。

2026-11-12 使用 Studio 的 Drive OAuth 凭据，在一致性数据库副本上进行只读检查：该 ID 的 Drive 元数据请求、媒体 HEAD 以及仅读取响应头的 `bytes=0-0` 范围 GET 均返回 **404**。因此当前 Studio 授权无法确认文件存在或可读；Google 的 404 也可能代表不可见，并不能区分文件不存在和无权限。没有下载原视频，也没有验证完整下载。用户之前能通过其他 Drive 连接查看元数据，并不代表 Studio 的 `drive.file` 令牌能读取该文件。

导入仅接受 File ID，调用固定的 Google Drive API，拒绝任意 URL；不公开文件、不扩权、不抓取分享页、不暴露令牌。如果 Studio 已授权的账号仍无访问权，请用本机 MP4 上传。当前尚未实现 Google Picker，无法通过本界面为 `drive.file` 增加对已有文件的访问授权。`drive_file_id`、上述未核实路径和导入后生成的 `studio_asset_id` 是三个不同标识；提交任务使用最后一个。

## 模型、许可证与节点

VACE 是用于视频编辑和控制的 Wan 模型。这里将原片帧的 Canny 边缘作为控制输入，文本提供画风；可选的一张参考图通过 VACE 的 `reference_image` 进入条件编码，并不是多人物身份绑定。无法保证消除闪烁、跳脸或完美复现动作。

- 上游：[Wan2.1](https://github.com/Wan-Video/Wan2.1)、[VACE 使用指南](https://github.com/ali-vilab/VACE/blob/main/UserGuide.md)、[ComfyUI 官方控制视频工作流](https://comfy.org/hub/workflows/wan2-1-vace-control-video-2652985596d8/)。
- 权重来源：[Comfy-Org/Wan_2.1_ComfyUI_repackaged](https://huggingface.co/Comfy-Org/Wan_2.1_ComfyUI_repackaged)，标示 Apache-2.0；管理员仍须在工作流审核时自行确认来源及许可证。
- Hugging Face 固定 revision：`ef09fd840388d354dcaeacef6a89ed9edec9fef4`。以下 SHA-256 和字节数来自该 revision 的仓库元数据；运行时依旧必须校验，不能用文档代替校验。

| 文件（在 `split_files/` 下） | 字节 | SHA-256 |
| --- | ---: | --- |
| `diffusion_models/wan2.1_vace_1.3B_fp16.safetensors` | 4309519800 | `640ccc0577e6a5d4bb15cd91b11b699ef914fc55f126c5a1c544e152130784f2` |
| `text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors` | 6735906897 | `c3355d30191f1f066b26d93fba017ae9809dce6c627dda5f6a66eaa651204f68` |
| `vae/wan_2.1_vae.safetensors` | 253815318 | `2fc39d31359a4b0a64f55876d8ff7fa8d780956ae2cb13463b0223e15148976b` |

ComfyUI 固定 commit `423cbd20511becb1f16122ab518e92e58bfa03d9`，无自定义节点。使用核心节点 `LoadVideo → GetVideoComponents → Canny → WanVaceToVideo.control_video`，正负提示词、VAE 和可选参考图接入 VACE；`KSampler → TrimVideoLatent → VAEDecode → CreateVideo → SaveVideo` 产生画面。节点白名单和权重锁在内置工作流配置中；启动 Comfy 后还会核对实际节点及输入名。Canny 所需的 kornia 和视频解码所需的 PyAV 来自该 commit 的 ComfyUI requirements；CPU 缓存现有机制会安装依赖，构建缓存可能联网并消耗 CPU runtime 的 CU。

默认使用 L4 规格、832×480、49 帧、16 FPS、20 步。宽高必须是 16 的倍数且面积不超过 832×480；帧数必须为 17–81 且为 `4n+1`，FPS 为 8–24。申请后的运行时须能识别 GPU，显存至少 20000 MiB、主机内存至少 10 GiB，否则尽早失败并走现有释放流程。这些只是拒绝明显不足的下限，不是运行成功、速度或画质保证。14B、Depth、Pose 尚未接入。

缓存权重恢复到 VACE 运行时和复用已热载的环境时，均重新计算文件 SHA-256；不以 Drive 元数据里的摘要或 MD5 代替权重 SHA-256 验证。校验失败必须检查缓存或上游文件，不能修改锁定值绕过审核。

## 集成位置与基线

- `api/app/main.py` 的 `/api/assets`、`/api/assets/drive-video` 负责鉴权上传及导入；`storage.asset_path` 和 `video_assets.VideoIngest` 管理私有文件与媒体检查。`video_assets.import_drive_video` 使用 `GoogleService` 和固定 Drive API。
- `admission.admit_job` 和 `review_routes.real_smoke` 解析参数、核查 owner/project，将资产 ID 绑定到 `gpu.vace_video.resolve_video_input`。`manifest.bind_parameters` 构建快照图，`builtin.seed_builtin_templates` 与 `Registry` 保持 Draft 和发布审核。
- `Scheduler._validate`/`_await_prep`/`_allocation` 保留预算、CPU 缓存和 GPU 调度；等待容量期间调用 `stage_video`；`MCPExecutor` 的载荷和 `runtime_worker` 负责模型恢复、视频校验、ComfyUI 及输出。`Scheduler._cleanup`、实例服务和 watchdog 保持释放机制。
- `DriveStorage.put_file` 使用可续传上传和校验；`GoogleService` 负责 OAuth 和账号作用域；`output_protocol.commit_output`、`/api/gpu/gallery` 和作品页维持结果及历史。
- `CreatePage`、`WorkflowActivation`、`JobsPage`、`GalleryPage` 负责素材选择、冒烟审核、分段及预览。`gpu.vace_segments` 负责只读计划与合成已完成输出。

本次文件级改动如下；素材、批次和任务均沿用现有表和 JSON 字段，无数据库结构迁移：

| 文件 | 作用 |
| --- | --- |
| `Dockerfile` | 在现有镜像中安装 FFmpeg/ffprobe。 |
| `README.md` | 标注 VACE 草稿和人工验收状态。 |
| `api/app/config.py` | 定义视频大小、时长、分辨率上限。 |
| `api/app/video_assets.py` | 分块落盘、媒体探测、Drive 导入和临时文件清理。 |
| `api/app/main.py` | 接入鉴权上传、Drive 导入、分段计划、合成和重试保护。 |
| `api/app/admission.py` | 对生成任务绑定视频素材和不可变片段描述。 |
| `api/app/presenters.py` | 在任务响应中暴露安全的重试关联 ID。 |
| `api/app/review_routes.py` | 冒烟时校验视频素材并提供片段起点。 |
| `api/app/gpu/manifest.py` | 绑定视频文件名并验证受限预处理参数。 |
| `api/app/gpu/compute.py` | 在模拟任务中生成明确标记的合成测试视频，不执行模型推理。 |
| `api/app/gpu/mcp.py` | 将短片段的 Drive 标识和哈希交给受信任运行时。 |
| `api/app/gpu/prep.py` | CPU 缓存任务不携带用户视频及参考图。 |
| `api/app/gpu/registry.py` | 将已核对的 VACE 核心节点纳入审核白名单。 |
| `api/app/gpu/runtime_worker.py` | 验证 GPU、节点、权重和视频；运行 ComfyUI；合成并核验音轨。 |
| `api/app/gpu/scheduler.py` | GPU 领取前暂存视频，保持预算、取消、清理与缓存流程。 |
| `api/app/gpu/vace_video.py` | 校验项目素材、片段范围和短片转码。 |
| `api/app/gpu/vace_segments.py` | 规划连续片段、定位成功重试并安全合成。 |
| `api/app/gpu/templates/builtin/catalog.json` | 将 VACE 注册为内置草稿。 |
| `api/app/gpu/templates/builtin/wan21-vace-v2v-1.0.0.json` | 模型来源、锁定权重、节点图及参数。 |
| `web/src/App.tsx` | 提交分段批次、重试、合成与查看源视频。 |
| `web/src/components/WorkflowActivation.tsx` | 支持视频冒烟输入、费用提示及人工验收。 |
| `web/src/lib/activation-resume.ts` | 在冒烟参数中保留数值起点。 |
| `web/src/lib/gpu-api.ts` | 视频导入、分段、合成及预览 API 客户端。 |
| `web/src/pages/CreatePage.tsx` | 视频重绘界面、元数据、片段预览和风格预设。 |
| `web/src/pages/JobsPage.tsx` | 分段状态和合成入口。 |
| `web/src/pages/GalleryPage.tsx` | 使用鉴权预览地址播放视频。 |
| `tests/test_vace_video.py` | 合成视频、音频、图连接、安全及队列测试。 |
| `tests/test_builtin_workflows.py` | 验证新增模板默认保持草稿。 |
| `scripts/offline_vace_browser_check.py` | 离线浏览器验证上传与分段。 |
| `scripts/offline_browser_check.py` | 修正原有浏览器检查的运行策略选择器。 |
| `docs/API调用文档.md` | 记录新增素材与分段 API。 |
| `docs/examples/verify_api_docs.py` | 改用当前应用版本验证 API，移除过期的固定源码版本。 |
| `docs/BUILTIN_WORKFLOWS.md` | 标注新增草稿和人工验收。 |
| `docs/WAN_VACE_V2V.md` | 记录模型、操作、测试、未验证项及回滚。 |

开发开始时基线为 `main` / `685c482`（`v0.3.0-1-g685c482`），工作区当时干净；本机运行中的 Compose 服务为 api、worker、watchdog，共享卷位于 `/var/lib/docker/volumes/colab-gpu-studio_studio-data/_data`。SQLite 一致性快照和 `.env`、Compose 配置的受限备份位于 `/root/colab-gpu-studio-backups/wan-vace-20261112T095009Z`；快照完整性检查为 `ok`。快照时有一项任务为运行状态，本次未重建生产服务、未中断任务。开发期间还发现其他并行工作修改了监控相关代码，应分别审查，不能混入本功能提交或部署镜像。

## 操作与短片冒烟

此节为**之后的手动验收步骤，不会由测试自动执行**：

1. 先确认部署已由管理员单独批准，GPU 空闲，项目和 Google Colab/Drive 账号正确，日/月预算与单任务上限足够。登录 Studio，进入创作 → 视频重绘 / V2V。
2. 在素材区上传 MP4/WebM，或输入 Drive File ID 后导入。上传有进度，素材在项目中持久化，可重复选择；显示时长、帧率、分辨率和音轨。默认上限 512 MiB、3600 秒、4096 像素最长边和 216000 估算帧；反向代理也必须允许该大小。导入和上传只准备素材，不会自动提交生成。
3. 在工作流与模型管理中审核 `wan21-vace-v2v` 1.0.0、许可证、权重与节点。真实冒烟时选择刚导入的项目视频，并输入片段起点；默认 49/16 = 3.0625 秒。按界面预算和费用提示，由用户明确确认后才会创建 CPU/GPU runtime。该工作流的冒烟完成后会暂停，用户须播放并检查结果及 GPU 释放，再勾选确认以验收和发布。
4. 发布后在创作页选择原视频、起点和终点；短片终点会显示为对齐后的实际 `4n+1` 帧范围，超出可处理范围会报错。可选一张人物或画风参考图，修改默认的原创热血漫画正负提示词、种子、控制强度、步数与是否保留原音。无需使用未经授权的漫画原图、角色或配乐。
5. 明确选择 Colab 账号。确认页检查工作流、机器、数量、单任务时限与费用；实际 GPU、进度、失败原因、CU 和释放状态见任务、算力页面。模型缓存首次准备会使用 CPU Colab runtime，GPU 推理也会消耗 CU；估算值不是 Google 账单。不得保证某 GPU 必然可用。
6. 完成后到作品页预览、下载 MP4、打开关联原视频；任务详情保留参数。原片没有音轨时生成无声视频。音轨由所选原片时间段提取，经 AAC 合成；结果会检查帧数、帧率、尺寸、时长和有无音轨，但听感和逐帧同步仍须人工验收。

### 长视频和失败重试

在视频重绘页勾选「长视频分段」，输入起止时间及 FPS，点击「计算分段」。终点先按视频帧对齐，系统将整个范围划成最多 20 个 `4n+1` 帧片段；无法完整覆盖会报错，不会悄悄截取前几秒。提交确认页显示片段数量与总范围；每段是独立任务、会单独检查预算并可能消耗 CU。任务页显示批次和已完成片段数；失败的真实片段点击「重试」并确认费用，只创建该片段的新任务。已有进行中或成功重试时服务端拒绝重复重试。等所有片段或其重试成功后，点击「合成完整视频」。合成按原始顺序核验片段与源视频哈希、参数和时间连续性，统一 H.264、帧率、尺寸及像素格式，并从原片完整范围取一次音轨；校验帧数、时长和音轨后保存作品。相同结果再次合成会复用已有作品；合成或单段失败不会删除源视频及成功片段。

该模式是固定时长分段基础版，不包含场景切换检测、自动衔接、多个身份参考图或闪烁修复；可先用短片确认质量。相邻片段的镜头和人物一致性不作保证。真实多段推理、长音轨同步、异常中断恢复和显存峰值仍需人工验收。批次历史状态保留原始失败任务，界面同时跟踪其新重试任务；合成读取已成功的重试结果。

模型缓存采用原有 CPU 准备任务和用户 Drive；VPS 不下载模型权重。用户视频在 VPS 上分块落盘，生成前先生成受限的短片段并使用 Drive 可续传上传；Colab 用短期 Drive 凭据下载并验证大小、MD5 和 SHA-256。片段和参考图不会以完整视频 Base64 装入任务 JSON。GPU 完成或失败后尝试删除 Drive 暂存片段；失败会记录事件，可能需要用户清理该任务的 `AI-Studio/<owner>/inputs/<job-id>/` 副本，绝不删除原素材。长传输中取消可能在当前步骤结束后生效。

真实冒烟前必须逐项记录：模型、实际目标 GPU/规格、预计耗时与 CU、当前账号及全局预算、工作流版本、模型缓存状态。尚无此模型在用户机器上的实测耗时或 CU；不要把界面估算当作已验证数值。

## 错误与限制

- `DRIVE_FILE_INACCESSIBLE` 的 403/404：先检查 Studio 账号和 Drive 文件权限；仍不可见时本机上传。`DRIVE_AUTH_FAILED`：重新连接 Drive。不要把分享链接填成下载地址。
- `VIDEO_INVALID` / `VIDEO_DECODE_FAILED`：文件损坏、容器不符或所选片段帧不足。`VIDEO_RANGE_INVALID`：选段超出源视频。
- `video_model_cache_failed`：CPU 缓存任务失败，先查看任务事件；默认不会在缓存失败后继续开 GPU 下载模型。模型 SHA 不符会中止；不要修改散列以绕过审核。
- `gpu_memory_insufficient`、`gpu_out_of_memory`：记录实际 GPU/内存并降低参数或更换规格；不要无限重试。`comfy_node_mismatch`：检查锁定的 ComfyUI commit 和节点状态。
- 任务预算、Colab 无空闲规格、运行时创建/超时/取消/清理失败继续由现有调度器及 watchdog 处理。页面显示清理待确认时，不能认定 GPU 已释放。
- 长视频合成需要手动触发；真实 GPU 长片及跨片段音画一致性未验证。真实片段重试会创建新任务；用户应先核对费用，已成功输出不会因其他任务失败而被删除。

## 离线测试

自动化测试以 FFmpeg 生成的图案和测试音频为输入，网络门禁阻止 Colab/Drive/OAuth 真实调用，真实消费为 0 CU。以下是本机从 `/root/apps/colab-gpu-studio-wan-vace` 实际执行并通过的命令；其中测试容器标签是本机已有的开发镜像，不是生产镜像：

```bash
docker run --rm --network none --user 0 -v "$PWD:/work" -w /work \
  -e PYTHONPATH=/work/api colab-gpu-studio:wan-vace-tests \
  python -m pytest -q --tb=short
docker run --rm --network none --user 0 -v "$PWD:/work" -w /work \
  -e PYTHONPATH=/work/api colab-gpu-studio:wan-vace-tests \
  python -m pytest -q tests/test_vace_video.py --tb=short
docker run --rm --network none --user 0 -v "$PWD:/work" -w /work \
  colab-gpu-studio:wan-vace-tests \
  ruff check api tests scripts/offline_vace_browser_check.py scripts/offline_browser_check.py
docker run --rm --network none --user 0 -v "$PWD:/source:ro" --entrypoint sh \
  colab-gpu-studio:wan-vace-tests -c \
  'cp -a /source /tmp/studio && cd /tmp/studio && python docs/examples/verify_api_docs.py'
docker run --rm --network none -v "$PWD:/source:ro" --entrypoint sh \
  colab-gpu-studio-web:diagnostic -c \
  'cp -a /source/web /tmp/studio-web && ln -s /web/node_modules /tmp/studio-web/node_modules && cd /tmp/studio-web && npm run typecheck && npm run test:contract && node --test tests/*.test.mjs && npm run build'
docker build --quiet -t colab-gpu-studio:wan-vace-delivery .
cd /root/apps/colab-gpu-studio
docker compose config --quiet
```

视频页面的独立离线浏览器检查实际执行：

```bash
cd /root/apps/colab-gpu-studio-wan-vace
docker run --rm --network none --user 0 -v "$PWD:/source:ro" -w /tmp --entrypoint sh \
  colab-gpu-studio:wan-vace-browser-tests -c 'set -e
cp -a /source /tmp/studio
cd /tmp/studio
python scripts/offline_server.py --port 18788 >/tmp/studio-server.log 2>&1 &
server=$!
trap "kill $server 2>/dev/null || true" EXIT
python -c '\''import time,urllib.request
for i in range(60):
 try:
  urllib.request.urlopen("http://127.0.0.1:18788/api/health",timeout=1); break
 except Exception: time.sleep(1)
else: raise SystemExit("offline server failed to start")'\''
OFFLINE_CHROMIUM=/usr/bin/chromium python scripts/offline_vace_browser_check.py'
```

Mock 任务在本机验证视频片段，返回明确标记的合成测试图案 MP4；它不上传视频至 Drive、不执行模型推理，也不包含原片声音。测试分别验证节点图连线、分块上传、元数据、所有权、Drive 错误、片段裁剪、原片音轨合成及队列保存；它不能证明真实模型可执行、GPU 性能、长视频同步或画风质量。其他负面场景和真实运行仍须补充验收。

本次在独立 worktree 的容器中执行了最终应用代码的 `python -m pytest -q --tb=short`（535 项通过，一条 Starlette 测试客户端弃用警告）；`python -m pytest -q tests/test_vace_video.py --tb=short` 也通过（24 项），包括在 Drive 存储已配置时禁止任何 Drive 调用的端到端模拟。`ruff check api tests scripts/offline_vace_browser_check.py scripts/offline_browser_check.py` 通过。修复原有 API 文档验证脚本的旧版本判断后，`python docs/examples/verify_api_docs.py` 的 25 项检查通过。前端 `npm run typecheck`、`npm run test:contract`、`node --test tests/*.test.mjs`（45 项）及 `npm run build` 通过。隔离 Chromium 测试验证了上传、草稿禁用、刷新后的素材和 15 秒分段；原有浏览器脚本的 70 个尺寸/主题检查通过，未观察到外部请求。浏览器测试使用 FFmpeg 合成素材与离线 Mock，0 CU。

生产 Compose 的 `docker compose config --quiet` 通过；独立 worktree 执行 `docker build --quiet -t colab-gpu-studio:wan-vace-delivery .` 成功，未使用生产标签。额外尝试的 `docker build --network none` 因 FFmpeg 系统包安装需要软件源而失败，普通构建随后成功。直接把源码挂载到前端测试容器运行 `npm run typecheck` 时，因源码目录不在容器依赖的搜索路径下而报 `tsc: not found`；上面的临时副本和依赖路径修正后，类型检查、契约、测试和构建均通过。最初直接启动测试镜像因未提供 Compose 中的 `DATABASE_URL` 和 `DATA_DIR` 而失败；指定为临时容器 `/data` 后，无网络的 `/api/health` 返回 `status=ok`。这些检查不代表已经部署生产或验证了真实 OAuth 文件下载、Colab GPU 和 VACE 输出。

## Docker 部署前与回滚

**以下仅为操作手册，本次没有执行生产部署。** 当前 Compose 将 SQLite、素材和历史作品放在 `colab-gpu-studio_studio-data` 卷，切勿删除该卷或运行 `docker compose down -v`。不修改或重置 `ENCRYPTION_KEY`；保留原 `.env`、OAuth 凭据和原视频。先确认所有测试通过、当前 Git 改动属于预期、没有运行中/待清理的 GPU 任务，确认新镜像包含审核通过的内容并获得部署授权。并行开发时不要把其他工作区改动意外打进镜像。

在项目目录执行以下示例，先做 SQLite Backup API 一致性快照，并把配置备份到 root 私有目录（目录和文件均含敏感内容）：

```bash
bash <<'SH'
set -euo pipefail
cd /root/apps/colab-gpu-studio
umask 077
STUDIO_BACKUP_DIR="/root/colab-gpu-studio-backups/pre-vace-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p /root/colab-gpu-studio-backups
mkdir -m 700 "$STUDIO_BACKUP_DIR"
cp -p .env docker-compose.yml "$STUDIO_BACKUP_DIR/"
docker compose exec -T api python - <<'PY' > "$STUDIO_BACKUP_DIR/studio.db"
import shutil, sqlite3, sys, tempfile
from pathlib import Path
with tempfile.TemporaryDirectory() as d:
    snapshot = Path(d) / 'studio.db'
    with sqlite3.connect('file:/data/studio.db?mode=ro', uri=True) as source, sqlite3.connect(snapshot) as target:
        source.backup(target)
        assert target.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    with snapshot.open('rb') as f:
        shutil.copyfileobj(f, sys.stdout.buffer)
PY
docker compose config --quiet
STUDIO_ROLLBACK_TAG="colab-gpu-studio:pre-vace-$(date -u +%Y%m%dT%H%M%SZ)"
if docker image inspect "$STUDIO_ROLLBACK_TAG" >/dev/null 2>&1; then
  printf '回滚镜像标签已存在，请检查后重新执行。\n' >&2
  exit 1
fi
docker image tag colab-gpu-studio:0.3.0 "$STUDIO_ROLLBACK_TAG"
printf '%s\n' "$STUDIO_ROLLBACK_TAG" > "$STUDIO_BACKUP_DIR/image-tag.txt"
docker build -t colab-gpu-studio:wan-vace-release /root/apps/colab-gpu-studio-wan-vace
docker image tag colab-gpu-studio:wan-vace-release colab-gpu-studio:0.3.0
docker compose up -d --no-build --no-deps api
docker compose up -d --no-build --no-deps worker watchdog
docker compose ps
curl -fsS http://127.0.0.1:8788/api/health
printf '\n备份目录：%s\n回滚镜像：%s\n' "$STUDIO_BACKUP_DIR" "$STUDIO_ROLLBACK_TAG"
SH
```

实际执行前还应按现有备份策略备份资产卷，确认恢复文件可读。新镜像不需要数据库表迁移；工作流草稿及媒体元数据写入现有 JSON 记录。重建后检查 API health、worker/watchdog、历史作品、设置页的 OAuth 连接状态与预算；不要把密钥或凭据打印到日志。网络服务持续健康并不等于真实 GPU 验收成功。

若需要回滚旧镜像，在确认没有 GPU 任务且得到操作授权后，填写前一步打印的备份目录：

```bash
bash <<'SH'
set -euo pipefail
cd /root/apps/colab-gpu-studio
read -r -p '部署时打印的备份目录：' STUDIO_BACKUP_DIR < /dev/tty
read -r STUDIO_ROLLBACK_TAG < "$STUDIO_BACKUP_DIR/image-tag.txt"
docker image tag "$STUDIO_ROLLBACK_TAG" colab-gpu-studio:0.3.0
docker compose up -d --no-build --no-deps api
docker compose up -d --no-build --no-deps worker watchdog
docker compose ps
curl -fsS http://127.0.0.1:8788/api/health
SH
```

旧 Git 版本可在**单独路径**建立只读目的的 worktree 并从该目录构建镜像，例如 `git worktree add --detach /root/apps/colab-gpu-studio-rollback 685c482`，再以该目录为 `docker build` 上下文；不要清理或覆盖当前工作区。无数据库迁移需要回滚，正常应保留当前数据库和卷。只有确认必须恢复数据库时，先停止所有服务并确保无任务，然后以 SQLite Backup API 从一致性快照恢复并验证；这会丢失快照之后的任务/素材记录，必须单独取得用户授权。`.env` 的备份只在确实需要时恢复，不能随意替换主密钥。

仅当用户单独授权恢复数据库、已确认无 GPU 任务，并且指定经过验证的快照目录时，恢复示例为：

```bash
bash <<'SH'
set -euo pipefail
cd /root/apps/colab-gpu-studio
read -r -p '部署时打印的备份目录：' STUDIO_BACKUP_DIR < /dev/tty
docker compose stop api worker watchdog
read -r STUDIO_ROLLBACK_TAG < "$STUDIO_BACKUP_DIR/image-tag.txt"
docker run --rm -i --user 0 -v colab-gpu-studio_studio-data:/data --entrypoint python \
  "$STUDIO_ROLLBACK_TAG" -c '
import os, shutil, sqlite3, sys, tempfile
from pathlib import Path
with tempfile.TemporaryDirectory() as folder:
    source_path = Path(folder) / "backup.db"
    with source_path.open("wb") as stream:
        shutil.copyfileobj(sys.stdin.buffer, stream)
    with sqlite3.connect(source_path) as source, sqlite3.connect("/data/studio.db") as target:
        assert source.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        source.backup(target)
        assert target.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    for suffix in ("", "-wal", "-shm"):
        path = Path("/data/studio.db" + suffix)
        if path.exists(): os.chown(path, 10001, 10001)
' < "$STUDIO_BACKUP_DIR/studio.db"
docker compose up -d
curl -fsS http://127.0.0.1:8788/api/health
SH
```
