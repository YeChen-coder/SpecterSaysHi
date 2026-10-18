"""Upper teeth stay rigid while current lips occlude them, without new audio."""
import sys,unittest
from pathlib import Path
import numpy as np
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'feathertalk_lab'))
from rigid_upper_jaw import RigidUpperJaw
from teeth_geometry import INNER


def fixture():
    points=np.zeros((478,2),np.float32);points[33],points[263],points[168]=(32,20),(96,20),(64,35)
    a=np.linspace(np.pi,0,11);upper=np.column_stack((64+35*np.cos(a),70-9*np.sin(a)))
    a=np.linspace(0,np.pi,11)[1:-1];lower=np.column_stack((64+35*np.cos(a),70+9*np.sin(a)))
    points[list(INNER)]=np.concatenate((upper,lower));donor=np.full((128,128,3),20,np.uint8)
    for x in [49,57,65,73]:donor[63:67,x:x+6]=220
    return points,donor


class RigidUpperJawTests(unittest.TestCase):
    def setUp(self):
        self.points,self.donor=fixture()
        self.jaw=RigidUpperJaw(self.donor,self.points,[self.points],(20,20,20),dict(lip_limit=0))

    def test_generated_tooth_brightness_does_not_gate_reference(self):
        dark=np.full_like(self.donor,20);bright=dark.copy();bright[62:74,45:82]=230
        self.jaw.process(dark,self.points,0);self.jaw.process(bright,self.points,0)
        self.assertGreater(int(dark[69,67].min()),125)
        np.testing.assert_array_equal(dark[68:71,66:70],bright[68:71,66:70])

    def test_generated_tooth_edge_does_not_move_fixed_row(self):
        first=np.full_like(self.donor,20);second=first.copy();first[62:65,49:79]=200;second[67:72,49:79]=200
        self.jaw.process(first,self.points,0);self.jaw.process(second,self.points,0)
        np.testing.assert_array_equal(first[65:72,50:78],second[65:72,50:78])

    def test_lower_highlights_are_not_imported_as_fixed_teeth(self):
        donor=self.donor.copy();donor[69:71,62:65]=240
        jaw=RigidUpperJaw(donor,self.points,[self.points],(20,20,20),dict(lip_limit=0))
        self.assertLess(float(jaw.mask[70,63]),.01)
        self.assertGreater(float(jaw.mask[64,60]),.5)

    def test_lower_jaw_pixels_are_not_filled_with_upper_cavity(self):
        image=np.full_like(self.donor,20);image[76:79,50:78]=(190,180,170);before=image.copy()
        self.jaw.process(image,self.points,0)
        np.testing.assert_array_equal(image[76:],before[76:])

    def test_closed_lips_leave_every_pixel_unchanged(self):
        closed=self.points.copy();closed[list(INNER),1]=70;image=np.full_like(self.donor,140);before=image.copy()
        row=self.jaw.process(image,closed,0)
        self.assertTrue(row['closed']);np.testing.assert_array_equal(image,before)

    def test_head_translation_moves_dentition_without_scaling(self):
        moved=self.points+[7,3];jaw=RigidUpperJaw(self.donor,self.points,[self.points,moved],(20,20,20),dict(lip_limit=0))
        a=np.full((160,160,3),20,np.uint8);b=a.copy();jaw.process(a,self.points,0);jaw.process(b,moved,1)
        np.testing.assert_array_equal(a[55:80,40:90],b[58:83,47:97])

    def test_lip_alignment_is_bounded_and_preserves_lower_row(self):
        jaw=RigidUpperJaw(self.donor,self.points,[self.points],(20,20,20))
        points=self.points.copy();points[list(INNER[:11]),1]-=3
        image=np.full_like(self.donor,20);image[50:60,45:84]=(70,80,160);image[76:79,50:78]=(190,180,170);before=image.copy()
        row=jaw.process(image,points,0)
        self.assertGreater(row['lip_adjustment_max_px'],0)
        self.assertLessEqual(row['lip_adjustment_max_px'],70*.055+.001)
        np.testing.assert_array_equal(image[76:],before[76:])


if __name__=='__main__':unittest.main()
