import json

from gate_server.schemas import Authorization, GateOutput, RiskLevel


def test_gate_output_content_contains_four_fields():
    g = GateOutput(
        authorization=Authorization.UNAUTHORIZED,
        risk_level=RiskLevel.HIGH,
        risk_factors=["external_side_effect"],
        rationale="reason",
    )
    content = g.to_content()
    data = json.loads(content)
    assert data["authorization"] == "UNAUTHORIZED"
    assert data["risk_level"] == "HIGH"
    assert data["risk_factors"] == ["external_side_effect"]
    assert data["rationale"] == "reason"

