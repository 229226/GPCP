"""wlbench - hardware-agnostic wireless protocol benchmark workloads.

The package describes *what* a protocol workload does and *what deadline* it
must meet.  It never runs on hardware and never describes an architecture.
Hardware projects consume a workload bundle and report clock_hz / cycles /
correct; wlbench decides whether the protocol timing requirement was met.
"""

__version__ = "0.1.0"
