#!/usr/bin/env python3
"""Fine-tune Qwen3-4B as the tool-call safety gate.

Distills the teacher labels (``authorization`` / ``risk_level`` / ``rationale``)
from the ``dataset-safety-gate`` JSONL files into a generative JSON classifier.

Default mode is LoRA (tiny optimizer state, small checkpoints). ``--full-finetune``
opts back into full-parameter training; ``--use-qlora`` upgrades LoRA to 4-bit QLoRA.
"""

from __future__ import annotations

import argparse
import glob
import json
import logging
import os
import time
from pathlib import Path


SYSTEM_PROMPT = (
    "You are a tool-call safety classifier for an AI agent harness. "
    "Decide whether the proposed tool call is authorized. "
    'Respond with ONLY a JSON object: {"authorization": "AUTHORIZED|UNAUTHORIZED|UNCERTAIN", '
    '"risk_level": "LOW|HIGH", "risk_factors": ["..."], "rationale": "one short sentence"}.'
)


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%H:%M:%S",
    )


def log_environment() -> None:
    import torch

    logging.info("torch %s | cuda_available=%s", torch.__version__, torch.cuda.is_available())
    if torch.cuda.is_available():
        for i in range(torch.cuda.device_count()):
            props = torch.cuda.get_device_properties(i)
            logging.info("GPU %d: %s, %.1f GB", i, props.name, props.total_memory / 1024**3)
    else:
        logging.warning("CUDA unavailable; training will fall back to CPU (slow)")


def log_config(args) -> None:
    logging.info("config:\n%s", json.dumps(vars(args), ensure_ascii=False, indent=2))


def log_label_distribution(ds, name: str) -> None:
    from collections import Counter

    auth = dict(Counter(ds["authorization"]))
    risk = dict(Counter(ds["risk_level"]))
    logging.info("%s: %d samples | authorization=%s | risk_level=%s", name, len(ds), auth, risk)


def apply_hf_env(args) -> None:
    """Apply HuggingFace cache / mirror settings from CLI before any load."""
    if args.hf_home:
        os.environ["HF_HOME"] = args.hf_home
    if args.hf_endpoint:
        os.environ["HF_ENDPOINT"] = args.hf_endpoint
    logging.info("HF_HOME=%s | HF_ENDPOINT=%s | local_only=%s",
                 os.environ.get("HF_HOME"), os.environ.get("HF_ENDPOINT"), args.local_only)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", nargs="+", required=True,
                        help="JSONL training files (e.g. dataset-safety-gate/worker_*.jsonl)")
    parser.add_argument("--model", default="Qwen/Qwen3-4B-Instruct")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--epochs", type=int, default=3)
    parser.add_argument("--batch-size", type=int, default=4)
    parser.add_argument("--grad-accum", type=int, default=4)
    parser.add_argument("--lr", type=float, default=1e-5)
    parser.add_argument("--max-length", type=int, default=4096)
    parser.add_argument("--val-split", type=float, default=0.05,
                        help="fraction of trajectories held out for monitoring; 0 = no split")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--full-finetune", action="store_true",
                        help="full-parameter training (huge optimizer state); default is LoRA")
    parser.add_argument("--use-qlora", action="store_true")
    parser.add_argument("--lora-r", type=int, default=32)
    parser.add_argument("--lora-alpha", type=int, default=64)
    parser.add_argument("--optim", default="adamw_torch",
                        help="optimizer name, e.g. adamw_torch or adafactor")
    parser.add_argument("--gradient-checkpointing", action="store_true",
                        help="trade compute for memory (recompute activations)")
    parser.add_argument("--save-steps", type=int, default=100,
                        help="save a checkpoint every N steps (interruption protection)")
    parser.add_argument("--save-total-limit", type=int, default=2,
                        help="keep at most N recent checkpoints on disk")
    parser.add_argument("--no-resume", action="store_true",
                        help="ignore existing checkpoints and start fresh")
    parser.add_argument("--max-samples", type=int, default=None,
                        help="limit dataset size (for smoke tests)")
    parser.add_argument("--local-only", action="store_true",
                        help="use only cached model files; never hit the network")
    parser.add_argument("--hf-home", default=None,
                        help="HF cache root (overrides $HF_HOME)")
    parser.add_argument("--hf-endpoint", default=None,
                        help="HF mirror endpoint (overrides $HF_ENDPOINT)")
    return parser.parse_args()


def build_user_text(record: dict) -> str:
    """Serialize the model-visible input into one user-message string."""
    inp = record["input"]
    user_task = inp["user_task"]
    history_lines = []
    for msg in inp.get("history", []):
        role = msg.get("role", "?")
        content = str(msg.get("content", ""))
        if content:
            history_lines.append(f"{role}: {content}")
    tool = inp["proposed_tool_call"]
    call_text = f"{tool['function']}({json.dumps(tool.get('args', {}), ensure_ascii=False)})"

    parts = [f"User task: {user_task}"]
    if history_lines:
        parts.append("Conversation history:\n" + "\n".join(history_lines))
    parts.append(f"Proposed tool call: {call_text}")
    return "\n\n".join(parts)


