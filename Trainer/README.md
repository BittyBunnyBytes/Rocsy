# BunnyBytes Trainer

Trainer is the ROCm-only LoRA training service linked from the BunnyBytes
gateway at `/Trainer/`. It shares the host GPU queue with the generation apps.

Start it on demand from `~/AI/BunnyBytes`:

```sh
docker compose --profile trainer up -d --build trainer
```

The first build downloads a large AMD ROCm PyTorch image. Open the Trainer tile
after the container is healthy, install the needed backend from the page, and
put datasets in `~/AI/BunnyBytes/Datasets/<dataset-name>/` (or use the Datasets tab; `~/AI/Trainer/datasets` is a symlink to it). Outputs and the Qwen
environment/cache persist in `~/AI/Trainer/data/`; Fizgig and AI Toolkit
environments are installed beside their sources under `~/AI/Model Trainers/`.

Krea 2 and MiniMax H3 use same-stem `.txt` captions (for example
`photo.jpg` + `photo.txt`). Qwen Image 2.1 uses `metadata.csv` with `image` and
`prompt` columns. LTX 2.5 uses short `.mp4`/`.webm` clips with same-stem `.txt`
captions; use a frame count of 8n+1. Model files are selected from the shared
ComfyUI models folder, mounted read-only at `/models`.

The service requires `/dev/kfd` and `/dev/dri` and refuses non-ROCm training.
MiniMax H3 and LTX 2.5 are included for a future higher-VRAM AMD GPU; they may
not fit the current card. Qwen may download model files into the persistent
local Hugging Face cache the first time it runs.
