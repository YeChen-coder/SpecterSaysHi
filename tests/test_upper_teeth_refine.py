"""Enamel refinement must preserve gums/cavity and moving lower teeth."""
import sys
import unittest
from pathlib import Path
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'feathertalk_lab'))
from teeth_geometry import INNER,jaw_transform
from upper_teeth_refine import refine_upper_enamel


def fixture():
    points=np.zeros((478,2),np.float32)
    points[33],points[263],points[168]=(32,20),(96,20),(64,35)
    angles=np.linspace(np.pi,0,11)
    upper=np.column_stack((64+35*np.cos(angles),70-9*np.sin(angles)))
    angles=np.linspace(0,np.pi,11)[1:-1]
    lower=np.column_stack((64+35*np.cos(angles),70+9*np.sin(angles)))
    points[list(INNER)]=np.concatenate((upper,lower))
    donor=np.full((128,128,3),10,np.uint8)
    for x in [49,57,65,73]:donor[63:67,x:x+6]=220
    return points,donor


class UpperTeethRefineTests(unittest.TestCase):
    def test_dark_cavity_and_lower_teeth_are_preserved(self):
        points,donor=fixture();image=np.full_like(donor,45)
        image[63:67,49:79]=160;image[73:77,49:79]=190
        before=image.copy()
        row=refine_upper_enamel(image,points,donor,points,jaw_transform(points,points),.85)
        self.assertTrue(row['replaced'])
        np.testing.assert_array_equal(image[70:],before[70:])
        dark=before.max(2)<125
        np.testing.assert_array_equal(image[dark],before[dark])

    def test_upper_row_follows_existing_enamel_without_importing_black_gap(self):
        points,donor=fixture();current=points.copy();current[list(INNER[:11]),1]-=4
        image=np.full_like(donor,45);image[59:63,49:79]=160
        row=refine_upper_enamel(image,current,donor,points,jaw_transform(current,points),.85)
        self.assertTrue(row['replaced'])
        self.assertAlmostEqual(row['enamel_alignment_y_px'],-4,delta=.5)
        self.assertGreaterEqual(int(image[60,59].min()),160)

    def test_closed_mouth_is_unchanged(self):
        points,donor=fixture();points[list(INNER),1]=70
        image=np.full_like(donor,140);before=image.copy()
        row=refine_upper_enamel(image,points,donor,points,jaw_transform(points,points),.85)
        self.assertFalse(row['replaced'])
        np.testing.assert_array_equal(image,before)

    def test_only_lower_row_visible_does_not_trigger_upper_replacement(self):
        points,donor=fixture();image=np.full_like(donor,45);image[74:77,49:79]=200;before=image.copy()
        row=refine_upper_enamel(image,points,donor,points,jaw_transform(points,points),.85)
        self.assertFalse(row['replaced'])
        np.testing.assert_array_equal(image,before)

    def test_stable_mode_bounds_brightness_correction_and_preserves_dark_pixels(self):
        points,donor=fixture();image=np.full_like(donor,45);image[63:67,49:79]=160;before=image.copy()
        row=refine_upper_enamel(image,points,donor,points,jaw_transform(points,points),.6,state={})
        self.assertTrue(row['replaced'])
        self.assertLessEqual(int(np.abs(image.astype(np.int16)-before).max()),6)
        np.testing.assert_array_equal(image[before.max(2)<=125],before[before.max(2)<=125])

    def test_stable_alignment_does_not_jump_with_noisy_bright_row(self):
        points,donor=fixture();state={}
        first=np.full_like(donor,45);first[63:67,49:79]=160
        a=refine_upper_enamel(first,points,donor,points,jaw_transform(points,points),.6,state=state)
        next_image=np.full_like(donor,45);next_image[66:69,49:79]=160
        b=refine_upper_enamel(next_image,points,donor,points,jaw_transform(points,points),.6,state=state)
        self.assertLessEqual(abs(b['enamel_alignment_y_px']-a['enamel_alignment_y_px']),70*.012+.001)

    def test_stable_mode_does_not_leave_teeth_on_closed_lips(self):
        points,donor=fixture();state={};image=np.full_like(donor,45);image[63:67,49:79]=160
        refine_upper_enamel(image,points,donor,points,jaw_transform(points,points),.6,state=state)
        closed=points.copy();closed[list(INNER),1]=70;image=np.full_like(donor,140);before=image.copy()
        row=refine_upper_enamel(image,closed,donor,points,jaw_transform(closed,points),.6,state=state)
        self.assertFalse(row['replaced']);np.testing.assert_array_equal(image,before)


if __name__=='__main__':unittest.main()
