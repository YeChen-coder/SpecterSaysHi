# 秦彻独立运行工程

2026-10-17，主项目新增独立 `feathertalk-qinche` 服务，使用已认可的 epoch 100 视觉模型、自然倾听底片和 `NativeFaceMatch` 嘴部局部合成。此公开目录提供程序代码与资产协议，不分发私人人物权重、底片、语音、训练素材、定位缓存、图片或预生成视频。

## 与 Gentleman 的隔离

| 内容 | 秦彻 | Gentleman |
| --- | --- | --- |
| Compose 服务 | `feathertalk-qinche` | `feathertalk` |
| 代码与前端 | `feathertalk_qinche_runtime/` | `feathertalk_lab/` |
| 镜像 | `feathertalk-qinche-approved:20261017` | `feathertalk-specter:local` |
| 角色配置 | `SPECTER_QINCHE_MODE/BUFFER_MS` | `SPECTER_FEATHERTALK_*` |
| 外观 | `NativeFaceMatch`，模型牙齿 | 原角色的牙齿、下巴、细节与收尾配置 |

秦彻运行时不挂载或导入 `feathertalk_lab`，不挂载旧秦彻资产，不使用原角色的牙齿 donor、刚性上颌、下巴匹配、说话锐化或收口动画。常驻预热、增量传输、四秒历史和前端帧队列在独立代码中适配，后续分别维护与验证。

`vendor/` 保存本版本使用的上游公共代码快照及其许可证，推理模型定义和音频编码实现不会随另一角色的工程变动。公共音频权重不在公开仓库中；所需文件为 `vendor/feather_hubert.pth`，指纹见 `source_lock.json`。上游来源为 [FeatherTalk](https://github.com/anliyuan/FeatherTalk)，快照基于 `ace1227ec0a367a983101bfc17d7e5fcf2bfb63f`。

Dockerfile 锁定原实验环境的本地基础镜像摘要。该环境镜像没有公开发布，GitHub 也不提供现成角色资产，因此全新克隆不能直接运行此角色。迁移自己的部署时需另行准备基础环境、模型、音频编码权重和下列资产；为新身份适配时也要重新建立并核对指纹，不能伪装成原认可包。

## 资产和合成协议

`compose.yaml` 把本地 `feathertalk_qinche_clean_20261016/` 挂到 `/assets`，仅允许读取：

| 文件 | 作用 |
| --- | --- |
| `checkpoints/epoch_100.pth` | 视觉模型，正式 SHA-256 在 `engine.py` |
| `data/base.jpg`、`data/base.lms` | 引擎初始化参考和定位 |
| `data/dynamic_base_natural/frames.npy` | `(251,1088,720,3)` uint8 BGR 原生帧 |
| `data/dynamic_base_natural/landmarks.npy` | `(251,110,2)` 定位 |
| `data/dynamic_base_natural/bboxes.npy` | `(251,4)` 下半脸 bbox |
| `data/dynamic_base_natural/source_indices.npy` | `(500,)`，`0..250,249..1` 往返映射 |
| `data/natural_selection.json` | 底片选择记录 |
| `output/dynamic_natural/idle_loop.mp4` | 25 fps、20 秒、无声待机视频 |
| `data/qinche_speech_12_v14/audio.wav` | 可选验证音频，非运行必需 |

另需在镜像构建目录准备对应底片的 `poster.jpg`。所有媒体、权重和数组均被公开仓库忽略。`asset_lock.json` 只有哈希，不包含资产内容。

视觉预测仍为 144×144，先从 152×152 crop 取中央区域；输入按参考导出程序使用 CPU float32 归一化。嘴部为 110 点布局的 90..109，鼻部为 51..65。`NativeFaceMatch` 只在软边嘴周合成，对周围皮肤估计空间颜色场，按当前及历史数据平滑；范围外保留原片像素。输出 ROI 直接写回原生 bbox，不能再次调用整块脸回贴。

启动时为所有唯一帧准备参考和 mask。推理帧号、模型输入、定位和恢复待机都通过同一 500 帧映射。颜色场在独立说话会话开始时重置；不修改原始帧缓存。说完直接接回下一张待机原片，`settle_ms=0`。

## 调用方式

具备独立准备的本地资源后，在根 `.env` 设置：

```dotenv
SPECTER_AVATAR_BACKEND=feathertalk
SPECTER_AVATAR_CHARACTER=QinChe
SPECTER_QINCHE_MODE=fast
SPECTER_QINCHE_BUFFER_MS=100
```

运行 `.\start_avatar.ps1`，访问 <http://127.0.0.1:18988/>。改成 `gentleman` 后再次运行同一脚本切回。两个角色保留相同访问端口，由脚本先停另一服务再启动所选服务；`18986` 在秦彻服务下转到正式页。

`/publish` 接收八字节时间戳＋24 kHz 单声道 PCM16，连续重采样到 16 kHz。fast/balanced/quality 的额外未来音频等待为 0/400/1000 ms；FIR 前视约 1.33 ms。健康检查在模型和实际分包形状预热后才开放，返回实际模型、底片、合成器、音频参数和合成计时。

## 验证范围

- `tests/test_qinche_runtime.py`：三个模式的 45 秒历史边界、时间线和会话重置，不依赖角色权重。
- `tests/validate_qinche_runtime_gpu.py`：有私有资源的环境中运行，留出音频十秒的 250 帧预测与认可缓存最大像素差为 0；嘴周外原片保护、往返和循环边界通过。
- 已在实际主项目完成 24 kHz、100 ms 墙钟发包测试：45 秒、1,125 个连续说话帧，覆盖两次循环、打断重说、断线重连、主动关闭与正确帧号恢复待机。
- 音频历史峰值约 4.14 秒，释放会话后归零。没有让诊断记录随会话时长累积。

浏览器首帧测量从首个完整 100 ms 包发出开始：启动预热后首次话语抵达约 119 ms、显示约 158 ms；后续轮次约 45/48 ms。仅是本机少量测量，不含上游生成、首包积累或未预热模型的冷启动。

clean 目录公开的训练和预处理代码是研究参考；动态导出程序也保留历史候选分支，不是生产服务。实时认可路径以本目录的 `engine.py` 和 `NativeFaceMatch` 为准。私有训练清单、素材和预测缓存不分发，也不要通过启动训练入口来运行主项目。
