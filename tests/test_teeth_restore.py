"""Dentition must keep its dimensions when only the lips change shape."""
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'dh_live_full'))
from teeth_restore import INNER, jaw_transform, restore_fixed_jaw, tone_enamel


def landmark_fixture(half_width=35, gap=18):
    points = np.zeros((478, 2), np.float32)
    points[33], points[263], points[168] = (32,20), (96,20), (64,35)
    angles = np.linspace(np.pi, 0, 11)
    upper = np.column_stack((64+half_width*np.cos(angles), 70-gap/2*np.sin(angles)))
    angles = np.linspace(0, np.pi, 11)[1:-1]
    lower = np.column_stack((64+half_width*np.cos(angles), 70+gap/2*np.sin(angles)))
    points[list(INNER)] = np.concatenate((upper,lower))
    return points


class FixedDentitionTests(unittest.TestCase):
    def setUp(self):
        self.donor_points = landmark_fixture()
        self.donor = np.zeros((128,128,3),np.uint8)
        for x in (48,57,66,75):
            self.donor[66:73,x:x+5] = 210

    def render(self, points):
        return restore_fixed_jaw(np.zeros_like(self.donor),points,self.donor,
                                 jaw_transform(points,self.donor_points),(0,0,0),1.)

    def test_lip_width_does_not_change_tooth_dimensions(self):
        narrow, wide = landmark_fixture(21), landmark_fixture(38)
        np.testing.assert_array_equal(jaw_transform(narrow,self.donor_points),
                                      jaw_transform(wide,self.donor_points))
        # Both masks expose these central incisors; the sampled tooth pixels
        # must match exactly even though the animated lips have widened.
        np.testing.assert_array_equal(self.render(narrow)[67:71,52:75],
                                      self.render(wide)[67:71,52:75])

    def test_closing_lips_occludes_instead_of_compressing_teeth(self):
        self.assertGreater(np.count_nonzero(self.render(landmark_fixture())>150),0)
        closed = landmark_fixture(gap=.1)
        closed[list(INNER),1] = 60
        self.assertEqual(np.count_nonzero(self.render(closed)>150),0)

    def test_actual_head_translation_moves_teeth_without_scaling(self):
        original = self.render(landmark_fixture())
        moved = self.render(landmark_fixture()+[7,3])
        np.testing.assert_array_equal(original[50:90,35:90],moved[53:93,42:97])

    def test_lower_lip_occludes_overlapping_upper_teeth(self):
        self.donor[66:90,48:80] = 240
        base = np.zeros_like(self.donor)
        # A bright fringe from the original model must also disappear inside
        # the lower-lip boundary, while the lip pixels remain untouched.
        base[76:79,48:80] = 240
        base[79:,:] = (30,45,85)
        result = restore_fixed_jaw(base.copy(),landmark_fixture(),self.donor,
                                   jaw_transform(self.donor_points,self.donor_points),(0,0,0))
        np.testing.assert_array_equal(result[79:,:],base[79:,:])
        self.assertLess(int(result[78,64,0]),80)
        self.assertGreater(np.count_nonzero(result[:76]>150),0)

    def test_enamel_tone_reduces_whites_without_flattening_shadows(self):
        self.donor[66:72,54:59] = 240
        self.donor[66:72,64:69] = 180
        self.donor[74:76,60:64] = 35
        toned = tone_enamel(self.donor,self.donor_points,.82)
        self.assertLessEqual(int(toned[68,56,0]),197)
        self.assertGreater(int(toned[68,56,0]),int(toned[68,66,0]))
        np.testing.assert_array_equal(toned[74,61],self.donor[74,61])


if __name__ == '__main__':
    unittest.main()
