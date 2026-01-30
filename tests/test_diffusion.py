import unittest
import math

class TestSmoothnessDiffusion(unittest.TestCase):
    def test_diffusion_gradient(self):
        """
        Simulates a strip of vertices from a sharp corner (Index 0) out to a flat face.
        Verifies that smoothing diffusion creates a gradient, preventing a cliff.
        """
        # Topology: 0 -- 1 -- 2 -- 3 -- 4
        # 0 is Corner (Sharp)
        # 1-4 are Face (Flat)
        
        # Initial Smoothness values
        # Corner starts at ~0.58 (sqrt(3)/3)
        # Face starts at 1.0
        smoothness = {
            0: 0.577, 
            1: 1.0, 
            2: 1.0, 
            3: 1.0, 
            4: 1.0
        }
        
        # Adjacency
        neighbors = {
            0: [1],
            1: [0, 2],
            2: [1, 3],
            3: [2, 4],
            4: [3]
        }
        
        print("\n--- Initial State ---")
        print_state(smoothness)
        
        # Cliff Check: Difference between 0 and 1
        cliff = smoothness[1] - smoothness[0]
        print(f"Initial Cliff (0-1): {cliff:.4f}")
        self.assertGreater(cliff, 0.4, "Expect large cliff initially")

        # --- Multi-Pass Diffusion ---
        # Simple averaging kernel
        passes = 3
        for p in range(passes):
            new_smoothness = smoothness.copy()
            for i in smoothness:
                # Gather neighbor values including self?
                # Usually Laplacian smoothing uses neighbors + self weight
                # Let's use simple average of neighbors (aggressive) or weighted matching self.
                # Average(Self, Neighbors...)
                
                vals = [smoothness[i]]
                for n in neighbors[i]:
                    vals.append(smoothness[n])
                
                avg = sum(vals) / len(vals)
                new_smoothness[i] = avg
            
            smoothness = new_smoothness
            print(f"\n--- Pass {p+1} ---")
            print_state(smoothness)

        # Final Cliff Check
        final_cliff = smoothness[1] - smoothness[0]
        print(f"Final Cliff (0-1): {final_cliff:.4f}")
        
        # Expect the cliff to be significantly reduced (e.g. < 0.2)
        # And the value at 1 should be lower than 1.0 (attenuation spread)
        self.assertLess(final_cliff, 0.2, "Cliff should be smoothed out")
        self.assertLess(smoothness[1], 0.95, "Vertex 1 should be attenuated")
        self.assertLess(smoothness[2], 0.99, "Vertex 2 should be slightly attenuated")

def print_state(s):
    line = " | ".join([f"{k}: {v:.3f}" for k,v in s.items()])
    print(line)

if __name__ == '__main__':
    unittest.main()
