# Licensed under GNU GPL v3.0, same as this project.
"""srzone-v1: new research execution layer around the existing S/R detector.

This package is not the original author's strategy and not the s2bt project.
A-share hold-rate calibration is not an input. Live USD-M orders stay off
unless every explicit opt-in in ``config.validate_live`` passes.
"""

DESIGN_ID = "srzone-v1"

__all__ = ["DESIGN_ID"]