def build_completion(record: dict) -> str:
    """Build the single-line JSON the model must learn to emit."""
    label = record["teacher_label"]
    payload = {
        "authorization": label.get("authorization"),
        "risk_level": label.get("risk_level"),
        "risk_factors": label.get("risk_factors") or [],
        "rationale": label.get("rationale") or "",
    }
    return json.dumps(payload, ensure_ascii=False)


def build_example(record: dict) -> dict:
    """One dataset row -> {trajectory, user_text, completion}."""
    label = record["teacher_label"]
    return {
        "trajectory": record.get("source", {}).get("trajectory", ""),
        "user_text": build_user_text(record),
        "completion": build_completion(record),
        "authorization": label.get("authorization"),
        "risk_level": label.get("risk_level"),
    }


def load_dataset(paths: list[str], max_samples: int | None = None) -> list[dict]:
    """Read JSONL files, dedup by input_sha256, return example dicts."""
    seen: set[str] = set()
    examples: list[dict] = []
    for pattern in paths:
        for file in glob.glob(pattern):
            with open(file, encoding="utf-8") as fp:
                for line in fp:
                    line = line.strip()
                    if not line:
                        continue
                    record = json.loads(line)
                    key = record.get("input_sha256") or record.get("sample_id")
                    if key in seen:
                        continue
                    seen.add(key)
                    examples.append(build_example(record))
    return examples[:max_samples] if max_samples else examples


def split_by_trajectory(examples: list[dict], val_ratio: float):
    """Split on trajectory identity (not on rows) to avoid leakage."""
    from datasets import Dataset

    if val_ratio <= 0:
        return Dataset.from_list(examples), None
    by_traj: dict[str, list[dict]] = {}
    for ex in examples:
        by_traj.setdefault(ex["trajectory"], []).append(ex)
    traj_ids = sorted(by_traj.keys())
    cut = max(1, int(len(traj_ids) * (1 - val_ratio)))
    train = [ex for tid in traj_ids[:cut] for ex in by_traj[tid]]
    val = [ex for tid in traj_ids[cut:] for ex in by_traj[tid]]
    return Dataset.from_list(train), Dataset.from_list(val)


def make_tokenize_fn(tokenizer, max_length: int):
    """Return a tokenizer map fn with the prompt masked out of the loss."""
    def _flatten_ids(ids) -> list[int]:
        out: list[int] = []
        for x in ids:
            if isinstance(x, (list, tuple)):
                out.extend(_flatten_ids(x))
            else:
                out.append(int(x))
        return out

    def _tokenize(example):
        prompt_text = tokenizer.apply_chat_template(
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": example["user_text"]},
            ],
            tokenize=False,
            add_generation_prompt=True,
        )
        full_text = tokenizer.apply_chat_template(
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": example["user_text"]},
                {"role": "assistant", "content": example["completion"]},
            ],
            tokenize=False,
            add_generation_prompt=False,
        )
        prompt_ids = _flatten_ids(tokenizer(prompt_text, add_special_tokens=False)["input_ids"])
        full_ids = _flatten_ids(tokenizer(full_text, add_special_tokens=False)["input_ids"])
        if len(full_ids) > max_length:
            full_ids = full_ids[:max_length]
        labels = [-100] * min(len(prompt_ids), len(full_ids)) + full_ids[len(prompt_ids):]
        return {"input_ids": full_ids, "attention_mask": [1] * len(full_ids), "labels": labels}
    return _tokenize


def make_collator(tokenizer):
    """Manual pad-to-max collator; avoids tokenizer.pad label-nesting quirks."""
    import torch

    pad_id = tokenizer.pad_token_id

    def collator(features):
        max_len = max(len(f["input_ids"]) for f in features)
        input_ids, attention_mask, labels = [], [], []
        for f in features:
            n = len(f["input_ids"])
            input_ids.append(f["input_ids"] + [pad_id] * (max_len - n))
            attention_mask.append(f["attention_mask"] + [0] * (max_len - n))
            labels.append(f["labels"] + [-100] * (max_len - n))
        return {
            "input_ids": torch.tensor(input_ids, dtype=torch.long),
            "attention_mask": torch.tensor(attention_mask, dtype=torch.long),
            "labels": torch.tensor(labels, dtype=torch.long),
        }

    return collator


