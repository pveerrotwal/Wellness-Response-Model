import gc
import os
import torch
from pathlib import Path

from datasets import load_dataset
from transformers import (
    AutoTokenizer, AutoModelForCausalLM,
    TrainerCallback,
)
from peft import LoraConfig, get_peft_model
from trl import SFTTrainer, SFTConfig


class MpsCacheCleanupCallback(TrainerCallback):
    """Empty MPS cache between epochs. On 8GB hardware, MPS's caching
    allocator can hold onto memory and trigger paging. We pay a small
    sync cost at epoch boundaries to keep the working set small."""

    def on_epoch_end(self, args, state, control, **kwargs):
        gc.collect()
        if torch.backends.mps.is_available():
            torch.mps.empty_cache()
        return control

ROOT = Path(__file__).resolve().parents[1]
BASE_MODEL = "Qwen/Qwen2.5-0.5B-Instruct"
OUTPUT_DIR = ROOT / "outputs" / "lora-wellness"
DATA_FILE = ROOT / "data" / "dataset.jsonl"


def main():
    # CPU rather than MPS. On 8GB M2 the MPS caching allocator
    # accumulates memory across steps and swaps to disk, killing throughput.
    # CPU at fp32 is slower per step but completely consistent. CUDA is
    # used if present (will not be on this machine but kept for portability).
    if torch.cuda.is_available():
        device = "cuda"
    else:
        device = "cpu"
    use_cpu = (device == "cpu")
    print(f"device: {device}")

    # Tokenizer first. Qwen2.5's tokenizer has a chat template baked in.
    tokenizer = AutoTokenizer.from_pretrained(BASE_MODEL)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token

    # Quick sanity: verify chat template exists and renders.
    probe_msgs = [
        {"role": "system", "content": "you are a coach"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hey"},
    ]
    rendered = tokenizer.apply_chat_template(probe_msgs, tokenize=False)
    print("chat template renders. sample (truncated):")
    print(rendered[:300])
    print("---")

    # fp32 throughout. Qwen 0.5B at fp32 is ~2GB which is fine in 8GB.
    # bf16 on MPS gave NaN gradients after a few steps - not worth fighting.
    print(f"loading base model: {BASE_MODEL}")
    model = AutoModelForCausalLM.from_pretrained(
        BASE_MODEL,
        torch_dtype=torch.float32,
    )
    model.config.use_cache = False

    # LoRA config. Target all linear projections in the attention and MLP
    # blocks. Qwen2 uses the same module names as Llama-style models.
    lora = LoraConfig(
        r=16,
        lora_alpha=32,
        lora_dropout=0.05,
        bias="none",
        task_type="CAUSAL_LM",
        target_modules=[
            "q_proj", "k_proj", "v_proj", "o_proj",
            "gate_proj", "up_proj", "down_proj",
        ],
    )
    model = get_peft_model(model, lora)
    trainable = sum(p.numel() for p in model.parameters() if p.requires_grad)
    total = sum(p.numel() for p in model.parameters())
    print(f"trainable params: {trainable:,} of {total:,} ({100*trainable/total:.2f}%)")

    # Dataset. TRL's SFTTrainer recognizes a "messages" column and will
    # apply the tokenizer's chat template automatically.
    ds = load_dataset("json", data_files=str(DATA_FILE), split="train")
    print(f"dataset size: {len(ds)}")

    cfg = SFTConfig(
        output_dir=str(OUTPUT_DIR),
        num_train_epochs=3,
        per_device_train_batch_size=1,
        gradient_accumulation_steps=4,
        learning_rate=2e-4,
        lr_scheduler_type="cosine",
        warmup_ratio=0.03,
        weight_decay=0.0,
        # Actual max in our dataset is 158 tokens. 192 covers everything.
        max_seq_length=192,
        packing=False,
        logging_steps=2,
        # Save often. CPU runs are slow enough that losing progress hurts.
        save_strategy="steps",
        save_steps=20,
        save_total_limit=2,
        report_to="none",
        bf16=False,
        fp16=False,
        seed=17,
        dataloader_num_workers=0,
        gradient_checkpointing=False,
        # Force HF Trainer to use CPU (not MPS).
        use_cpu=use_cpu,
    )

    trainer = SFTTrainer(
        model=model,
        tokenizer=tokenizer,
        args=cfg,
        train_dataset=ds,
        callbacks=[MpsCacheCleanupCallback()],
    )

    print("starting training...")
    trainer.train()
    print("training done.")

    # Save LoRA adapter (not full model - we merge-on-load at inference time).
    trainer.save_model(str(OUTPUT_DIR))
    tokenizer.save_pretrained(str(OUTPUT_DIR))
    print(f"saved adapter + tokenizer to {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
