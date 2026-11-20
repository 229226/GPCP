"""Compatibility metadata for setuptools < 61 (the build host has 59.6.0).

The project metadata lives here instead of pyproject.toml [project] so both
old and new setuptools can install the package in editable mode.
"""
from setuptools import find_packages, setup

setup(
    name="wlbench",
    version="0.1.0",
    description="Hardware-agnostic wireless protocol benchmark workloads",
    packages=find_packages(where="src"),
    package_dir={"": "src"},
    python_requires=">=3.9",
    entry_points={"console_scripts": ["wlbench = wlbench.cli:main"]},
)
