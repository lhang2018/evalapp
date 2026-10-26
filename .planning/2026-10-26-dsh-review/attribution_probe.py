import evoagent.release.manager as manager
manager._SUPPORTED_PLATFORMS = ("android", "ios")
import pytest
code = pytest.main(["-q", "tests/test_release_signing.py", "-x", "--tb=no"])
print("pytest_exit:", int(code))
