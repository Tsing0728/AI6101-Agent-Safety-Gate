"""HFEngine：加载 train.py 产出的 checkpoint 做真实推理。"""

from __future__ import annotations

import json
from pathlib import Path

from .engine import Engine
from .prompt import SYSTEM_PROMPT, build_user_text
from .schemas import Authorization, ClassifierInput, GateOutput, RiskLevel


class HFEngine(Engine):
    def __init__(self, model_path: str, local_only: bool = False, device: str = "auto", quantize: str = "4bit"):
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        self.tokenizer = AutoTokenizer.from_pretrained(model_path, trust_remote_code=True, local_files_only=local_only)
        p = Path(model_path)
        quant_kwargs = {}
        if quantize in ("4bit", "8bit"):
            from transformers import BitsAndBytesConfig

            quant_kwargs["quantization_config"] = BitsAndBytesConfig(
                load_in_4bit=(quantize == "4bit"),
                load_in_8bit=(quantize == "8bit"),
                bnb_4bit_quant_type="nf4",
                bnb_4bit_compute_dtype=torch.bfloat16,
                bnb_4bit_use_double_quant=True,
            )
        if (p / "adapter_config.json").exists():
            from peft import PeftModel

            with open(p / "adapter_config.json", encoding="utf-8") as f:
                base = json.load(f)["base_model_name_or_path"]
            base_model = AutoModelForCausalLM.from_pretrained(
                base, device_map=device, local_files_only=local_only, **quant_kwargs
            )
            self.model = PeftModel.from_pretrained(base_model, model_path)
        else:
            self.model = AutoModelForCausalLM.from_pretrained(
                model_path, torch_dtype=torch.bfloat16, device_map=device, local_files_only=local_only
            )
        self.model.eval()

    def classify(self, inp: ClassifierInput) -> GateOutput:
        import torch

        user_text = build_user_text(inp.user_task, inp.history, inp.proposed_tool_call.model_dump(mode="json"))
        messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user_text}]
        prompt = self.tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        enc = self.tokenizer(prompt, return_tensors="pt").to(self.model.device)
        with torch.no_grad():
            out = self.model.generate(**enc, max_new_tokens=64, do_sample=False)
        text = self.tokenizer.decode(out[0][enc["input_ids"].shape[-1]:], skip_special_tokens=True).strip()
        data = self._parse_json(text)
        return GateOutput(
            authorization=Authorization(data["authorization"].strip().upper()),
            risk_level=RiskLevel(data["risk_level"].strip().upper()),
            risk_factors=data.get("risk_factors") or [],
            rationale=data.get("rationale") or "",
        )

    @staticmethod
    def _parse_json(text: str) -> dict:
        try:
            return json.loads(text)
        except Exception:
            start, end = text.find("{"), text.rfind("}")
            if 0 <= start < end:
                return json.loads(text[start:end + 1])
            raise

