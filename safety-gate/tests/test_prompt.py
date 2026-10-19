from gate_server.prompt import SYSTEM_PROMPT, build_user_text


def test_system_prompt_has_four_fields():
    for key in ("authorization", "risk_level", "risk_factors", "rationale"):
        assert key in SYSTEM_PROMPT


def test_build_user_text_format():
    text = build_user_text(
        "Pay the bill",
        [{"role": "user", "content": "hello"}],
        {"function": "send_money", "args": {"amount": 1}},
    )
    assert text.startswith("User task: Pay the bill")
    assert "Conversation history:" in text
    assert "user: hello" in text
    assert 'send_money({"amount": 1})' in text

