#!/usr/bin/env python3
"""Explicit-approval boundary for real staging/production mutation."""
import argparse
import os

parser = argparse.ArgumentParser()
parser.add_argument("stage", choices=["staging", "production"])
stage = parser.parse_args().stage
key = "DIVECHA_ALLOW_STAGING" if stage == "staging" else "DIVECHA_ALLOW_PRODUCTION"
if os.environ.get(key) != "1":
    raise SystemExit(f"requires_permission:{key}=1")
print(f"PERMISSION_GRANTED {stage}; run the operator-specific preflight and cohort procedure")
