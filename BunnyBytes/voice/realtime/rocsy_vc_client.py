#!/usr/bin/env python3
"""Rocsy real-time voice changer client: your microphone -> Rocsy -> a virtual microphone for games.

    pip install sounddevice numpy websockets
    python3 rocsy_vc_client.py --list-voices
    python3 rocsy_vc_client.py --voice Bitty --virtual-mic

On Linux (PipeWire / PulseAudio) --virtual-mic creates a microphone called "Rocsy Voice"; pick it as the
input device in the game, Discord, Steam... It is removed again when the client stops. On Windows,
install a virtual audio cable (e.g. VB-Audio Cable), run with --output "CABLE Input" and pick
"CABLE Output" as the microphone in the game.

Same PC as Rocsy: the default URL (ws://127.0.0.1:8953/v1/realtime) needs no key. Another PC: use the
site, --url wss://<your Rocsy site>/Voice/v1/realtime --key rk-..., with a key made on the Llama API
page ("Keys for other apps") that has "Real-time voice conversion" ticked.
Test without a microphone: --input-file speech.wav --output-file converted.wav
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import urllib.request

LOCAL_URL = "ws://127.0.0.1:8953/v1/realtime"
VIRTUAL = None   # the VirtualMic while --virtual-mic runs
SINK = "rocsy_vc"
MIC = "rocsy_vc_mic"


def voices_url(url: str) -> str:
    http = url.replace("wss://", "https://", 1).replace("ws://", "http://", 1)
    return http.split("?", 1)[0].rstrip("/") + "/voices"


def list_voices(url: str, key: str) -> None:
    request = urllib.request.Request(voices_url(url), headers={"Authorization": f"Bearer {key}"} if key else {})
    with urllib.request.urlopen(request, timeout=10) as response:
        for voice in json.load(response)["voices"]:
            print(f"{voice['name']:<28} {voice['id']:<40} ({voice['kind']})")


class VirtualMic:
    """A PipeWire/PulseAudio null sink we play into, and its monitor remapped as a named microphone."""

    def __init__(self) -> None:
        self.modules: list[str] = []

    def __enter__(self) -> "VirtualMic":
        if not shutil.which("pactl"):
            sys.exit("--virtual-mic needs pactl (PipeWire's pipewire-pulse or PulseAudio).")
        load = lambda *args: subprocess.run(["pactl", "load-module", *args], check=True, capture_output=True,
                                            text=True).stdout.strip()
        self.modules.append(load("module-null-sink", f"sink_name={SINK}", "rate=48000", "channels=1",
                                 "sink_properties=device.description=Rocsy-Voice-Output"))
        self.modules.append(load("module-remap-source", f"master={SINK}.monitor", f"source_name={MIC}",
                                 "channels=1", "source_properties=device.description=Rocsy-Voice"))
        os.environ["PULSE_SINK"] = SINK      # PortAudio's "pulse" device plays into it
        print('Virtual microphone "Rocsy Voice" is ready: pick it as the input device in your game.')
        return self

    def route_playback(self) -> bool:
        """Move this process's playback onto the Rocsy sink, whichever ALSA/PipeWire path PortAudio took."""
        pid = str(os.getpid())
        for _ in range(20):
            listed = subprocess.run(["pactl", "-f", "json", "list", "sink-inputs"], capture_output=True, text=True)
            try:
                inputs = json.loads(listed.stdout or "[]")
            except ValueError:
                inputs = []
            ours = [i for i in inputs if str(i.get("properties", {}).get("application.process.id")) == pid]
            if ours:
                for item in ours:
                    subprocess.run(["pactl", "move-sink-input", str(item["index"]), SINK], capture_output=True)
                return True
            time.sleep(0.1)
        print("Could not move this program's audio onto Rocsy Voice, so it stays silent rather than "
              "play through your speakers. Try --output pulse, or move it in pavucontrol and restart.")
        return False

    def __exit__(self, *_exc) -> None:
        for module in reversed(self.modules):
            subprocess.run(["pactl", "unload-module", module], capture_output=True)


