"""Epoch tests: render the same agent-written prompts with the same seeds once per saved epoch of a
LoRA training run, so the epochs can be compared side by side.

Flow (run()): copy each epoch checkpoint into the studio's custom LoRA folder under epoch-tests/<job>/
(never listed in a gallery) -> the studio's agentic model writes one prompt per "run" (a page in the
UI; Video: its prompt enhancer with the H3/LTX guide; Image: the shared framework's write-prompt with
the model guide) -> one render per epoch and run is queued in the studio with that run's prompt, seed
and the checkpoint -> each result is copied to data/epoch-tests/<job>/. State lives in
data/epoch-tests/<job>/test.json, so a Trainer restart resumes polling. Deleting a test removes its
renders and the checkpoint copies.

Review (review_run()): a vision model from llama-swap scores every render against dataset samples
(what the LoRA should teach) in the shared GPU queue, logging live like auto-tag, then picks the
epoch with the best average score.
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import re
import shutil
import time
from contextlib import suppress
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

import httpx

from tagger import COMFY_URL, IMAGE, LLAMA_KEY, LLAMA_URL, VIDEO, duration_of, frames_b64, vision_models

# Trainer family -> (studio, the studio's model id, the custom LoRA folder that studio can load from)
FAMILIES: dict[str, tuple[str, str, str]] = {
    "minimax_h3": ("video", "minimax_h3", "H3MachineCustom"),
    "ltx25": ("video", "ltx_2_5", "H3MachineCustom"),
    "krea2": ("image", "krea2", "KreaCustom"),
    "qwen_image_21": ("image", "qwen_image_21", "KreaCustom"),
    "moss_image": ("image", "moss", "KreaCustom"),
    "anima": ("image", "anima", "KreaCustom"),
}
EPOCH_RE = re.compile(r"-0*(\d+)$")
ACTIVE = {"preparing", "writing_prompt", "rendering"}
REVIEW_ACTIVE = {"waiting_for_gpu", "preparing", "running"}
RENDER_TRIES = 3        # attempts per render before it stays failed (manual Retry starts the count again)
MAX_RUNS = 32          # prompts (pages) per test, incl. extra seeds / epoch-subset pages added later
REVIEW_FRAMES = 6          # frames per video render the reviewer sees
REVIEW_REFS = 3            # dataset samples shown as "what the LoRA should teach"
REMOTE_DONE = {"complete", "failed", "cancelled"}
DEFAULT_VIDEO_PRESET = "Agentic Preview"
# Without a preset, Video renders a short low-res clip (its own defaults are a 15 s 720p-class render).
VIDEO_FALLBACK = {"duration": 5, "megapixels": 0.25}
EXTS = {"video/mp4": ".mp4", "image/png": ".png", "image/webp": ".webp", "image/jpeg": ".jpg"}


class EpochTests:
    def __init__(self, *, data_dir: Path, model_root: Path, dataset_root: Path, video_url: str, image_url: str,
                 framework_url: str, headers: dict[str, str], get_job: Callable[[str], dict[str, Any] | None],
                 output_dir: Callable[[dict[str, Any]], Path], log: Callable[[str, str], None],
                 queue_request: Callable[..., Any]):
        self.root = data_dir / "epoch-tests"
        self.tagging_settings = data_dir / "tagging.json"
        self.queue_request = queue_request
        self.reviews: dict[str, asyncio.Task[Any]] = {}
        self.review_cancel: set[str] = set()
        self.model_root, self.dataset_root = model_root, dataset_root
        self.urls = {"video": video_url, "image": image_url}
        self.framework, self.headers = framework_url, headers
        self.get_job, self.output_dir, self.log = get_job, output_dir, log
        self.tasks: dict[str, asyncio.Task[Any]] = {}

    # ---- state ----------------------------------------------------------------------------------
    def folder(self, job_id: str) -> Path:
        return self.root / job_id

    def load(self, job_id: str) -> dict[str, Any] | None:
        try:
            test = json.loads((self.folder(job_id) / "test.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if "runs" not in test:      # tests from before multi-prompt runs: one run
            test["runs"] = [{"prompt": test.get("prompt", ""), "prompt_by": test.get("prompt_by", ""), "seed": test.get("seed", 0)}]
            for i in test["items"]:
                i.setdefault("run", 0)
        test.setdefault("review", None)
        return test

    def save(self, test: dict[str, Any]) -> None:
        folder = self.folder(test["job_id"])
        folder.mkdir(parents=True, exist_ok=True)
        tmp = folder / "test.json.tmp"
        tmp.write_text(json.dumps(test, indent=1), encoding="utf-8")
        os.replace(tmp, folder / "test.json")

    # Your starred epochs (labels) live beside test.json so a click never races the runner's saves.
    def stars(self, job_id: str) -> list[str]:
        try:
            data = json.loads((self.folder(job_id) / "stars.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return [x for x in data if isinstance(x, str)] if isinstance(data, list) else []

    def save_stars(self, job_id: str, labels: list[str]) -> None:
        folder = self.folder(job_id)
        tmp = folder / "stars.json.tmp"
        tmp.write_text(json.dumps(labels), encoding="utf-8")
        os.replace(tmp, folder / "stars.json")

    def star(self, job_id: str, label: str, starred: bool) -> list[str]:
        test = self.load(job_id)
        if not test:
            raise ValueError("This job has no epoch test yet.")
        order = list(dict.fromkeys(i["label"] for i in test["items"]))
        if label not in order:
            raise ValueError("That epoch isn't part of this test.")
        keep = set(self.stars(job_id)) - {label} | ({label} if starred else set())
        labels = [x for x in order if x in keep]
        self.save_stars(job_id, labels)
        return labels

    def summary(self, job_id: str) -> dict[str, Any] | None:
        """What a job card needs: state and counts (None = never tested)."""
        test = self.load(job_id)
        if not test:
            return None
        items = test["items"]
        review = test.get("review") or {}
        return {"state": test["state"], "total": len(items), "done": sum(i["state"] == "complete" for i in items),
                "failed": sum(i["state"] in ("failed", "cancelled") for i in items), "stage": test.get("stage", ""),
                "runs": len(test["runs"]), "review": review.get("state", ""), "best": review.get("best", ""),
                "starred": self.stars(job_id)}

    def public(self, job_id: str, prefix: str, log_after: int = 0) -> dict[str, Any] | None:
        test = self.load(job_id)
        if not test:
            return None
        out = {k: v for k, v in test.items() if k not in ("items", "review")}
        out["starred"] = self.stars(job_id)
        out["items"] = [{**i, "media_url": f"{prefix}/api/jobs/{job_id}/epoch-test/media/{i['output']}?v={int(i.get('finished') or 0)}"
                         if i.get("output") else None} for i in test["items"]]
        review = test.get("review")
        if review:
            log, off = review.get("log") or [], review.get("log_offset", 0)
            out["review"] = {**{k: v for k, v in review.items() if k not in ("log", "queue_entry_id")},
                             "epochs": self.epoch_scores(test), "log": log[max(0, log_after - off):], "log_next": off + len(log),
                             "unreviewed": sum(1 for i in test["items"] if i["state"] == "complete" and not i.get("review"))}
        return out

    @staticmethod
    def epoch_scores(test: dict[str, Any]) -> list[dict[str, Any]]:
        """Average review score per epoch label, in epoch order."""
        out: dict[str, dict[str, Any]] = {}
        for i in test["items"]:
            e = out.setdefault(i["label"], {"label": i["label"], "scores": [], "renders": 0})
            e["renders"] += 1
            if isinstance((i.get("review") or {}).get("score"), (int, float)):
                e["scores"].append(float(i["review"]["score"]))
        return [{"label": e["label"], "renders": e["renders"], "reviewed": len(e["scores"]),
                 "score": round(sum(e["scores"]) / len(e["scores"]), 2) if e["scores"] else None} for e in out.values()]

    # ---- checks ---------------------------------------------------------------------------------
    @staticmethod
    def supported(item: dict[str, Any]) -> bool:
        request = json.loads(item["request_json"])
        return item["model"] in FAMILIES and request.get("kind", "lora") == "lora"

    def epochs(self, item: dict[str, Any]) -> list[tuple[str, Path]]:
        """(label, file) per saved epoch, oldest first; the final file alone when no epoch was saved."""
        folder = self.output_dir(item)
        files = sorted(folder.glob("*.safetensors")) if folder.is_dir() else []
        numbered = sorted(((int(m.group(1)), f) for f in files if (m := EPOCH_RE.search(f.stem))), key=lambda x: x[0])
        if numbered:
            return [(f"Epoch {n}", f) for n, f in numbered]
        final = folder / f"{item['name']}.safetensors"
        return [("Final", final)] if final.is_file() else []

    async def presets(self, family: str) -> dict[str, Any]:
        studio, model, _ = FAMILIES[family]
        if studio != "video":
            return {"presets": [], "default": ""}
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(self.urls["video"] + "/api/presets")
                r.raise_for_status()
                names = list((r.json().get(model) or {}).keys())
        except (httpx.HTTPError, ValueError, AttributeError):
            names = []
        return {"presets": names, "default": DEFAULT_VIDEO_PRESET if DEFAULT_VIDEO_PRESET in names else (names[0] if names else "")}

    # ---- start / cancel / delete ------------------------------------------------------------------
    @staticmethod
    def new_runs(count: int, prompts: list[str], seed: int | None, guides: list[str] | None = None,
                 no_tags: bool = False) -> list[dict[str, Any]]:
        """`count` runs: your prompts first, the agent writes the rest. Run 1 takes `seed`, the others a random one.
        `guides` steer the agent-written ones in order (what each should test); a single guide steers all of them."""
        prompts = [p.strip() for p in prompts if p.strip()][:MAX_RUNS]
        guides = [g.strip()[:500] for g in guides or [] if g.strip()]
        count = max(1, min(MAX_RUNS, max(count, len(prompts) + len(guides))))
        runs = [{"prompt": prompts[k] if k < len(prompts) else "", "prompt_by": "you" if k < len(prompts) else "",
                 "seed": seed if (k == 0 and seed is not None) else random.randint(1, 2_147_483_647)} for k in range(count)]
        for j, run in enumerate(r for r in runs if not r["prompt"]):
            guide = guides[0] if len(guides) == 1 else guides[j] if j < len(guides) else ""
            if guide:
                run["guide"] = guide
            if no_tags:
                run["no_tags"] = True       # the agent sees no training captions and leaves the look to the trigger
        return runs

    def start(self, item: dict[str, Any], *, preset: str = "", strength: float = 1.0, prompt: str = "",
              seed: int | None = None, every: int = 1, runs: int = 1, prompts: list[str] | None = None,
              review: bool = False, review_model: str = "", teach: str = "", guides: list[str] | None = None,
              no_tags: bool = False) -> dict[str, Any]:
        job_id = item["id"]
        old = self.load(job_id)
        if old and (old["state"] in ACTIVE or (old.get("review") or {}).get("state") in REVIEW_ACTIVE):
            raise ValueError("An epoch test (or its review) is already running for this job.")
        epochs = self.epochs(item)[::-1][::every][::-1] if every > 1 else self.epochs(item)
        if not epochs:
            raise ValueError("This job has no saved LoRA files to test.")
        starred = self.stars(job_id)       # "Test again" keeps your stars on epochs it renders again
        if old:
            self.delete_files(job_id, keep_state=False)
        studio, model, lora_dir = FAMILIES[item["model"]]
        request = json.loads(item["request_json"])
        run_list = self.new_runs(runs, [prompt, *(prompts or [])], seed, guides, no_tags)
        test = {"job_id": job_id, "name": item["name"], "family": item["model"], "studio": studio, "model": model,
                "trigger": str(request.get("trigger") or "").strip(), "dataset": item["dataset"],
                "preset": preset, "strength": strength, "runs": run_list,
                "auto_review": review, "review_model": review_model, "teach": teach.strip()[:500],
                "state": "preparing", "stage": "Copying checkpoints", "error": "", "created": time.time(),
                "review": None,
                "items": [{"label": label, "file": f.name, "lora": f"{lora_dir}/epoch-tests/{job_id}/{f.name}", "run": k,
                           "remote_id": "", "state": "waiting", "output": "", "error": ""}
                          for k in range(len(run_list)) for label, f in epochs]}
        self.save(test)
        if starred:
            self.save_stars(job_id, [label for label, _ in epochs if label in starred])
        self.spawn(job_id)
        return test

    def extend(self, job_id: str, count: int, prompts: list[str], labels: list[str] | None = None,
               seed_from: int | None = None, seed: int | None = None, guides: list[str] | None = None,
               no_tags: bool = False) -> dict[str, Any]:
        """More pages for a finished test (earlier renders are kept): new prompts, or with `seed_from` the prompt of that
        page again under new seeds; `labels` limits the new pages to those epochs (empty = every epoch)."""
        test = self.load(job_id)
        if not test:
            raise ValueError("This job has no epoch test yet.")
        if test["state"] in ACTIVE or (test.get("review") or {}).get("state") in REVIEW_ACTIVE:
            raise ValueError("Wait until the epoch test and its review have finished.")
        if len(test["runs"]) >= MAX_RUNS:
            raise ValueError(f"A test holds at most {MAX_RUNS} prompts; start a new test for more.")
        first = {}
        for i in test["items"]:
            first.setdefault(i["label"], i)
        wanted = [x for x in dict.fromkeys(labels or []) if x]
        if any(x not in first for x in wanted):
            raise ValueError("Some of those epochs aren't part of this test.")
        if seed_from is not None:
            if not 0 <= seed_from < len(test["runs"]) or not test["runs"][seed_from]["prompt"]:
                raise ValueError("That prompt doesn't exist (or has no text yet).")
            src = test["runs"][seed_from]
            added = [{"prompt": src["prompt"], "prompt_by": src["prompt_by"], "of": seed_from, **run}
                     for run in ({"seed": r["seed"]} for r in self.new_runs(count, [], seed))]
        else:
            added = self.new_runs(count, prompts, seed, guides, no_tags)
        added = added[:MAX_RUNS - len(test["runs"])]
        if wanted:
            for run in added:
                run["labels"] = wanted
            first = {x: first[x] for x in first if x in wanted}
        base = len(test["runs"])
        test["runs"] += added
        test["items"] += [{"label": e["label"], "file": e["file"], "lora": e["lora"], "run": base + k,
                           "remote_id": "", "state": "waiting", "output": "", "error": ""}
                          for k in range(len(added)) for e in first.values()]
        test.update(state="preparing", stage="Copying checkpoints", error="")
        self.save(test)
        self.spawn(job_id)
        return test

    def spawn(self, job_id: str) -> None:
        task = asyncio.create_task(self.run(job_id))
        self.tasks[job_id] = task
        task.add_done_callback(lambda _t, j=job_id: self.tasks.pop(j, None) if self.tasks.get(j) is _t else None)

    def resume_all(self) -> None:
        """After a restart: keep polling/queuing tests that were still running."""
        if not self.root.is_dir():
            return
        for folder in self.root.iterdir():
            test = self.load(folder.name)
            if test and test["state"] in ACTIVE and folder.name not in self.tasks:
                self.spawn(folder.name)
            review = (test or {}).get("review") or {}
            if review.get("state") in ("preparing", "running"):   # its GPU-queue slot died with the old process
                review.update(state="failed", stage="Interrupted", error="The Trainer restarted during the review; press Review to continue.",
                              current=None)
                self.save(test)

    def retry(self, job_id: str) -> dict[str, Any]:
        """Resume a failed / cancelled test: renders still in the studio are picked up again, the rest re-queued."""
        test = self.load(job_id)
        if not test:
            raise ValueError("This job has no epoch test yet.")
        if test["state"] in ACTIVE or (test.get("review") or {}).get("state") in REVIEW_ACTIVE:
            raise ValueError("The epoch test (or its review) is still running.")
        todo = 0
        for i in test["items"]:
            if i["state"] in ("queued", "rendering") and i["remote_id"]:
                todo += 1                                # finished or still running in the studio: poll it again
            elif i["state"] != "complete":
                i.update(state="waiting", remote_id="", error="", tries=1)
                todo += 1
        if not todo:
            raise ValueError("Every render is already done.")
        test.update(state="preparing", stage=f"Retrying {todo} render(s)", error="")
        self.save(test)
        self.spawn(job_id)
        return test

    async def cancel(self, job_id: str) -> dict[str, Any]:
        test = self.load(job_id)
        if not test or test["state"] not in ACTIVE:
            raise ValueError("No epoch test is running for this job.")
        task = self.tasks.get(job_id)
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task
        test = self.load(job_id) or test
        async with httpx.AsyncClient(timeout=20) as client:
            for i in test["items"]:
                if i["remote_id"] and i["state"] in ("queued", "rendering"):
                    with suppress(httpx.HTTPError):
                        await client.post(f"{self.urls[test['studio']]}/api/jobs/{i['remote_id']}/cancel", headers=self.headers)
                if i["state"] not in ("complete", "failed"):
                    i["state"] = "cancelled"
        test.update(state="cancelled", stage="Cancelled")
        self.save(test)
        return test

    def delete_files(self, job_id: str, keep_state: bool = False) -> None:
        test = self.load(job_id)
        if test:
            lora_dir = FAMILIES.get(test["family"], ("", "", ""))[2]
            if lora_dir:
                shutil.rmtree(self.model_root / "loras" / lora_dir / "epoch-tests" / job_id, ignore_errors=True)
        if not keep_state:
            shutil.rmtree(self.folder(job_id), ignore_errors=True)

    # ---- deleting single pages / renders (finished tests only; indices shift, so the client refetches) ----------
    def _editable(self, job_id: str) -> dict[str, Any]:
        test = self.load(job_id)
        if not test:
            raise ValueError("This job has no epoch test yet.")
        if test["state"] in ACTIVE or (test.get("review") or {}).get("state") in REVIEW_ACTIVE:
            raise ValueError("Wait until the epoch test and its review have finished.")
        return test

    def _drop_items(self, test: dict[str, Any], drop: set[int]) -> None:
        for n in drop:
            if test["items"][n].get("output"):
                with suppress(OSError):
                    (self.folder(test["job_id"]) / test["items"][n]["output"]).unlink()
        test["items"] = [i for n, i in enumerate(test["items"]) if n not in drop]

    def _drop_run(self, test: dict[str, Any], k: int) -> None:
        self._drop_items(test, {n for n, i in enumerate(test["items"]) if (i.get("run") or 0) == k})
        test["runs"].pop(k)
        for i in test["items"]:
            if (i.get("run") or 0) > k:
                i["run"] -= 1
        for r in test["runs"]:
            if r.get("of") == k:
                r.pop("of")          # its source page is gone; the prompt text stays
            elif (r.get("of") or 0) > k:
                r["of"] -= 1

    def delete_run(self, job_id: str, k: int) -> dict[str, Any]:
        test = self._editable(job_id)
        if not 0 <= k < len(test["runs"]):
            raise ValueError("That page doesn't exist.")
        if len(test["runs"]) == 1:
            raise ValueError("That's the only page; use Delete test to remove the whole test.")
        self._drop_run(test, k)
        test["stage"] = f"{sum(i['state'] == 'complete' for i in test['items'])} of {len(test['items'])} renders done"
        self.save(test)
        return test

    def delete_item(self, job_id: str, n: int) -> dict[str, Any]:
        test = self._editable(job_id)
        if not 0 <= n < len(test["items"]):
            raise ValueError("That render doesn't exist.")
        if len(test["items"]) == 1:
            raise ValueError("That's the only render; use Delete test to remove the whole test.")
        k = test["items"][n].get("run") or 0
        self._drop_items(test, {n})
        if not any((i.get("run") or 0) == k for i in test["items"]):
            self._drop_run(test, k)          # its page is empty now
        test["stage"] = f"{sum(i['state'] == 'complete' for i in test['items'])} of {len(test['items'])} renders done"
        self.save(test)
        return test

    async def delete(self, job_id: str) -> None:
        test = self.load(job_id)
        if test and test["state"] in ACTIVE:
            await self.cancel(job_id)
        if test and (test.get("review") or {}).get("state") in REVIEW_ACTIVE:
            await self.cancel_review(job_id)
        self.delete_files(job_id)

    # ---- the run --------------------------------------------------------------------------------
    async def run(self, job_id: str) -> None:
        test = self.load(job_id)
        if not test:
            return
        try:
            item = self.get_job(job_id)
            if not item:
                raise ValueError("The training job is gone.")
            await asyncio.to_thread(self.copy_checkpoints, item, test)
            # All prompts first: alternating prompt writing (LLM) and renders (ComfyUI) would swap models every page.
            for k, run in enumerate(test["runs"]):
                if not run["prompt"]:
                    test.update(state="writing_prompt", stage=f"The agent is writing prompt {k + 1} of {len(test['runs'])}")
                    self.save(test)
                    run["prompt"] = await self.write_prompt(test, k)
                    run["prompt_by"] = "agent"
                    self.save(test)
                    self.log(job_id, f"\n[Epoch test] Prompt {k + 1} ({run['prompt_by']}, seed {run['seed']}): {run['prompt']}\n")
            test.update(state="rendering", stage="Queuing renders")
            self.save(test)
            self.log(job_id, f"\n[Epoch test] {len(test['runs'])} prompt(s), {len(test['items'])} render(s) in the {test['studio'].title()} studio.\n")
            async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=10)) as client:
                while True:                    # rounds: a failed render is retried automatically (RENDER_TRIES in all)
                    for i in test["items"]:          # queue every epoch first, so they render back to back
                        if not i["remote_id"] and i["state"] == "waiting":
                            try:
                                i["remote_id"] = await self.submit(client, test, i)
                            except ValueError as exc:      # only this epoch fails; the others still render
                                i.update(state="failed", error=str(exc)[:300])
                                self.save(test)
                                self.log(job_id, f"\n[Epoch test] {i['label']}: {exc}\n")
                                continue
                            i["state"] = "queued"
                            test["stage"] = f"Queued {sum(1 for x in test['items'] if x['remote_id'])} of {len(test['items'])}"
                            self.save(test)
                    while any(i["state"] in ("queued", "rendering") for i in test["items"]):
                        for i in test["items"]:
                            if i["state"] in ("queued", "rendering"):
                                await self.poll(client, test, i)
                        done = sum(i["state"] == "complete" for i in test["items"])
                        test["stage"] = f"Rendered {done} of {len(test['items'])}"
                        self.save(test)
                        if any(i["state"] in ("queued", "rendering") for i in test["items"]):
                            await asyncio.sleep(5)
                    again = [i for i in test["items"] if i["state"] == "failed" and i.get("tries", 1) < RENDER_TRIES]
                    if not again:
                        break
                    for i in again:
                        self.log(job_id, f"\n[Epoch test] {i['label']} failed ({i['error']}); retrying ({i.get('tries', 1) + 1} of {RENDER_TRIES}).\n")
                        i.update(state="waiting", remote_id="", error="", tries=i.get("tries", 1) + 1)
                    test["stage"] = f"Retrying {len(again)} failed render(s) in 20 s"
                    self.save(test)
                    await asyncio.sleep(20)
            ok = sum(i["state"] == "complete" for i in test["items"])
            test.update(state="complete" if ok else "failed", stage=f"{ok} of {len(test['items'])} renders done",
                        error="" if ok else "Nothing rendered; see each epoch's error.")
            self.save(test)
            self.log(job_id, f"\n[Epoch test] {test['stage']}.\n")
            if ok and test.get("auto_review"):
                try:
                    await self.start_review(job_id, test.get("review_model") or "", test.get("teach") or "")
                except Exception as exc:  # noqa: BLE001 - the renders are fine; only the review didn't start
                    self.log(job_id, f"\n[Epoch review] Could not start: {exc}\n")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - shown on the card instead of crashing the task
            test.update(state="failed", stage="Failed", error=str(exc)[:500])
            self.save(test)
            self.log(job_id, f"\n[Epoch test] Failed: {exc}\n")

    def copy_checkpoints(self, item: dict[str, Any], test: dict[str, Any]) -> None:
        source = self.output_dir(item)
        for i in test["items"]:
            src, dest = source / i["file"], self.model_root / "loras" / i["lora"]
            if dest.is_file() and dest.stat().st_size == src.stat().st_size:
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_suffix(".part")
            shutil.copyfile(src, tmp)
            owner = (self.model_root / "loras").stat()
            for path in (tmp, dest.parent, dest.parent.parent):
                with suppress(OSError):   # the studios run as the folder's owner
                    os.chown(path, owner.st_uid, owner.st_gid)
            os.replace(tmp, dest)

    def captions(self, dataset: str, n: int = 4) -> list[str]:
        folder = self.dataset_root / dataset
        texts = []
        for f in sorted(folder.glob("*.txt")) if folder.is_dir() else []:
            with suppress(OSError):
                text = f.read_text(encoding="utf-8", errors="replace").strip()
                if text:
                    texts.append(text[:600])
        random.shuffle(texts)
        return texts[:n]

    def brief(self, test: dict[str, Any], k: int = 0) -> str:
        kind = "video clip" if test["studio"] == "video" else "image"
        lines = [f"Write the prompt for a test {kind} of a freshly trained LoRA named \"{test['name']}\". "
                 "The SAME prompt and seed will be rendered once per training epoch to compare how well each epoch learned it, "
                 "so describe one clear, typical scene of what the LoRA was trained on, with nothing it can't have learned."]
        if test.get("teach"):
            lines.append(f"What the LoRA should teach: {test['teach']}")
        guide = test["runs"][k].get("guide") if k < len(test["runs"]) else ""
        if guide:
            lines.append(f"THIS PROMPT'S TEST GUIDE (from the user; follow it, it decides what the scene tests): {guide}")
        others = list(dict.fromkeys(r["prompt"] for j, r in enumerate(test["runs"]) if j != k and r["prompt"]))
        if others:
            lines.append(f"This is test prompt {k + 1} of {len(test['runs'])}. Write a DIFFERENT scene from the ones below "
                         "(another setting, pose, framing or action that the training data still covers), so the epochs are compared on variety:\n"
                         + "\n".join(f"- {o[:300]}" for o in others))
        if test["trigger"]:
            lines.append(f"Use the trigger word \"{test['trigger']}\" exactly as written, where the guide puts the subject.")
        if k < len(test["runs"]) and test["runs"][k].get("no_tags"):
            lines.append("Do NOT describe the subject's look (hair, eyes, skin, body, outfit, art style or any other trait from the training "
                         "data): " + ("the trigger word alone" if test["trigger"] else "the LoRA alone") + " must bring it, so the test shows "
                         "what the LoRA learned. Describe only the scene: setting, pose, action, framing, lighting.")
            return "\n\n".join(lines)
        caps = self.captions(test["dataset"])
        if caps:
            lines.append("Training captions (examples of what the LoRA learned):\n" + "\n".join(f"- {c}" for c in caps))
        return "\n\n".join(lines)

    async def write_prompt(self, test: dict[str, Any], k: int = 0) -> str:
        brief = self.brief(test, k)
        async with httpx.AsyncClient(timeout=httpx.Timeout(900, connect=10)) as client:
            if test["studio"] == "image":
                r = await client.post(f"{self.framework}/v1/agentic/image/write-prompt", headers=self.headers,
                                      json={"brief": brief, "settings": {"model": test["model"]}, "purpose": "epoch test prompt"})
                if r.status_code >= 400:
                    raise ValueError(f"The Image agent could not write a prompt: {r.text[:300]}")
                return r.json()["prompt"]
            duration = (await self.video_request(client, test, test["items"][0])).get("duration")   # so the prompt fits the clip
            r = await client.post(self.urls["video"] + "/api/enhancement/enhance",
                                  json={"model": test["model"], "prompt": brief, "mode": "text", "duration": duration})
            if r.status_code >= 400:
                raise ValueError(f"The Video agent could not start: {r.text[:300]}")
            enhance_id = r.json()["job_id"]
            deadline = time.monotonic() + 3600   # it waits its turn in the GPU queue
            while time.monotonic() < deadline:
                await asyncio.sleep(3)
                j = (await client.get(f"{self.urls['video']}/api/enhancement/jobs/{enhance_id}")).json()
                if j.get("state") == "complete":
                    prompt = str((j.get("result") or {}).get("prompt") or "").strip()
                    if not prompt:
                        raise ValueError("The Video agent returned an empty prompt.")
                    return prompt
                if j.get("state") in ("failed", "cancelled"):
                    raise ValueError(f"The Video agent {j['state']}: {str(j.get('error') or '')[:300]}")
            raise ValueError("The Video agent took over an hour to write the prompt.")

    async def video_request(self, client: httpx.AsyncClient, test: dict[str, Any], i: dict[str, Any]) -> dict[str, Any]:
        settings: dict[str, Any] = dict(VIDEO_FALLBACK)
        if test["preset"]:
            r = await client.get(self.urls["video"] + "/api/presets")
            preset = (r.json().get(test["model"]) or {}).get(test["preset"]) if r.status_code == 200 else None
            if not isinstance(preset, dict):
                raise ValueError(f"The Video preset \"{test['preset']}\" no longer exists.")
            settings = dict(preset)
        for key in ("loras", "ltx_loras", "wan_loras", "reference_ids", "first_frame_id", "last_frame_id", "prompt", "seed", "model", "mode"):
            settings.pop(key, None)       # only the epoch under test, text mode, our prompt and seed
        run = test["runs"][i.get("run", 0)]
        return {**settings, "model": test["model"], "mode": "text", "prompt": run["prompt"], "seed": run["seed"],
                "test_lora": i["lora"], "test_lora_strength": test["strength"]}

    async def submit(self, client: httpx.AsyncClient, test: dict[str, Any], i: dict[str, Any]) -> str:
        base = self.urls[test["studio"]]
        offline_since = None
        for _attempt in range(2000):     # Video refuses another model's job while one runs: wait up to ~6 h
            try:
                if test["studio"] == "video":
                    r = await client.post(base + "/api/generate", json=await self.video_request(client, test, i))
                else:
                    run = test["runs"][i.get("run", 0)]
                    r = await client.post(base + "/api/internal/generate", headers=self.headers, json={"request": {
                        "model": test["model"], "prompt": run["prompt"], "seed": run["seed"], "count": 1,
                        "test_lora": i["lora"], "test_lora_strength": test["strength"]}})
                down = r.status_code in (502, 503, 504) or "comfy_unavailable" in r.text
                why = r.text[:300]
            except httpx.TransportError as exc:      # the studio container itself is restarting
                down, why = True, str(exc) or type(exc).__name__
            # ComfyUI restarts for a profile switch (e.g. ltx -> qwen on the first image render) and is offline for
            # a few seconds; a studio being rebuilt is gone for a minute. Wait it out instead of failing the test.
            if down:
                offline_since = offline_since or time.monotonic()
                if time.monotonic() - offline_since > 900:
                    raise ValueError(f"The {test['studio'].title()} studio / ComfyUI stayed offline for 15 minutes: {why}")
                test["stage"] = f"Waiting for ComfyUI to come back ({i['label']})"
                self.save(test)
                await asyncio.sleep(10)
                continue
            offline_since = None
            if r.status_code == 409 and "busy" in r.text:
                test["stage"] = "Waiting for the studio to finish another model's renders"
                self.save(test)
                await asyncio.sleep(30)
                continue
            if r.status_code >= 400:
                raise ValueError(f"The {test['studio'].title()} studio refused {i['label']}: {r.text[:300]}")
            return r.json()["id"]
        raise ValueError("The studio stayed busy with another model for hours.")

    async def poll(self, client: httpx.AsyncClient, test: dict[str, Any], i: dict[str, Any]) -> None:
        base = self.urls[test["studio"]]
        r = await client.get(f"{base}/api/jobs/{i['remote_id']}")
        if r.status_code == 404:
            i.update(state="failed", error="The render was removed from the studio.")
            return
        if r.status_code >= 400:
            return  # try again next round
        job = r.json()
        state = job.get("state")
        if state not in REMOTE_DONE:
            i["state"] = "rendering" if state in ("rendering", "finalizing", "enhancing") else "queued"
            i["progress"] = max(0, min(100, int(job.get("progress") or 0)))   # the studio's own %, for the tile's bar
            return
        if state != "complete":
            i.update(state=state, error=str(job.get("error") or state)[:300])
            return
        parts = urlsplit(base)
        url = f"{parts.scheme}://{parts.netloc}" + job["download_url"]   # download_url already has the /Video or /Image prefix + ("?raw=1" if test["studio"] == "video" else "")
        async with client.stream("GET", url) as resp:
            if resp.status_code >= 400:
                i.update(state="failed", error=f"Could not download the render (HTTP {resp.status_code}).")
                return
            ext = EXTS.get(resp.headers.get("content-type", "").split(";")[0], ".mp4" if test["studio"] == "video" else ".png")
            name = re.sub(r"[^A-Za-z0-9_-]+", "_", i["label"]).strip("_").lower() + (f"_p{i['run'] + 1}" if i.get("run") else "") + ext
            target = self.folder(test["job_id"]) / name
            tmp = target.with_suffix(target.suffix + ".part")
            with tmp.open("wb") as fh:
                async for chunk in resp.aiter_bytes():
                    fh.write(chunk)
            os.replace(tmp, target)
        i.update(state="complete", output=name, finished=time.time(), error="")
        if test["studio"] == "image":     # hidden render: the copy is ours now
            with suppress(httpx.HTTPError):
                await client.delete(f"{base}/api/internal/jobs/{i['remote_id']}", headers=self.headers)

    # ---- agent review: a vision model scores every render for what the LoRA should teach -----------
    def rev(self, review: dict[str, Any], kind: str, text: str, **extra: Any) -> None:
        """Review log line for the Hermes-style terminal (kinds as in auto-tag: user, system, tool, assistant, ok, warn, error)."""
        log = review.setdefault("log", [])
        log.append({"t": time.time(), "kind": kind, "text": text, **extra})
        if len(log) > 600:
            del log[:100]
            review["log_offset"] = review.get("log_offset", 0) + 100

    def default_vision(self) -> str:
        models = [m["id"] for m in vision_models()]
        with suppress(OSError, ValueError):
            saved = json.loads(self.tagging_settings.read_text(encoding="utf-8")).get("default_model")
            if saved in models:
                return saved
        return "qwen-9b" if "qwen-9b" in models else (models[0] if models else "")

    async def start_review(self, job_id: str, model: str = "", teach: str = "", redo: bool = False) -> dict[str, Any]:
        test = self.load(job_id)
        if not test:
            raise ValueError("This job has no epoch test yet.")
        if test["state"] in ACTIVE:
            raise ValueError("Wait until every epoch has rendered.")
        old = test.get("review") or {}
        if old.get("state") in REVIEW_ACTIVE:
            raise ValueError("The review is already running.")
        model = model or old.get("model") or self.default_vision()
        if model not in {m["id"] for m in vision_models()}:
            raise ValueError("Pick a vision model (one that can see images) for the review.")
        if redo:
            for i in test["items"]:
                i.pop("review", None)
        if not any(i["state"] == "complete" and not i.get("review") for i in test["items"]):
            raise ValueError("Every render is already reviewed (use Review again to redo them).")
        if teach.strip():
            test["teach"] = teach.strip()[:500]
        review = {"state": "waiting_for_gpu", "stage": "Waiting for the shared GPU queue", "model": model, "error": "",
                  "current": None, "best": "" if redo else old.get("best", ""), "summary": "" if redo else old.get("summary", ""),
                  "created": time.time(), "log": old.get("log", []) if not redo else [], "log_offset": 0 if redo else old.get("log_offset", 0)}
        todo = sum(1 for i in test["items"] if i["state"] == "complete" and not i.get("review"))
        self.rev(review, "user", f"review {todo} render(s) of {test['name']} with {model}"
                 + (f" · teaches: {test['teach']}" if test.get("teach") else "") + (f" · trigger \"{test['trigger']}\"" if test["trigger"] else ""))
        self.rev(review, "system", "waiting for the shared GPU queue (renders and training go first)")
        test["review"] = review
        self.save(test)
        self.review_cancel.discard(job_id)
        try:
            r = await self.queue_request("/enqueue", {"app": "Trainer", "job_id": f"erv{job_id}", "label": f"Epoch review: {test['name']} ({todo})"})
            r.raise_for_status()
        except Exception as exc:
            review.update(state="failed", stage="Queue error", error=str(exc)[:300])
            self.rev(review, "error", f"could not reach the shared GPU queue: {str(exc)[:200]}")
            self.save(test)
            raise ValueError("Could not reach the shared GPU queue.") from exc
        test = self.load(job_id) or test
        test["review"]["queue_entry_id"] = r.json().get("entry_id")
        self.save(test)
        return test

    def review_proceed(self, job_id: str, token: str) -> bool:
        test = self.load(job_id)
        if not test or (test.get("review") or {}).get("state") != "waiting_for_gpu" or job_id in self.reviews:
            return False
        task = asyncio.create_task(self.review_run(job_id, token))
        self.reviews[job_id] = task
        task.add_done_callback(lambda _t, j=job_id: self.reviews.pop(j, None) if self.reviews.get(j) is _t else None)
        return True

    async def cancel_review(self, job_id: str) -> dict[str, Any]:
        test = self.load(job_id)
        review = (test or {}).get("review") or {}
        if review.get("state") not in REVIEW_ACTIVE:
            raise ValueError("No review is running for this job.")
        if review["state"] == "waiting_for_gpu":
            with suppress(Exception):
                await self.queue_request(f"/dequeue/{review.get('queue_entry_id')}")
            review.update(state="cancelled", stage="Cancelled", current=None)
            self.rev(review, "warn", "Cancelled before it got the GPU")
            self.save(test)
            return test
        self.review_cancel.add(job_id)      # stops after the render it is looking at
        review["stage"] = "Stopping after the current render"
        self.save(test)
        return test

    def references(self, dataset: str) -> list[str]:
        """A few dataset samples (one frame each) = what the LoRA was trained to produce."""
        folder = self.dataset_root / dataset
        media = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in IMAGE | VIDEO] if folder.is_dir() else []
        images = [p for p in media if p.suffix.lower() in IMAGE]
        pool = images if len(images) >= REVIEW_REFS else media
        random.Random(dataset).shuffle(pool)      # the same samples every time, so scores stay comparable
        out = []
        for p in pool[:REVIEW_REFS]:
            dur = duration_of(p) if p.suffix.lower() in VIDEO else 0.0
            out += [u for _, u in frames_b64(p, dur, 1)]
        return out

    @staticmethod
    def parse_review(text: str) -> dict[str, Any]:
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
        m = re.search(r"\{.*\}", text, re.S)
        data: dict[str, Any] = {}
        with suppress(ValueError):
            data = json.loads(m.group(0)) if m else {}
        def num(key: str) -> float | None:
            with suppress(TypeError, ValueError):
                return max(0.0, min(10.0, float(data.get(key))))
            return None
        score = num("score")
        if score is None:
            raise ValueError(f"no score in the reply: {text[:160]!r}")
        chips = lambda key: [str(x).strip()[:60] for x in (data.get(key) or []) if str(x).strip()][:4] if isinstance(data.get(key), list) else []
        return {"score": round(score, 1), "concept": num("concept"), "prompt": num("prompt"), "quality": num("quality"),
                "good": chips("good"), "bad": chips("bad"), "notes": str(data.get("notes") or "").strip()[:400]}

    async def review_run(self, job_id: str, token: str) -> None:
        test = self.load(job_id)
        review = test["review"]
        stop = asyncio.Event()

        async def heartbeat() -> None:
            while not stop.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), timeout=20)
                except asyncio.TimeoutError:
                    with suppress(Exception):
                        await self.queue_request("/heartbeat", {"token": token}, timeout=6)
        hb = asyncio.create_task(heartbeat())
        headers = {"Authorization": f"Bearer {LLAMA_KEY}"} if LLAMA_KEY else {}
        model = review["model"]
        try:
            self.rev(review, "system", f"GPU queue slot acquired after {time.time() - review['created']:.0f}s")
            review.update(state="preparing", stage="Freeing ComfyUI VRAM")
            self.save(test)
            self.rev(review, "tool", "comfyui.free  unload_models=true free_memory=true", icon="🧹")
            async with httpx.AsyncClient(timeout=20) as c:
                try:
                    fr = await c.post(COMFY_URL + "/free", json={"unload_models": True, "free_memory": True})
                    self.rev(review, "ok", f"ComfyUI VRAM released (HTTP {fr.status_code})")
                except Exception as exc:  # noqa: BLE001
                    self.rev(review, "warn", f"ComfyUI not reachable ({type(exc).__name__}); continuing")
            t0 = time.time()
            refs = await asyncio.to_thread(self.references, test["dataset"])
            self.rev(review, "tool", f"vaapi.frames  dataset {test['dataset']}  {len(refs)} reference sample(s)", icon="🎞",
                     ms=int((time.time() - t0) * 1000))
            caps = self.captions(test["dataset"], 3)
            kind = "video clip" if test["studio"] == "video" else "image"
            system = (f"You judge test renders of a freshly trained {'video' if test['studio'] == 'video' else 'image'} LoRA, one training epoch at a time. "
                      "Score how well a render shows what the LoRA was trained to teach (the subject/style/concept in the reference samples and captions), "
                      "then how well it follows its prompt, then its technical quality (anatomy, melting, noise, flicker, burned/overcooked colours = overfitting). "
                      "Be strict and consistent so epochs can be ranked. Reply with ONLY a JSON object: "
                      '{"concept": 0-10, "prompt": 0-10, "quality": 0-10, "score": 0-10 overall (concept counts most), '
                      '"good": ["up to 3 short phrases"], "bad": ["up to 3 short phrases"], "notes": "one or two sentences"}')
            intro = [f"LoRA \"{test['name']}\"" + (f", trigger word \"{test['trigger']}\"" if test["trigger"] else "") + "."]
            if test.get("teach"):
                intro.append(f"What it should teach: {test['teach']}")
            if caps:
                intro.append("Training captions:\n" + "\n".join(f"- {c[:400]}" for c in caps))
            intro.append(f"Reference samples from the training data ({len(refs)}):")
            # The references come first and never change, so llama.cpp reuses that part of the prompt between renders.
            head: list[dict[str, Any]] = [{"type": "text", "text": "\n".join(intro)}] + [{"type": "image_url", "image_url": {"url": u}} for u in refs]
            review.update(state="running", stage=f"Loading {model} (the first review is slow)")
            self.save(test)
            self.rev(review, "system", f"llama-swap → {model}")
            todo = [n for n, i in enumerate(test["items"]) if i["state"] == "complete" and not i.get("review")]
            async with httpx.AsyncClient(timeout=httpx.Timeout(900, connect=10)) as client:
                for done, n in enumerate(todo):
                    if job_id in self.review_cancel:
                        break
                    i = test["items"][n]
                    run = test["runs"][i.get("run", 0)]
                    where = f"{i['label']} · prompt {i.get('run', 0) + 1}"
                    review.update(current=n, stage=f"Reviewing {done + 1} of {len(todo)} · {where}")
                    self.save(test)
                    try:
                        path = self.folder(job_id) / i["output"]
                        t0 = time.time()
                        dur = await asyncio.to_thread(duration_of, path) if path.suffix.lower() in VIDEO else 0.0
                        frames = await asyncio.to_thread(frames_b64, path, dur, REVIEW_FRAMES)
                        if not frames:
                            raise RuntimeError("could not read the render")
                        self.rev(review, "tool", f"vaapi.frames  {where}  {len(frames)} frame(s)" + (f" of {dur:.1f}s" if dur else ""),
                                 icon="🎞", item=n, ms=int((time.time() - t0) * 1000))
                        what = (f"The test {kind} ({i['label']}, in time order, {len(frames)} frames):" if len(frames) > 1
                                else f"The test {kind} ({i['label']}):")
                        content = head + [{"type": "text", "text": f"Its prompt: {run['prompt'][:2500]}\n\n{what}"}]
                        content += [{"type": "image_url", "image_url": {"url": u}} for _, u in frames]
                        body = {"model": model, "temperature": 0.2, "max_tokens": 500, "chat_template_kwargs": {"enable_thinking": False},
                                "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}]}
                        result = None
                        for attempt in range(2):
                            t0 = time.time()
                            self.rev(review, "tool", f"llama.chat  {model}  [{len(refs)} ref + {len(frames)} render image(s)]" + ("  (retry)" if attempt else ""),
                                     icon="🧠", item=n)
                            r = await client.post(LLAMA_URL + "/v1/chat/completions", json=body, headers=headers)
                            if r.status_code != 200:
                                raise RuntimeError(f"llama HTTP {r.status_code}: {r.text[:200]}")
                            data = r.json()
                            usage = data.get("usage") or {}
                            self.rev(review, "system", f"{time.time() - t0:.1f}s · {usage.get('prompt_tokens', '?')} in / "
                                     f"{usage.get('completion_tokens', '?')} out tokens", item=n)
                            try:
                                result = self.parse_review(data["choices"][0]["message"].get("content") or "")
                                break
                            except ValueError as exc:
                                self.rev(review, "warn", f"{exc}; retrying", item=n)
                                body["max_tokens"] = 1200
                        if result is None:
                            raise RuntimeError("the model returned no usable score")
                        i["review"] = result
                        self.rev(review, "assistant", f"{where}: {result['score']}/10 · {result['notes']}", item=n,
                                 good=result["good"], bad=result["bad"])
                    except Exception as exc:  # noqa: BLE001 - one bad render must not stop the review
                        self.rev(review, "error", f"{where}: {str(exc)[:300]}", item=n)
                    review["best"] = self.best(test)
                    self.save(test)
                cancelled = job_id in self.review_cancel
                review.update(current=None)
                epochs = [e for e in self.epoch_scores(test) if e["score"] is not None]
                if epochs and not cancelled:
                    review["stage"] = "Writing the verdict"
                    self.save(test)
                    review["summary"] = await self.verdict(client, headers, test, epochs)
                    self.rev(review, "assistant", review["summary"])
            review["best"] = self.best(test)
            scored = sum(1 for i in test["items"] if i.get("review"))
            review.update(state="cancelled" if cancelled else "complete" if scored else "failed",
                          stage="Cancelled" if cancelled else f"{scored} render(s) reviewed" + (f" · best: {review['best']}" if review["best"] else ""),
                          error="" if scored else "No render could be reviewed; see the log.")
            self.rev(review, "warn" if cancelled else "ok", review["stage"])
            self.log(job_id, f"\n[Epoch review] {review['stage']}.\n")
        except Exception as exc:  # noqa: BLE001
            review.update(state="failed", stage="Failed", error=str(exc)[:400], current=None)
            self.rev(review, "error", f"review failed: {str(exc)[:300]}")
        finally:
            self.save(test)
            self.review_cancel.discard(job_id)
            stop.set()
            hb.cancel()
            with suppress(Exception):     # the card also drives the desktop: give the VRAM back
                async with httpx.AsyncClient(timeout=30) as c:
                    await c.get(LLAMA_URL + "/unload", headers=headers)
                self.rev(review, "tool", "llama.unload  (VRAM freed)", icon="⏏")
                self.save(test)
            with suppress(Exception):
                await self.queue_request("/release", {"token": token}, timeout=6)

    def best(self, test: dict[str, Any]) -> str:
        """Highest average score; a tie goes to the earlier epoch (less risk of overfitting)."""
        best, top = "", -1.0
        for e in self.epoch_scores(test):
            if e["score"] is not None and e["score"] > top:
                best, top = e["label"], e["score"]
        return best

    async def verdict(self, client: httpx.AsyncClient, headers: dict[str, str], test: dict[str, Any], epochs: list[dict[str, Any]]) -> str:
        """A short text verdict from the score table and the per-render notes (text only, no images)."""
        lines = []
        for e in epochs:
            notes = [f"{i['review']['score']}: {i['review']['notes']}" for i in test["items"] if i["label"] == e["label"] and i.get("review")]
            lines.append(f"{e['label']}: average {e['score']}/10 over {e['reviewed']} render(s). " + " | ".join(n[:200] for n in notes[:4]))
        body = {"model": test["review"]["model"], "temperature": 0.3, "max_tokens": 400, "chat_template_kwargs": {"enable_thinking": False},
                "messages": [{"role": "user", "content": "These are review scores for each saved epoch of one LoRA training run:\n" + "\n".join(lines)
                              + f"\n\nThe highest average is {self.best(test)}. In 2-4 plain sentences say which epoch to use and why "
                                "(learned the concept vs. signs of overfitting in later epochs), and mention a runner-up if it is close."}]}
        with suppress(Exception):
            r = await client.post(LLAMA_URL + "/v1/chat/completions", json=body, headers=headers)
            if r.status_code == 200:
                text = re.sub(r"<think>.*?</think>", "", r.json()["choices"][0]["message"].get("content") or "", flags=re.S).strip()
                if text:
                    return text[:1200]
        return f"{self.best(test)} has the highest average score."
