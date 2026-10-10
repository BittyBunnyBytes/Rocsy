# Rocsy

Rocsy came from my own frustration trying to get MiniMax H3 running well on a Radeon RX 7900 XTX.

I spent about a month dealing with ROCm setup, compatibility problems, and performance tweaks before I had something I was happy with. Even after that, I was still spending too much time managing ComfyUI and different workloads instead of actually using the machine.

Rocsy is my attempt to make that whole process simpler. It handles setup and optimization, and it includes a GPU queue so image, video, music, voice, and LLM jobs can all share the same hardware without stepping on each other.

The main goal is to make powerful local AI more practical for one person with one machine. You should be able to queue up what you want to run, leave it alone, and let Rocsy handle the messy parts in the background.
