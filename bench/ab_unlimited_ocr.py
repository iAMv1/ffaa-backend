"""A/B: baidu/Unlimited-OCR (the bar) vs our pipeline on one real invoice page.

Runs the 3B VLM on CPU (slow, ~5-15 min/page) — one page only.
Outputs its raw parse so we can compare against our structured fields.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fitz

# render page 1 of the Azure invoice at 300 DPI (same doc we extract in 0.05s)
PDF = os.path.join(os.path.dirname(__file__), "real", "invoice.pdf")
IMG = os.path.join(os.environ.get("TEMP", "."), "unl_ocr_p1.png")
doc = fitz.open(PDF)
pix = doc[0].get_pixmap(matrix=fitz.Matrix(300 / 72, 300 / 72))
pix.save(IMG)
doc.close()
print("rendered page 1 ->", IMG, flush=True)

t0 = time.time()
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

from transformers import AutoModel, AutoTokenizer  # noqa: E402

print("loading model (bf16, CPU) — first download ~6GB...", flush=True)
tok = AutoTokenizer.from_pretrained("baidu/Unlimited-OCR", trust_remote_code=True)
model = AutoModel.from_pretrained(
    "baidu/Unlimited-OCR",
    trust_remote_code=True,
    use_safetensors=True,
    torch_dtype="bfloat16",
)
model = model.eval()
print("model loaded", round(time.time() - t0, 1), "s", flush=True)

t1 = time.time()
out_dir = os.path.join(os.path.dirname(__file__), "unl_ocr_out")
model.infer(
    tok,
    prompt="<image>document parsing.",
    image_file=IMG,
    output_path=out_dir,
    base_size=1024,
    image_size=640,
    crop_mode=True,
    max_length=8192,
    no_repeat_ngram_size=35,
    ngram_window=128,
    save_results=True,
)
print("inference", round(time.time() - t1, 1), "s", flush=True)
for f in os.listdir(out_dir):
    print("out file:", f, flush=True)
    p = os.path.join(out_dir, f)
    if os.path.isfile(p) and p.endswith((".txt", ".md", ".json")):
        print(open(p, encoding="utf-8", errors="replace").read()[:3000], flush=True)
