"""评测域（ALN-10-E：真实宿主上的候选对照基准与收益/回归证据）。

本包只放「选择评测数据与证据形态」的消费端逻辑，不复制 ALN-10 已接受的
回归门禁（``evolution/regression_gate.py``）与十维收益评价
（``evolution/benefit.py``），也不改 ``evolution/`` 既有切片。
"""

from .host_bench import (BenchCase, BenchObservation, HostBenchmark,
                         benefit_chain, evidence_digest, release_decision)

__all__ = ["BenchCase", "BenchObservation", "HostBenchmark",
           "benefit_chain", "evidence_digest", "release_decision"]
