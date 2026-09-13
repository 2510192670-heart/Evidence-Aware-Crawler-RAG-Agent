"""Deterministic multi-structure loopback fixtures (M4/M6 shared test targets).

This package is a *test and benchmark target only*. The production pipeline
never imports it, and nothing here may import the production pipeline, so that
fixtures stay an independent answer key instead of mirroring the code they are
meant to check.
"""
