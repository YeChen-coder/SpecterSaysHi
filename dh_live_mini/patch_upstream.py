"""Compatibility fixes for the pinned DH_live preprocessing source."""

from pathlib import Path


def replace(path: str, old: str, new: str, expected: int = 1) -> None:
    file = Path(path)
    source = file.read_text()
    if source.count(old) != expected:
        raise RuntimeError(f"unexpected upstream source in {path}: {old!r}")
    file.write_text(source.replace(old, new))


replace(
    "data_preparation_mini.py",
    "subprocess.CREATE_NO_WINDOW",
    'getattr(subprocess, "CREATE_NO_WINDOW", 0)',
)

# OpenCV returns the alternate SCRFD model's outputs in name order, not stride
# order. The official checkpoint uses out0..out8 and a batch dimension.
detector = "talkingface/util/face_detect_scrfd.py"
replace(
    detector,
    "outs = self.net.forward(self.net.getUnconnectedOutLayersNames())",
    "names = self.net.getUnconnectedOutLayersNames()\n"
    "        outs = dict(zip(names, self.net.forward(names)))\n"
    "        if 'score_8' not in outs:\n"
    "            for idx, stride in enumerate(self._feat_stride_fpn):\n"
    "                outs[f'score_{stride}'] = outs[f'out{idx}']\n"
    "                outs[f'bbox_{stride}'] = outs[f'out{idx + self.fmc}']\n"
    "                outs[f'kps_{stride}'] = outs[f'out{idx + self.fmc * 2}']",
    expected=2,
)
replace(detector, "scores = outs[idx][0]", 'scores = outs[f"score_{stride}"].reshape(-1)', expected=2)
replace(detector, "bbox_preds = outs[idx + self.fmc * 1][0] * stride", 'bbox_preds = outs[f"bbox_{stride}"].reshape(-1, 4) * stride', expected=2)
replace(detector, "kps_preds = outs[idx + self.fmc * 2][0] * stride", 'kps_preds = outs[f"kps_{stride}"].reshape(-1, 10) * stride', expected=2)
replace(detector, "scores = np.vstack(scores_list).ravel()", "scores = np.concatenate(scores_list)", expected=2)
