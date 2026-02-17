"""
Statsmodels API Stub.

Provides minimal stubs for statsmodels imports used by IQE forecasting tests.
"""

import pytest


class OLS:
    """Stub for statsmodels OLS."""
    
    def __init__(self, *args, **kwargs):
        pass
    
    def fit(self):
        pytest.skip("Statsmodels OLS requires full statsmodels installation")


def add_constant(*args, **kwargs):
    """Stub for add_constant."""
    pytest.skip("Statsmodels add_constant requires full statsmodels installation")
