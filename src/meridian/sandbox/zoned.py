"""One Executor that picks, per committed policy, where a run happens ([runtime] executors).

Zones with a network rule (rbqm.narrator, safety.local_model) stay on OpenShell, whose
proxy enforces per-binary, per-method and per-path rules; zones without network may run
on Agent Substrate (docs/substrate-design.md, option C). A policy not listed runs on the
default executor. The choice is checked when the executor is built: substrate is refused
for any policy with a network rule or a provider.
"""

from collections.abc import Mapping

from meridian.domain.events import Event
from meridian.sandbox.executor import ExecutionError, ExecutionResult, Executor, RunPlan
from meridian.sandbox.policies import ALL_POLICIES, policy_for_agent
from meridian.sandbox.substrate_templates import substrate_eligible

ZONED = "zoned"


class ZonedExecutor:
    name = ZONED

    def __init__(
        self, *, executors: Mapping[str, Executor], choice: Mapping[str, str], default: str
    ) -> None:
        for policy, executor in choice.items():
            if policy not in ALL_POLICIES:
                raise ExecutionError(f"[runtime] executors names unknown policy {policy}")
            if executor not in executors:
                raise ExecutionError(f"no {executor} executor is configured for {policy}")
            if executor == "substrate" and not substrate_eligible(policy):
                raise ExecutionError(f"{policy} has network rules and may not run in substrate")
        if default not in executors:
            raise ExecutionError(f"no {default} executor is configured")
        self._executors = dict(executors)
        self._choice = dict(choice)
        self._default = default

    def executor_name(self, agent_id: str) -> str:
        return self._choice.get(policy_for_agent(agent_id), self._default)

    def executor_for(self, agent_id: str) -> Executor:
        return self._executors[self.executor_name(agent_id)]

    def plan(self, agent_id: str, trigger: Event) -> RunPlan:
        return self.executor_for(agent_id).plan(agent_id, trigger)

    def capacity(self, executor: str) -> int:
        return self._executors[executor].capacity(executor)

    def run(
        self, agent_id: str, trigger: Event, cut_id: str, plan: RunPlan | None = None
    ) -> ExecutionResult:
        executor = self.executor_for(agent_id)
        if plan is not None and plan.executor != executor.name:
            raise ExecutionError(f"{agent_id} was planned for {plan.executor}, not {executor.name}")
        return executor.run(agent_id, trigger, cut_id, plan)

    def drain(self) -> None:
        for executor in self._executors.values():
            executor.drain()
