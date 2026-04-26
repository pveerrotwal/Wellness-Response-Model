
import argparse
import json
import os
from pathlib import Path

import torch
from transformers import AutoTokenizer, AutoModelForCausalLM
from peft import PeftModel

ROOT = Path(__file__).resolve().parents[1]
BASE_MODEL = "Qwen/Qwen2.5-0.5B-Instruct"
ADAPTER_DIR = ROOT / "outputs" / "lora-wellness"
EVAL_FILE = ROOT / "data" / "eval_prompts.jsonl"
OUT_FILE = ROOT / "results" / "comparisons.md"

SYSTEM_PROMPT = (
    "You are a wellness coach. You respond with warmth and specificity, "
    "not as a generic assistant. You acknowledge feelings first, offer "
    "one concrete small step when useful, keep responses short, and "
    "redirect to real human help if someone is in crisis."
)

GEN_KWARGS = dict(
    max_new_tokens=180,
    do_sample=True,
    temperature=0.6,
    top_p=0.9,
    repetition_penalty=1.15,
)


def pick_device():
    if torch.backends.mps.is_available():
        return "mps"
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


def load_models(device: str):
    """Load tokenizer + base model + fine-tuned (adapter on top of base).

    We keep two model objects so we can switch between them without
    reloading. On 8GB this is tight but workable for 1.1B at fp32 -
    roughly 4.4GB per copy. If memory pressure is a problem, call
    load_models_sequential() instead (slower but lower peak).
    """
    print(f"loading tokenizer...")
    tokenizer = AutoTokenizer.from_pretrained(BASE_MODEL)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    # fp32 to match training. Inference-only could go bf16 to save memory,
    # but consistency with training keeps the comparison clean.
    dtype = torch.float32

    print(f"loading base model -> {device} ({dtype})...")
    base = AutoModelForCausalLM.from_pretrained(
        BASE_MODEL, torch_dtype=dtype
    ).to(device)
    base.eval()

    print(f"loading fine-tuned (base + LoRA adapter) -> {device}...")
    ft_base = AutoModelForCausalLM.from_pretrained(
        BASE_MODEL, torch_dtype=dtype
    )
    ft = PeftModel.from_pretrained(ft_base, str(ADAPTER_DIR))
    ft = ft.to(device)
    ft.eval()

    return tokenizer, base, ft


def generate(model, tokenizer, prompt: str, device: str, seed: int = 17) -> str:
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]
    input_ids = tokenizer.apply_chat_template(
        messages, add_generation_prompt=True, return_tensors="pt"
    ).to(device)

    torch.manual_seed(seed)
    if device == "mps":
        torch.mps.manual_seed(seed)

    with torch.no_grad():
        out = model.generate(
            input_ids,
            pad_token_id=tokenizer.eos_token_id,
            **GEN_KWARGS,
        )
    # Slice off the input tokens so we only decode the new completion.
    new_tokens = out[0, input_ids.shape[1]:]
    text = tokenizer.decode(new_tokens, skip_special_tokens=True)
    return text.strip()


def eval_mode():
    device = pick_device()
    tokenizer, base, ft = load_models(device)

    prompts = []
    with open(EVAL_FILE) as f:
        for line in f:
            prompts.append(json.loads(line)["prompt"])

    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Base vs Fine-Tuned: Side-by-Side",
        "",
        f"**Base model:** `{BASE_MODEL}`  ",
        f"**Fine-tuned:** base + LoRA adapter at `outputs/lora-wellness/`  ",
        f"**Decoding:** temperature={GEN_KWARGS['temperature']}, "
        f"top_p={GEN_KWARGS['top_p']}, "
        f"rep_penalty={GEN_KWARGS['repetition_penalty']}, "
        f"max_new_tokens={GEN_KWARGS['max_new_tokens']}, "
        f"seed=17 (same for both)",
        "",
        "These 20 prompts are held-out - not in the training set.",
        "",
    ]

    for i, prompt in enumerate(prompts, 1):
        print(f"[{i}/{len(prompts)}] {prompt!r}")
        base_out = generate(base, tokenizer, prompt, device)
        ft_out = generate(ft, tokenizer, prompt, device)
        lines.extend([
            f"## {i}. {prompt}",
            "",
            "**Base (Qwen2.5-0.5B-Instruct):**",
            "",
            "> " + base_out.replace("\n", "\n> "),
            "",
            "**Fine-tuned (wellness LoRA):**",
            "",
            "> " + ft_out.replace("\n", "\n> "),
            "",
            "---",
            "",
        ])

    OUT_FILE.write_text("\n".join(lines))
    print(f"wrote {OUT_FILE.relative_to(ROOT)}")


def interactive_mode():
    device = pick_device()
    tokenizer, base, ft = load_models(device)
    print("\ninteractive mode. empty line to quit.\n")
    while True:
        try:
            prompt = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not prompt:
            break
        base_out = generate(base, tokenizer, prompt, device)
        ft_out = generate(ft, tokenizer, prompt, device)
        print(f"\nBASE:\n  {base_out}\n")
        print(f"FINE-TUNED:\n  {ft_out}\n")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--eval", action="store_true", help="run eval prompts and write markdown")
    ap.add_argument("--interactive", action="store_true", help="interactive comparison CLI")
    args = ap.parse_args()
    if args.interactive:
        interactive_mode()
    else:
        # default = eval
        eval_mode()
