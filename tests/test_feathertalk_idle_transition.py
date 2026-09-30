"""Run inside the FeatherTalk container (OpenCV), or a host with OpenCV installed."""
import sys
import unittest
import importlib.util
from types import SimpleNamespace

import numpy as np

sys.path.insert(0, "/lab")
HAS_OPENCV = importlib.util.find_spec("cv2") is not None
if HAS_OPENCV:
    try:
        from feathertalk_lab.idle_transition import IdleTransition
    except ModuleNotFoundError:
        from idle_transition import IdleTransition


@unittest.skipUnless(HAS_OPENCV, "Run these image checks in the FeatherTalk container")
class IdleTransitionTests(unittest.TestCase):
    def make_transition(self):
        base = np.stack([np.full((60, 60, 3), 30 + i * 10, np.uint8) for i in range(4)])
        boxes = np.array([[10+i, 15, 40+i, 45] for i in range(4)])
        engine = SimpleNamespace(frames=base, bboxes=boxes, cursor=3, count=4)
        last = base[2].copy()
        x1, y1, x2, y2 = boxes[2]
        last[y1:y2, x1:x2] += 80
        return engine, IdleTransition(engine, 2, last, 8)

    def test_head_trajectory_and_loop_are_continuous(self):
        engine, transition = self.make_transition()
        indexes = [transition.next_frame()[0] for _ in range(8)]
        self.assertEqual(indexes, [3, 0, 1, 2, 3, 0, 1, 2])
        self.assertEqual(engine.cursor, 3)
        with self.assertRaises(StopIteration):
            transition.next_frame()

    def test_only_face_changes_and_final_frame_exactly_matches_idle(self):
        engine, transition = self.make_transition()
        for _ in range(8):
            index, image = transition.next_frame()
            x1, y1, x2, y2 = engine.bboxes[index]
            outside = np.ones(image.shape[:2], bool)
            outside[y1:y2, x1:x2] = False
            np.testing.assert_array_equal(image[outside], engine.frames[index][outside])
        np.testing.assert_array_equal(image, engine.frames[index])

    def test_mouth_residual_eases_down_without_brightness_jump(self):
        engine, transition = self.make_transition()
        values = []
        for _ in range(8):
            index, image = transition.next_frame()
            values.append(float(np.max(image.astype(float) - engine.frames[index])))
        self.assertTrue(all(a >= b for a, b in zip(values, values[1:])))
        self.assertGreater(values[0], 70)
        self.assertEqual(values[-1], 0)
        self.assertLess(values[-2], 5)

    def test_processed_idle_is_the_tail_target_instead_of_raw_base(self):
        engine, _ = self.make_transition()
        processed = engine.frames.copy()
        for i, (x1, y1, x2, y2) in enumerate(engine.bboxes):
            processed[i, y1:y2, x1:x2] += 25
        engine.idle_frame = lambda index: processed[index].copy()
        last = processed[2].copy()
        x1, y1, x2, y2 = engine.bboxes[2]
        last[y1:y2, x1:x2] += 55
        transition = IdleTransition(engine, 2, last, 8)
        residual = []
        for _ in range(8):
            index, image = transition.next_frame()
            residual.append(float(np.max(image.astype(float) - processed[index])))
        self.assertTrue(all(a >= b for a, b in zip(residual, residual[1:])))
        np.testing.assert_array_equal(image, processed[index])
        self.assertFalse(np.array_equal(image, engine.frames[index]))


if __name__ == "__main__":
    unittest.main()
