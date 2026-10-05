"""下載語音辨識模型，並轉成 faster-whisper（CTranslate2）可用的格式。

用法（在專案根目錄執行）:
    python setup_models.py large-v3 breeze-25 breeze-26
    python setup_models.py breeze-26 --models-dir D:/models
    python setup_models.py breeze-25 --quantization int8   # 檔案小一半，準確度相當

模型會放在 <models-dir>/<名稱>-ct2/。Breeze 系列需要 torch 與 transformers 才能轉檔：
    pip install torch --index-url https://download.pytorch.org/whl/cpu
    pip install transformers
"""
import argparse
import os
import shutil
import sys
from pathlib import Path

# 只下載推論需要的檔案，略過 optimizer.bin 等訓練用檔案
HF_FILES = ["*.json", "*.txt", "*.safetensors"]

MODELS = {
    # 名稱: (Hugging Face repo, 是否已是 CTranslate2 格式)
    "large-v3": ("Systran/faster-whisper-large-v3", True),
    "breeze-25": ("MediaTek-Research/Breeze-ASR-25", False),
    "breeze-26": ("MediaTek-Research/Breeze-ASR-26", False),
}
# 現成的 CTranslate2 版只有 float16；要 int8 就從原始權重轉
ORIGINALS = {"large-v3": "openai/whisper-large-v3"}


def default_models_dir() -> Path:
    return Path(os.environ.get("AUTOEDIT_MODELS", Path.cwd() / "models"))


def download(repo: str, dest: Path, patterns=None) -> Path:
    from huggingface_hub import snapshot_download

    print(f"下載 {repo} → {dest}", flush=True)
    # whisper 的原始 repo 同時放了 fp16 與 fp32 兩份權重，只要 fp16 那份
    snapshot_download(repo_id=repo, local_dir=dest, allow_patterns=patterns, ignore_patterns=["*fp32*"])
    return dest


def convert(src: Path, out: Path, quantization: str = "float16") -> None:
    import ctranslate2
    from transformers import WhisperTokenizerFast

    print(f"轉檔 {src.name} → {out}", flush=True)
    # faster-whisper 需要 tokenizer.json；部分 repo 只有 vocab.json/merges.txt，這裡補產生
    if not (src / "tokenizer.json").exists():
        WhisperTokenizerFast.from_pretrained(src).save_pretrained(src)
    copy_files = [f for f in ("tokenizer.json", "preprocessor_config.json") if (src / f).exists()]
    converter = ctranslate2.converters.TransformersConverter(
        str(src), copy_files=copy_files, load_as_float16=True
    )
    converter.convert(str(out), quantization=quantization, force=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("names", nargs="+", choices=sorted(MODELS))
    parser.add_argument("--models-dir", type=Path, default=default_models_dir())
    parser.add_argument("--keep-hf", action="store_true", help="轉檔後保留原始 Hugging Face 權重")
    parser.add_argument("--quantization", choices=["float16", "int8"], default="float16",
                        help="存檔精度。辨識本來就用 int8 計算，存 int8 檔案小一半；breeze-25、large-v3 準確度相當，"
                             "breeze-26 在吵雜台語素材上約差 2 個百分點，建議維持 float16")
    args = parser.parse_args()

    for name in args.names:
        repo, is_ct2 = MODELS[name]
        if is_ct2 and args.quantization != "float16":
            repo, is_ct2 = ORIGINALS[name], False
        out = args.models_dir / f"{name}-ct2"
        if (out / "model.bin").exists():
            print(f"{name}: 已存在，略過", flush=True)
            continue
        if is_ct2:
            download(repo, out)
            continue
        hf_dir = download(repo, args.models_dir / "hf" / name, HF_FILES)
        convert(hf_dir, out, args.quantization)
        if not args.keep_hf:
            shutil.rmtree(hf_dir)
        print(f"{name}: 完成", flush=True)


if __name__ == "__main__":
    sys.exit(main())
