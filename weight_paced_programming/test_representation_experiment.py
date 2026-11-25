import unittest
import numpy as np
import torch
import common
from representation_candidates import NAMES, LOW, HIGH, evaluate
from representation_experiment import Candidate, feedback, final_grids, validate

class RepresentationTests(unittest.TestCase):
    def test_finite_derivatives_on_domain_and_bounds(self):
        rng = np.random.default_rng(123)
        vectors = [LOW, HIGH, *rng.uniform(LOW, HIGH, (20,4))]
        for name in NAMES:
            for coeff in vectors:
                t=torch.linspace(0,20,401).reshape(-1,1).requires_grad_(True)
                x=evaluate(name,t,coeff)
                v=torch.autograd.grad(x,t,torch.ones_like(x),create_graph=True)[0]
                a=torch.autograd.grad(v,t,torch.ones_like(v))[0]
                self.assertTrue(torch.isfinite(x).all() and torch.isfinite(v).all() and torch.isfinite(a).all(),name)

    def test_original_and_common_loss(self):
        c=[1.9,.2,.3,.04]
        t=torch.tensor([[0.],[1.],[10.]])
        torch.testing.assert_close(evaluate('original',t,c),torch.cos(c[0]*t)*(1+c[1]*t)/(1+c[2]*t+c[3]*t*t))
        self.assertAlmostEqual(feedback('original',c)['loss'],float(common.losses(Candidate('original',c),'free')[0].detach()))

    def test_validation_and_final_grids(self):
        for invalid in ([1,2,3], [1,0,0,float('nan')], [1,0,0,-.01], [5,0,0,0], [True,0,0,0]):
            with self.assertRaises(ValueError): validate(invalid)
        a,b=final_grids(); c,d=final_grids()
        np.testing.assert_array_equal(a,c); np.testing.assert_array_equal(b,d)
        self.assertTrue((a>0).all() and (a<10).all() and (b>10).all() and (b<20).all())

if __name__=='__main__': unittest.main()