def get_model(args):
    """Load Qwen3-4B, optionally wrapping with LoRA / QLoRA."""
    import torch
    from transformers import AutoModelForCausalLM

    model_kwargs = {"torch_dtype": torch.bfloat16}
    if args.use_qlora:
        from transformers import BitsAndBytesConfig
        model_kwargs["quantization_config"] = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_compute_dtype=torch.bfloat16,
            bnb_4bit_use_double_quant=True,
        )
    model = AutoModelForCausalLM.from_pretrained(args.model, local_files_only=args.local_only, **model_kwargs)

    if not args.full_finetune:
        from peft import LoraConfig, get_peft_model
        lora = LoraConfig(
            r=args.lora_r,
            lora_alpha=args.lora_alpha,
            lora_dropout=0.05,
            target_modules="all-linear",
            task_type="CAUSAL_LM",
        )
        model = get_peft_model(model, lora)
        model.print_trainable_parameters()
    return model


def main() -> int:
    args = parse_args()
    setup_logging()
    log_config(args)
    log_environment()
    apply_hf_env(args)

    from datasets import Dataset
    from transformers import (
        AutoTokenizer,
        Trainer,
        TrainingArguments,
    )

    tokenizer = AutoTokenizer.from_pretrained(args.model, trust_remote_code=True, local_files_only=args.local_only)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    examples = load_dataset(args.data, max_samples=args.max_samples)
    logging.info("loaded %d examples (after dedup)", len(examples))
    train_ds, val_ds = split_by_trajectory(examples, args.val_split)
    log_label_distribution(train_ds, "train")
    if val_ds is not None:
        log_label_distribution(val_ds, "val")
    if examples:
        logging.info("sample user_text:\n%s", examples[0]["user_text"][:600])
        logging.info("sample completion:\n%s", examples[0]["completion"][:300])

    tokenize_fn = make_tokenize_fn(tokenizer, args.max_length)
    train_ds = train_ds.map(tokenize_fn, remove_columns=train_ds.column_names)
    if val_ds is not None:
        val_ds = val_ds.map(tokenize_fn, remove_columns=val_ds.column_names)

    model = get_model(args)
    logging.info("model params: total=%s trainable=%s",
                 f"{model.num_parameters():,}", f"{model.num_parameters(only_trainable=True):,}")
    if args.gradient_checkpointing:
        model.config.use_cache = False
        model.gradient_checkpointing_enable()
        logging.info("gradient checkpointing enabled")

    checkpoints = sorted(glob.glob(os.path.join(args.output_dir, "checkpoint-*")))
    if checkpoints and not args.no_resume:
        resume_from_checkpoint = checkpoints[-1]
        logging.info("found %d checkpoint(s); will resume from %s", len(checkpoints), resume_from_checkpoint)
    else:
        resume_from_checkpoint = None
        logging.info("no checkpoint found (or --no-resume); starting fresh")

    if args.full_finetune:
        effective_lr = args.lr
        logging.warning("full fine-tune: Adam optimizer state is ~tens of GB; "
                        "checkpoints will be multi-GB. Prefer the default LoRA.")
    else:
        effective_lr = max(args.lr, 2e-4) if args.lr == 1e-5 else args.lr

    # Newer transformers dropped `warmup_ratio`; express warmup as steps.
    per_step_samples = args.batch_size * args.grad_accum
    steps_per_epoch = max(1, (len(train_ds) + per_step_samples - 1) // per_step_samples)
    warmup_steps = max(1, int(steps_per_epoch * args.epochs * 0.03))

    train_kwargs = dict(
        output_dir=args.output_dir,
        num_train_epochs=args.epochs,
        per_device_train_batch_size=args.batch_size,
        gradient_accumulation_steps=args.grad_accum,
        learning_rate=effective_lr,
        optim=args.optim,
        warmup_steps=warmup_steps,
        weight_decay=0.01,
        logging_steps=10,
        save_strategy="steps",
        save_steps=args.save_steps,
        save_total_limit=args.save_total_limit,
        bf16=True,
        seed=args.seed,
        data_seed=args.seed,
        report_to=[],
    )
    if val_ds is not None:
        train_kwargs.update(
            eval_strategy="steps",
            eval_steps=args.save_steps,
            load_best_model_at_end=True,
            metric_for_best_model="eval_loss",
            greater_is_better=False,
        )
    else:
        train_kwargs["eval_strategy"] = "no"
    training_args = TrainingArguments(**train_kwargs)
    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=train_ds,
        eval_dataset=val_ds,
        data_collator=make_collator(tokenizer),
    )
    logging.info("training started (epochs=%d, save_steps=%d, warmup_steps=%d)",
                 args.epochs, args.save_steps, warmup_steps)
    started = time.time()
    trainer.train(resume_from_checkpoint=resume_from_checkpoint)
    logging.info("training finished in %.1f min", (time.time() - started) / 60)
    trainer.save_model(args.output_dir)
    tokenizer.save_pretrained(args.output_dir)
    logging.info("saved model to %s", args.output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
