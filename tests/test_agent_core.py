import unittest
from system.agent_core import AGENT_SPECS, TOOL_REGISTRY, validate_contracts
class Contracts(unittest.TestCase):
    def test_contracts(self):
        self.assertEqual(len(AGENT_SPECS),8)
        self.assertEqual(validate_contracts(),[])
        self.assertNotIn('git.commit',AGENT_SPECS['Mercury']['tools'])
if __name__=='__main__': unittest.main()
