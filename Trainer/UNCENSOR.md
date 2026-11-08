# Chat model uncensoring

Open Trainer → **Uncensor a chat model**. Install the uncensor tools once, then enter a Hugging Face safetensors repo or a local model folder, choose Heretic or Abliteration, and queue the job. The original weights are unchanged. The resulting full Hugging Face model is saved in `~/AI/Trainer/data/output/<job id>/model/`.

Both methods use the shared GPU queue. They leave the running Llama model and Hermes process alone. The existing Llama model may occupy enough VRAM that a job fails; the job log will show the error. GGUF inputs are unsupported by these tools.

After a job completes, click **Test refusals** and enter 1–12 prompts. This queues deterministic generation against the saved model. The test job shows every prompt, full response, and whether a common refusal phrase appeared. This phrase check is a quick indicator, not a measure of answer quality or a guarantee that the model will always comply. The report is saved as `refusals.json` in the test job's output folder.

Heretic: https://github.com/p-e-w/heretic (AGPL-3.0)

Abliteration: https://github.com/Orion-zhen/abliteration (GPL-3.0)