async def run(args: argparse.Namespace) -> None:
    import numpy as np
    import websockets

    rate, frame = args.sample_rate, args.sample_rate // 50          # 20 ms capture frames
    mic: queue.Queue[bytes] = queue.Queue()
    out_buffer = bytearray()
    out_lock = threading.Lock()
    stop = threading.Event()
    converted: list[bytes] = []
    started = time.monotonic()

    headers = {"Authorization": f"Bearer {args.key}"} if args.key else {}
    settings = {"voice": args.voice, "sample_rate": rate, "block_ms": args.block_ms,
                "diffusion_steps": args.steps, "silence_db": args.silence_db}
    if args.publish:
        settings["publish"] = args.publish
    try:
        connect = websockets.connect(args.url, additional_headers=headers, max_size=8 * 1024 * 1024)
    except TypeError:   # websockets < 14
        connect = websockets.connect(args.url, extra_headers=headers, max_size=8 * 1024 * 1024)
    async with connect as ws:
        await ws.send(json.dumps(settings))
        while True:
            reply = json.loads(await ws.recv())
            if reply["type"] == "ready":
                break
            if reply["type"] == "error":
                sys.exit(f"Rocsy refused: {reply['message']}")
            if reply["type"] == "waiting":
                print(f"Waiting for the GPU (queue position {reply.get('position')})...")
        print(f"Speaking as {reply['voice']['name']} - about {reply['latency_ms']} ms delay plus conversion time. Ctrl+C stops.")

        streams = []
        import sounddevice as sd

        # With --virtual-mic, nothing plays until the stream sits on the Rocsy sink: the converted
        # voice must never come out of the speakers or reach a call you're already in.
        routed = threading.Event()
        if not (args.virtual_mic and not args.output):
            routed.set()

        def playback(outdata, frames, _time, _status) -> None:
            need = frames * 2
            if not routed.is_set():
                outdata[:] = b"\0" * need
                return
            with out_lock:
                chunk = bytes(out_buffer[:need])
                del out_buffer[:need]
            outdata[:len(chunk)] = chunk
            outdata[len(chunk):] = b"\0" * (need - len(chunk))

        if not args.output_file:
            streams.append(sd.RawOutputStream(samplerate=rate, channels=1, dtype="int16", blocksize=frame,
                                              device=args.output, callback=playback))
        if args.input_file:
            import soundfile as sf
            audio, file_rate = sf.read(args.input_file, dtype="float32", always_2d=True)
            audio = audio.mean(axis=1)
            if file_rate != rate:
                audio = np.interp(np.arange(0, len(audio), file_rate / rate), np.arange(len(audio)), audio).astype("float32")
            pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes()

            def feed() -> None:      # paced like a real microphone
                for i, start in enumerate(range(0, len(pcm), frame * 2)):
                    if stop.is_set():
                        return
                    mic.put(pcm[start:start + frame * 2])
                    time.sleep(max(0.0, started + (i + 1) * 0.02 - time.monotonic()))
                time.sleep(args.block_ms / 1000 * 3)
                stop.set()
            threading.Thread(target=feed, daemon=True).start()
        else:
            def captured(indata, _frames, _time, status) -> None:
                mic.put(bytes(indata))

            streams.append(sd.RawInputStream(samplerate=rate, channels=1, dtype="int16", blocksize=frame,
                                             device=args.input, callback=captured))
        for stream in streams:
            stream.start()
        if args.virtual_mic and not args.output_file and not args.output and VIRTUAL is not None:
            if VIRTUAL.route_playback():
                routed.set()

        async def send() -> None:
            loop = asyncio.get_running_loop()
            while not stop.is_set():
                try:
                    chunk = await loop.run_in_executor(None, mic.get, True, 0.2)
                except queue.Empty:
                    continue
                await ws.send(chunk)

        async def receive() -> None:
            async for message in ws:
                if isinstance(message, bytes):
                    if args.output_file:
                        converted.append(message)
                    else:
                        with out_lock:
                            out_buffer.extend(message)
                            # More than half a second queued (network hiccup): drop the oldest.
                            limit = rate  # 0.5 s of int16
                            if len(out_buffer) > limit:
                                del out_buffer[: len(out_buffer) - limit]
                else:
                    info = json.loads(message)
                    if info["type"] == "stats" and args.verbose:
                        print(f"  conversion {info['infer_ms']} ms per block ({info['realtime_factor']}x real time), "
                              f"~{info['latency_ms']} ms total delay")
                    elif info["type"] == "error":
                        print(f"Rocsy: {info['message']}")

        async def until_stopped() -> None:
            while not stop.is_set():
                await asyncio.sleep(0.1)

        tasks = [asyncio.create_task(t) for t in (send(), receive(), until_stopped())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            stop.set()
            for task in tasks:
                task.cancel()
            for stream in streams:
                stream.stop()
                stream.close()
    if args.output_file:
        import soundfile as sf
        audio = np.frombuffer(b"".join(converted), dtype="<i2")
        sf.write(args.output_file, audio, rate, subtype="PCM_16")
        print(f"Wrote {len(audio) / rate:.1f} s to {args.output_file}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default=os.environ.get("ROCSY_VC_URL", LOCAL_URL))
    parser.add_argument("--key", default=os.environ.get("ROCSY_VC_KEY", ""), help="rk-... key (needed through the site)")
    parser.add_argument("--voice", help="voice name or id (see --list-voices)")
    parser.add_argument("--list-voices", action="store_true")
    parser.add_argument("--virtual-mic", action="store_true", help='Linux: create the "Rocsy Voice" microphone')
    parser.add_argument("--input", help="microphone (sounddevice name or index; default: system default)")
    parser.add_argument("--output", help="where the converted voice plays (default: system default / the virtual mic)")
    parser.add_argument("--sample-rate", type=int, default=48000, choices=(16000, 22050, 24000, 32000, 44100, 48000))
    parser.add_argument("--block-ms", type=int, default=200, help="smaller = less delay, more GPU (100-1000)")
    parser.add_argument("--steps", type=int, default=6, help="diffusion steps: more = closer to the voice, slower")
    parser.add_argument("--silence-db", type=float, default=-50, help="quieter than this is treated as silence")
    parser.add_argument("--input-file", help="test: convert this audio file instead of the microphone")
    parser.add_argument("--output-file", help="test: write the converted audio here instead of playing it")
    parser.add_argument("--publish", metavar="CHANNEL", help="also publish the converted voice on this channel "
                        "(others receive it from /v1/realtime/listen?channel=CHANNEL)")
    parser.add_argument("-v", "--verbose", action="store_true", help="print conversion speed every few seconds")
    args = parser.parse_args()
    if args.list_voices:
        return list_voices(args.url, args.key)
    if not args.voice:
        parser.error("--voice is required (see --list-voices)")
    global VIRTUAL
    try:
        if args.virtual_mic and not args.output_file:
            with VirtualMic() as VIRTUAL:
                asyncio.run(run(args))
        else:
            asyncio.run(run(args))
    except KeyboardInterrupt:
        print("Stopped.")


if __name__ == "__main__":
    main()
