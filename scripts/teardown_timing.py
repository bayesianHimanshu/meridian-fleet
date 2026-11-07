"""Time 3 agents on one cut through the OpenShell executor (nothing goes to the ledger).

Per run: the critical path, from executor.run() called to drafts returned. Per pass: wall
time from the first run to the last sandbox deleted (the executor's drain(), when it has
one, is part of the pass).

    uv run python scripts/teardown_timing.py [--cut DC-BASE] [--label NAME]
"""

import argparse
import time

import structlog

from meridian.platform.config import Settings
from meridian.platform.container import Container
from meridian.sandbox.verify import trigger_for


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cut", default="DC-BASE")
    parser.add_argument("--label", default="")
    args = parser.parse_args()
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(30))
    log = structlog.get_logger()
    with Container.build(Settings()) as c:
        executor = c.openshell_executor(log)
        trigger = trigger_for(c.ledger, c.settings.study_id, args.cut)
        start = time.perf_counter()
        runs = []
        for registered in c.agent_registry().enabled():
            t0 = time.perf_counter()
            executor.run(registered.agent.agent_id, trigger, args.cut)
            runs.append((registered.agent.agent_id, (time.perf_counter() - t0) * 1000))
        drain = getattr(executor, "drain", None)
        if callable(drain):
            drain()
        wall = (time.perf_counter() - start) * 1000
    print(f"## {args.label or 'run'} on {args.cut}")
    for agent_id, ms in runs:
        print(f"* {agent_id}: critical path {ms:.0f} ms")
    print(f"* mean critical path {sum(ms for _, ms in runs) / len(runs):.0f} ms")
    print(f"* pass wall time {wall:.0f} ms")


if __name__ == "__main__":
    main()
