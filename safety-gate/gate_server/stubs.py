"""规则桩：模型未训好前的端到端验证。"""

from __future__ import annotations

from .engine import Engine
from .schemas import Authorization, ClassifierInput, GateOutput, RiskLevel


class StubEngine(Engine):
    def classify(self, inp: ClassifierInput) -> GateOutput:
        meta = inp.tool_metadata
        if meta.read_only:
            return GateOutput(
                authorization=Authorization.AUTHORIZED,
                risk_level=RiskLevel.LOW,
                risk_factors=[],
                rationale="Read-only tool.",
            )
        if meta.touches_remote:
            return GateOutput(
                authorization=Authorization.UNAUTHORIZED,
                risk_level=RiskLevel.HIGH,
                risk_factors=["external_side_effect"],
                rationale="Tool has remote side effects.",
            )
        return GateOutput(
            authorization=Authorization.UNCERTAIN,
            risk_level=RiskLevel.LOW,
            risk_factors=[],
            rationale="Write tool; needs review.",
        )

