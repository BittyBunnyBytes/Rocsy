"""Humanoid auto-rig for the 3D Models studio.

Takes a GLB from the studio (one mesh, Y-up, facing +Z, standing in a T-pose or A-pose), finds the body's
joints from the mesh's own shape, builds a 16-joint glTF skeleton with smooth skin weights, adds three
preview animations (Idle, Walk, Wave) and writes a rigged GLB that Blender, Unity, Godot and <model-viewer>
load as-is.

How joints are found (all from the vertices, no ML):
  * height profile: the mesh is cut into horizontal slices; the widest slice in the upper body is the arm
    line (T-pose), the narrowest slice above it the neck, and the highest slice whose middle is empty is
    the crotch (the gap between the legs);
  * arms: vertices well outside the chest on each side; the farthest one is the fingertip, and the
    shoulder -> fingertip line is split at human proportions (elbow 43 %, wrist 78 %);
  * legs: the two vertex clusters below the crotch, followed down to the ankles.
Skin weights: every vertex is first given a body region (head, torso, each arm, each leg) so legs never
pull on each other and arms never pull on the torso far from the shoulder; inside its region it is
weighted by inverse distance (power 4) to the bones it may follow, up to four bones per vertex.
"""
from __future__ import annotations

import json
import math
import struct
from dataclasses import dataclass, field

import numpy as np

import physics

JOINTS = ["Hips", "Spine", "Chest", "Neck", "Head",
          "UpperArm.L", "LowerArm.L", "Hand.L", "UpperArm.R", "LowerArm.R", "Hand.R",
          "UpperLeg.L", "LowerLeg.L", "Foot.L", "UpperLeg.R", "LowerLeg.R", "Foot.R"]
PARENT = {"Hips": None, "Spine": "Hips", "Chest": "Spine", "Neck": "Chest", "Head": "Neck",
          "UpperArm.L": "Chest", "LowerArm.L": "UpperArm.L", "Hand.L": "LowerArm.L",
          "UpperArm.R": "Chest", "LowerArm.R": "UpperArm.R", "Hand.R": "LowerArm.R",
          "UpperLeg.L": "Hips", "LowerLeg.L": "UpperLeg.L", "Foot.L": "LowerLeg.L",
          "UpperLeg.R": "Hips", "LowerLeg.R": "UpperLeg.R", "Foot.R": "LowerLeg.R"}


class RigError(Exception):
    """The mesh doesn't look like a standing humanoid we can rig."""


# ------------------------------------------------------------------ GLB in/out

COMP = {5120: np.int8, 5121: np.uint8, 5122: np.int16, 5123: np.uint16, 5125: np.uint32, 5126: np.float32}
NCOMP = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}


@dataclass
class Glb:
    gltf: dict
    bin: bytes

    @classmethod
    def read(cls, data: bytes) -> "Glb":
        if data[:4] != b"glTF":
            raise RigError("not a GLB file")
        offset, gltf, binary = 12, None, b""
        while offset < len(data):
            length, kind = struct.unpack("<II", data[offset:offset + 8])
            chunk = data[offset + 8: offset + 8 + length]
            if kind == 0x4E4F534A:
                gltf = json.loads(chunk)
            elif kind == 0x004E4942:
                binary = chunk
            offset += 8 + length
        return cls(gltf, binary)

    def accessor(self, index: int) -> np.ndarray:
        acc = self.gltf["accessors"][index]
        view = self.gltf["bufferViews"][acc["bufferView"]]
        dtype = np.dtype(COMP[acc["componentType"]])
        n = NCOMP[acc["type"]]
        start = view.get("byteOffset", 0) + acc.get("byteOffset", 0)
        stride = view.get("byteStride", 0) or dtype.itemsize * n
        raw = np.frombuffer(self.bin, dtype=np.uint8, count=stride * (acc["count"] - 1) + dtype.itemsize * n, offset=start)
        if stride == dtype.itemsize * n:
            arr = raw.view(dtype).reshape(acc["count"], n)
        else:
            arr = np.lib.stride_tricks.as_strided(raw.view(dtype), shape=(acc["count"], n), strides=(stride, dtype.itemsize))
        return np.array(arr)

    def view_bytes(self, index: int) -> bytes:
        view = self.gltf["bufferViews"][index]
        start = view.get("byteOffset", 0)
        return self.bin[start:start + view["byteLength"]]


class GlbWriter:
    def __init__(self) -> None:
        self.gltf: dict = {"asset": {"version": "2.0", "generator": "Rocsy 3D Models auto-rig"},
                           "buffers": [], "bufferViews": [], "accessors": []}
        self.blob = bytearray()

    def view(self, data: bytes, target: int | None = None) -> int:
        while len(self.blob) % 4:
            self.blob.append(0)
        entry = {"buffer": 0, "byteOffset": len(self.blob), "byteLength": len(data)}
        if target:
            entry["target"] = target
        self.blob += data
        self.gltf["bufferViews"].append(entry)
        return len(self.gltf["bufferViews"]) - 1

    def accessor(self, arr: np.ndarray, kind: str, comp: int, target: int | None = None, minmax: bool = False) -> int:
        arr = np.ascontiguousarray(arr)
        entry = {"bufferView": self.view(arr.tobytes(), target), "componentType": comp,
                 "count": int(arr.shape[0]), "type": kind}
        if minmax:
            flat = arr.reshape(arr.shape[0], -1)
            entry["min"] = [float(v) for v in flat.min(0)]
            entry["max"] = [float(v) for v in flat.max(0)]
        self.gltf["accessors"].append(entry)
        return len(self.gltf["accessors"]) - 1

    def bytes(self) -> bytes:
        while len(self.blob) % 4:
            self.blob.append(0)
        self.gltf["buffers"] = [{"byteLength": len(self.blob)}]
        js = json.dumps(self.gltf, separators=(",", ":")).encode()
        js += b" " * (-len(js) % 4)
        total = 12 + 8 + len(js) + 8 + len(self.blob)
        return (struct.pack("<III", 0x46546C67, 2, total) + struct.pack("<II", len(js), 0x4E4F534A) + js
                + struct.pack("<II", len(self.blob), 0x004E4942) + bytes(self.blob))


# ------------------------------------------------------------------ joints

@dataclass
class Skeleton:
    pos: dict[str, np.ndarray] = field(default_factory=dict)      # world-space joint positions (bind pose)
    tips: dict[str, np.ndarray] = field(default_factory=dict)     # bone end points for leaf joints
    pose: str = "T"                                               # T or A (arms out / arms angled down)
    arm_angle: dict[str, float] = field(default_factory=dict)     # bind-pose arm angle below horizontal, degrees
    crotch: float = 0.0                                           # top of the gap between the legs (skirt hem)


def find_joints(v: np.ndarray) -> Skeleton:
    lo, hi = v.min(0), v.max(0)
    h = float(hi[1] - lo[1])
    if h <= 0:
        raise RigError("the mesh is flat")
    width = float(hi[0] - lo[0])
    if width > 2.2 * h:
        raise RigError("the model is much wider than tall; it doesn't look like a standing character")
    y0 = float(lo[1])
    rel = (v[:, 1] - y0) / h                           # 0 at the feet, 1 at the top of the head
    cx = float(np.median(v[(rel > 0.45) & (rel < 0.6), 0])) if np.any((rel > 0.45) & (rel < 0.6)) else float(v[:, 0].mean())
    dx = v[:, 0] - cx
    bins = 120
    idx = np.clip((rel * bins).astype(int), 0, bins - 1)
    half = np.zeros(bins)
    gap = np.zeros(bins, dtype=bool)
    zmid = np.zeros(bins)
    for b in range(bins):
        sel = idx == b
        if not np.any(sel):
            continue
        d = dx[sel]
        half[b] = max(-d.min(), d.max())
        zmid[b] = float(np.median(v[sel, 2]))
        # The middle of the slice is empty while both sides have vertices: the gap between the legs.
        gap[b] = (not np.any(np.abs(d) < 0.02 * h)) and np.any(d < -0.03 * h) and np.any(d > 0.03 * h)

    def bin_y(b: int) -> float:
        return y0 + (b + 0.5) / bins * h

    lower = [b for b in range(int(0.2 * bins), int(0.62 * bins)) if gap[b]]
    crotch_b = max(lower) if lower else int(0.47 * bins)
    crotch_y = bin_y(crotch_b)
    # Arm line: the widest slice between 55 % and 92 % of the height.
    band = range(int(0.55 * bins), int(0.92 * bins))
    arm_b = max(band, key=lambda b: half[b])
    hip_half = float(np.median(half[max(0, crotch_b - 2): crotch_b + 6])) or 0.12 * h
    # Chest width: just below the arms (where only the torso is cut).
    chest_b = max(crotch_b + 2, arm_b - int(0.1 * bins))
    chest_half = float(np.median(half[chest_b - 2: chest_b + 2])) or 0.14 * h
    tpose = half[arm_b] > 1.7 * max(chest_half, hip_half)
    # Neck: the narrowest slice between the arm line and the top.
    neck_band = [b for b in range(arm_b + 2, int(0.97 * bins)) if half[b] > 0]
    neck_b = min(neck_band, key=lambda b: half[b]) if neck_band else int(0.85 * bins)
    neck_y = bin_y(neck_b)

    def zat(y: float) -> float:
        return float(zmid[int(np.clip((y - y0) / h * bins, 0, bins - 1))])

    sk = Skeleton(pose="T" if tpose else "A", crotch=crotch_y)
    hips_y = crotch_y + 0.04 * h
    sk.pos["Hips"] = np.array([cx, hips_y, zat(hips_y)])
    sk.pos["Neck"] = np.array([cx, neck_y, zat(neck_y)])
    sk.pos["Spine"] = sk.pos["Hips"] + (sk.pos["Neck"] - sk.pos["Hips"]) * 0.33
    sk.pos["Chest"] = sk.pos["Hips"] + (sk.pos["Neck"] - sk.pos["Hips"]) * 0.68
    head_y = neck_y + 0.25 * (float(hi[1]) - neck_y)
    sk.pos["Head"] = np.array([cx, head_y, zat(head_y)])
    sk.tips["Head"] = np.array([cx, float(hi[1]), zat(head_y)])
    shoulder_y = bin_y(arm_b) if tpose else sk.pos["Chest"][1] + 0.55 * (neck_y - sk.pos["Chest"][1])

    for side, sign in (("L", 1.0), ("R", -1.0)):
        shoulder = np.array([cx + sign * chest_half * 0.85, shoulder_y, zat(shoulder_y)])
        # Arm vertices: well outside the chest on this side and above the hips.
        arm = v[(sign * dx > chest_half * 1.15) & (v[:, 1] > hips_y + 0.05 * h)]
        if len(arm) < 20:
            raise RigError("couldn't find the arms; ask for a T-pose with the arms away from the body")
        tip = arm[np.argmax(sign * (arm[:, 0] - cx))]
        if not tpose:   # A-pose: the farthest point along the arm's direction from the shoulder
            d = arm - shoulder
            tip = arm[np.argmax(np.linalg.norm(d, axis=1))]
        reach = tip - shoulder
        sk.pos[f"UpperArm.{side}"] = shoulder
        sk.pos[f"LowerArm.{side}"] = shoulder + reach * 0.43
        sk.pos[f"Hand.{side}"] = shoulder + reach * 0.78
        sk.tips[f"Hand.{side}"] = tip
        sk.arm_angle[side] = math.degrees(math.atan2(-(reach[1]), abs(reach[0]) or 1e-6))
        # Legs: the vertex cluster on this side below the crotch.
        leg = v[(sign * dx > 0.0) & (v[:, 1] < crotch_y)]
        if len(leg) < 20:
            raise RigError("couldn't find the legs")

        def leg_center(y: float) -> np.ndarray:
            near = leg[np.abs(leg[:, 1] - y) < 0.03 * h]
            if len(near) == 0:
                near = leg
            return np.array([float(np.median(near[:, 0])), y, float(np.median(near[:, 2]))])

        ankle_y = y0 + 0.05 * h
        top = leg_center(crotch_y - 0.03 * h)
        top[1] = hips_y - 0.02 * h
        ankle = leg_center(ankle_y)
        sk.pos[f"UpperLeg.{side}"] = top
        sk.pos[f"LowerLeg.{side}"] = leg_center(top[1] + (ankle_y - top[1]) * 0.5)
        sk.pos[f"Foot.{side}"] = ankle
        foot = leg[leg[:, 1] < y0 + 0.08 * h]
        toe = foot[np.argmax(foot[:, 2])] if len(foot) else ankle + np.array([0, -0.04 * h, 0.08 * h])
        sk.tips[f"Foot.{side}"] = np.array([ankle[0], y0, toe[2]])
    return sk


# ------------------------------------------------------------------ skin weights

def _segment_distance(p: np.ndarray, a: np.ndarray, b: np.ndarray) -> np.ndarray:
    ab = b - a
    t = np.clip(((p - a) @ ab) / max(float(ab @ ab), 1e-12), 0.0, 1.0)
    return np.linalg.norm(p - (a + t[:, None] * ab), axis=1)


def _ramp(t: np.ndarray, centre: float, width: float) -> np.ndarray:
    """0 before centre - width/2, 1 after centre + width/2, smooth (smoothstep) in between."""
    x = np.clip((t - (centre - width / 2)) / max(width, 1e-9), 0.0, 1.0)
    return x * x * (3 - 2 * x)


def _chain_weights(t: np.ndarray, bounds: list[tuple[float, float]]) -> np.ndarray:
    """Weights for bones along one limb, from the position t along it. bounds = [(centre, width)] of the
    joints between consecutive bones; returns (N, len(bounds) + 1) weights summing to 1."""
    ups = [_ramp(t, c, w) for c, w in bounds]
    cols = []
    for i in range(len(bounds) + 1):
        lo = ups[i - 1] if i > 0 else np.ones_like(t)
        hi = ups[i] if i < len(bounds) else np.zeros_like(t)
        cols.append(np.clip(lo - hi, 0.0, 1.0))
    w = np.stack(cols, axis=1)
    return w / np.maximum(w.sum(1, keepdims=True), 1e-9)


def body_regions(v: np.ndarray, sk: Skeleton) -> np.ndarray:
    """Body region per vertex: head, torso, armL, armR, legL, legR."""
    h = float(v[:, 1].max() - v[:, 1].min())
    cx = sk.pos["Hips"][0]
    dx = v[:, 0] - cx
    neck_y, hips_y = sk.pos["Neck"][1], sk.pos["Hips"][1]
    region = np.full(len(v), "torso", dtype=object)
    region[v[:, 1] > neck_y - 0.01 * h] = "head"
    for side, sign in (("L", 1.0), ("R", -1.0)):
        sh = sk.pos[f"UpperArm.{side}"]
        axis = sk.tips[f"Hand.{side}"] - sh
        length = float(np.linalg.norm(axis))
        t = (v - sh) @ (axis / length) / length
        radial = np.linalg.norm((v - sh) - np.outer(t * length, axis / length), axis=1)
        # Past the shoulder line along the arm, and within an arm's thickness of its axis.
        arm = (t > -0.04) & (t < 1.08) & (radial < np.maximum(0.10 * h, 0.22 * length)) & (sign * dx > 0)
        region[arm & (v[:, 1] > hips_y)] = f"arm{side}"
        region[(v[:, 1] < hips_y - 0.02 * h) & (sign * dx >= 0)] = f"leg{side}"
    return region


def skin_weights(v: np.ndarray, sk: Skeleton) -> tuple[np.ndarray, np.ndarray]:
    """Linear-blend skin weights from smooth ramps along each limb (no inverse-distance falloff, which
    pinches shoulders and elbows). Each vertex follows only its own region's chain."""
    J = {n: i for i, n in enumerate(JOINTS)}
    h = float(v[:, 1].max() - v[:, 1].min())
    region = body_regions(v, sk)
    W = np.zeros((len(v), len(JOINTS)), dtype=np.float64)

    def put(sel: np.ndarray, bones: list[str], w: np.ndarray) -> None:
        for k, b in enumerate(bones):
            W[sel, J[b]] += w[:, k]

    # Torso + head: by height, Hips -> Spine -> Chest -> Neck -> Head, blends centred on each joint.
    sel = (region == "torso") | (region == "head")
    y = v[sel, 1]
    ys = [sk.pos[n][1] for n in ("Spine", "Chest", "Neck", "Head")]
    put(sel, ["Hips", "Spine", "Chest", "Neck", "Head"],
        _chain_weights(y, [(ys[0], 0.08 * h), (ys[1], 0.08 * h), (ys[2] + 0.01 * h, 0.05 * h), (ys[3], 0.05 * h)]))
    for side in ("L", "R"):
        # Arm: along the shoulder -> fingertip axis. Chest/UpperArm blend straddles the shoulder.
        sel = region == f"arm{side}"
        if np.any(sel):
            sh = sk.pos[f"UpperArm.{side}"]
            axis = sk.tips[f"Hand.{side}"] - sh
            length = float(np.linalg.norm(axis))
            t = (v[sel] - sh) @ (axis / length) / length
            put(sel, ["Chest", f"UpperArm.{side}", f"LowerArm.{side}", f"Hand.{side}"],
                _chain_weights(t, [(0.03, 0.16), (0.43, 0.10), (0.78, 0.07)]))
        # Leg: along hip -> ankle, Hips/UpperLeg blend at the top, knee, ankle.
        sel = region == f"leg{side}"
        if np.any(sel):
            top, knee, ankle = sk.pos[f"UpperLeg.{side}"], sk.pos[f"LowerLeg.{side}"], sk.pos[f"Foot.{side}"]
            span = float(top[1] - ankle[1]) or 1.0
            t = (top[1] - v[sel, 1]) / span
            tk = float((top[1] - knee[1]) / span)
            put(sel, ["Hips", f"UpperLeg.{side}", f"LowerLeg.{side}", f"Foot.{side}"],
                _chain_weights(t, [(-0.02, 0.10), (tk, 0.10), (1.0, 0.06)]))
    return W


def _top4(W: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    top = np.argsort(-W, axis=1)[:, :4]
    tw = np.take_along_axis(W, top, axis=1)
    tw = np.where(tw > 1e-4, tw, 0.0)
    tw /= np.maximum(tw.sum(1, keepdims=True), 1e-12)
    return top.astype(np.uint16), tw.astype(np.float32)


# ------------------------------------------------------------------ animations

def quat(axis: tuple[float, float, float], deg: float) -> np.ndarray:
    a = np.array(axis, dtype=float)
    a /= np.linalg.norm(a)
    s = math.sin(math.radians(deg) / 2)
    return np.array([a[0] * s, a[1] * s, a[2] * s, math.cos(math.radians(deg) / 2)])


def qmul(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    x1, y1, z1, w1 = a
    x2, y2, z2, w2 = b
    return np.array([w1 * x2 + x1 * w2 + y1 * z2 - z1 * y2, w1 * y2 - x1 * z2 + y1 * w2 + z1 * x2,
                     w1 * z2 + x1 * y2 - y1 * x2 + z1 * w2, w1 * w2 - x1 * x2 - y1 * y2 - z1 * z2])


def animations(sk: Skeleton) -> dict[str, dict[str, list[np.ndarray]]]:
    """{clip: {joint: [quaternion per frame]}} + "_times" and "_hips" (translation keys) per clip.
    The character faces +Z; its left (L) is +X. Arms are lowered from the bind pose to ~10 degrees out of
    vertical for idle/walk."""
    clips: dict = {}
    down = {s: (78.0 - sk.arm_angle.get(s, 0.0)) for s in ("L", "R")}   # how far to lower each arm

    def arm_down(side: str, extra_swing: float = 0.0, extra_down: float = 0.0) -> np.ndarray:
        sign = 1.0 if side == "L" else -1.0
        lower = quat((0, 0, 1), -sign * (down[side] + extra_down))
        return qmul(quat((1, 0, 0), extra_swing), lower)

    def elbow(side: str, deg: float) -> np.ndarray:
        """Bend the elbow forward (+Z). In the bind frame the forearm lies along +X (L) / -X (R), so the
        bend is a turn about Y, opposite signs per side (a turn about X would only twist the forearm)."""
        return quat((0, 1, 0), -deg if side == "L" else deg)

    # Idle: 2.4 s loop, breathing and a slow arm sway.
    n = 25
    times = np.linspace(0, 2.4, n)
    idle = {j: [] for j in JOINTS}
    for t in times:
        ph = 2 * math.pi * t / 2.4
        for j in JOINTS:
            q = np.array([0, 0, 0, 1.0])
            if j == "Spine":
                q = quat((1, 0, 0), 1.5 * math.sin(ph))
            elif j == "Chest":
                q = quat((1, 0, 0), -1.2 * math.sin(ph))
            elif j == "Head":
                q = quat((0, 1, 0), 4 * math.sin(ph / 2))
            elif j.startswith("UpperArm"):
                q = arm_down(j[-1], extra_down=2.5 * math.sin(ph))
            elif j.startswith("LowerArm"):
                q = elbow(j[-1], 8.0)
            idle[j].append(q)
    clips["Idle"] = {"_times": times, **idle,
                     "_hips": [sk.pos["Hips"] + np.array([0, 0.004 * math.sin(2 * math.pi * t / 2.4), 0]) for t in times]}

    # Walk: 1.1 s loop.
    period = 1.1
    times = np.linspace(0, period, 23)
    walk = {j: [] for j in JOINTS}
    h = float(sk.pos["Neck"][1] - sk.tips["Foot.L"][1]) / 0.85
    for t in times:
        ph = 2 * math.pi * t / period
        swing = math.sin(ph)
        for j in JOINTS:
            q = np.array([0, 0, 0, 1.0])
            if j == "UpperLeg.L":
                q = quat((1, 0, 0), -28 * swing)
            elif j == "UpperLeg.R":
                q = quat((1, 0, 0), 28 * swing)
            elif j == "LowerLeg.L":
                q = quat((1, 0, 0), 35 * max(0.0, math.sin(ph + math.pi * 0.6)))
            elif j == "LowerLeg.R":
                q = quat((1, 0, 0), 35 * max(0.0, math.sin(ph + math.pi * 1.6)))
            elif j == "Foot.L":
                q = quat((1, 0, 0), -10 * swing)
            elif j == "Foot.R":
                q = quat((1, 0, 0), 10 * swing)
            elif j == "UpperArm.L":
                q = arm_down("L", extra_swing=24 * swing)
            elif j == "UpperArm.R":
                q = arm_down("R", extra_swing=-24 * swing)
            elif j.startswith("LowerArm"):
                q = elbow(j[-1], 15.0)
            elif j == "Spine":
                q = quat((0, 1, 0), 5 * swing)
            elif j == "Chest":
                q = quat((0, 1, 0), -8 * swing)
            walk[j].append(q)
    clips["Walk"] = {"_times": times, **walk,
                     "_hips": [sk.pos["Hips"] + np.array([0, -0.012 * h * abs(math.cos(2 * math.pi * t / period)), 0]) for t in times]}

    # Run: 0.7 s loop, bigger strides, knees high, arms pumping, leaning forward.
    period = 0.7
    times = np.linspace(0, period, 22)
    run = {j: [] for j in JOINTS}
    for t in times:
        ph = 2 * math.pi * t / period
        swing = math.sin(ph)
        for j in JOINTS:
            q = np.array([0, 0, 0, 1.0])
            if j == "UpperLeg.L":
                q = quat((1, 0, 0), -45 * swing - 10)
            elif j == "UpperLeg.R":
                q = quat((1, 0, 0), 45 * swing - 10)
            elif j == "LowerLeg.L":
                q = quat((1, 0, 0), 20 + 60 * max(0.0, math.sin(ph + math.pi * 0.55)))
            elif j == "LowerLeg.R":
                q = quat((1, 0, 0), 20 + 60 * max(0.0, math.sin(ph + math.pi * 1.55)))
            elif j == "Foot.L":
                q = quat((1, 0, 0), -15 * swing)
            elif j == "Foot.R":
                q = quat((1, 0, 0), 15 * swing)
            elif j == "UpperArm.L":
                q = arm_down("L", extra_swing=40 * swing, extra_down=-8)
            elif j == "UpperArm.R":
                q = arm_down("R", extra_swing=-40 * swing, extra_down=-8)
            elif j.startswith("LowerArm"):
                q = elbow(j[-1], 75.0)
            elif j == "Spine":
                q = qmul(quat((1, 0, 0), 9.0), quat((0, 1, 0), 7 * swing))
            elif j == "Chest":
                q = quat((0, 1, 0), -10 * swing)
            elif j == "Head":
                q = quat((1, 0, 0), -6.0)
            run[j].append(q)
    clips["Run"] = {"_times": times, **run,
                    "_hips": [sk.pos["Hips"] + np.array([0, 0.025 * h * abs(math.sin(2 * math.pi * t / period)) - 0.02 * h, 0]) for t in times]}

    # Wave: 2 s loop, right hand raised and waving.
    times = np.linspace(0, 2.0, 21)
    wave = {j: [] for j in JOINTS}
    for t in times:
        ph = 2 * math.pi * t / 1.0
        for j in JOINTS:
            q = np.array([0, 0, 0, 1.0])
            if j == "UpperArm.L":
                q = arm_down("L")
            elif j == "LowerArm.L":
                q = elbow("L", 8.0)
            elif j == "UpperArm.R":
                # Upper arm just above shoulder height (raising it further pinches the shoulder).
                q = quat((0, 0, 1), -(22 + sk.arm_angle.get("R", 0.0)))
            elif j == "LowerArm.R":
                q = quat((0, 0, 1), -(72 + 20 * math.sin(ph)))              # forearm up, waving side to side
            elif j == "Head":
                q = quat((0, 0, 1), -5.0)
            elif j == "Spine":
                q = quat((0, 0, 1), 3.0)
            wave[j].append(q)
    clips["Wave"] = {"_times": times, **wave, "_hips": [sk.pos["Hips"] for _ in times]}
    return clips


def _carry_material(src: "Glb", prim: dict, out: "GlbWriter", new_prim: dict) -> None:
    """Copy the primitive's material and every texture it uses (base colour, metal/roughness, normal, AO,
    emissive) into the new GLB."""
    g = out.gltf
    if "material" in prim and src.gltf.get("materials"):
        mat = json.loads(json.dumps(src.gltf["materials"][prim["material"]]))
        g["images"], g["textures"], g["samplers"] = [], [], []
        remap: dict[int, int] = {}

        def carry(ref: dict | None) -> None:
            if not ref or "index" not in ref:
                return
            old = ref["index"]
            if old not in remap:
                tex = src.gltf["textures"][old]
                img = src.gltf["images"][tex["source"]]
                view = out.view(src.view_bytes(img["bufferView"]))
                g["images"].append({"bufferView": view, "mimeType": img.get("mimeType", "image/png")})
                sampler = src.gltf["samplers"][tex["sampler"]] if "sampler" in tex and src.gltf.get("samplers") else {}
                g["samplers"].append(sampler)
                g["textures"].append({"source": len(g["images"]) - 1, "sampler": len(g["samplers"]) - 1})
                remap[old] = len(g["textures"]) - 1
            ref["index"] = remap[old]

        pbr = mat.get("pbrMetallicRoughness") or {}
        for key in ("baseColorTexture", "metallicRoughnessTexture"):
            carry(pbr.get(key))
        for key in ("normalTexture", "occlusionTexture", "emissiveTexture"):
            carry(mat.get(key))
        for key in ("images", "textures", "samplers"):
            if not g[key]:
                del g[key]
        g["materials"] = [mat]
        new_prim["material"] = 0


# ------------------------------------------------------------------ main

def rig(data: bytes, parts_spec: list[dict] | None = None) -> tuple[bytes, dict]:
    """Rig a studio GLB. parts_spec: loose parts to simulate (physics.py), each
    {"kind": "skirt"|"cape"|"hair"|"brush", "material": "chainmail"|..., optional "stiffness"/"gravity"/"drag"
    overrides, and for "brush": "mask" (bool array over the front picture) and "box" (object bbox in it)}.
    Returns (rigged GLB bytes, report)."""
    src = Glb.read(data)
    meshes = src.gltf.get("meshes") or []
    if len(meshes) != 1 or len(meshes[0]["primitives"]) != 1:
        raise RigError("expected one mesh with one part (a model made in this studio)")
    prim = meshes[0]["primitives"][0]
    pos = src.accessor(prim["attributes"]["POSITION"]).astype(np.float32)
    uv = src.accessor(prim["attributes"]["TEXCOORD_0"]).astype(np.float32) if "TEXCOORD_0" in prim["attributes"] else None
    normals = src.accessor(prim["attributes"]["NORMAL"]).astype(np.float32) if "NORMAL" in prim["attributes"] else None
    indices = src.accessor(prim["indices"]).astype(np.uint32).reshape(-1)
    v64 = pos.astype(np.float64)
    sk = find_joints(v64)
    W = skin_weights(v64, sk)
    region = body_regions(v64, sk)
    # Loose parts: chains + re-weighting.
    parts: list[physics.Part] = []
    taken = np.zeros(len(v64), dtype=bool)
    part_notes = []
    for spec in parts_spec or []:
        kind = spec.get("kind")
        material = dict(physics.MATERIALS.get(spec.get("material") or "cloth", physics.MATERIALS["cloth"]))
        for key in ("stiffness", "gravity", "drag"):
            if spec.get(key) is not None:
                material[key] = float(np.clip(float(spec[key]), 0.0, 1.0))
        if kind == "brush":
            idx = physics.brush_vertices(np.asarray(spec["mask"], bool), tuple(spec["box"]), v64)
            # A brush stroke goes through the whole model: below the crotch, keep whatever hugs a leg
            # (within the thigh's own thickness) on the leg, or it tears away when the legs swing.
            idx = idx[~physics.hugs_legs(idx, v64, sk, region)]
            anchor = physics.anchor_for(idx, v64, sk) if len(idx) else "Hips"
            name = spec.get("name") or "Brushed part"
        else:
            idx, anchor = physics.preset_vertices(kind, v64, sk, region)
            name = {"skirt": "Skirt", "cape": "Cape", "hair": "Hair"}[kind]
        idx = idx[~taken[idx]]
        if len(idx) < 60:
            part_notes.append(f"{name}: too few vertices found, skipped")
            continue
        name = f"{name}{len(parts) + 1}" if any(p.name == name for p in parts) else name
        part = physics.Part(name=name, material=material, vertices=idx)
        part.chains = physics.build_chains(name, idx, anchor, v64, sk)
        if not part.chains:
            part_notes.append(f"{name}: couldn't build chains, skipped")
            continue
        taken[idx] = True
        parts.append(part)
    chain_joints = [f"{c.name}.{i}" for p in parts for c in p.chains for i in range(len(c.points))]
    all_joints = JOINTS + chain_joints
    if chain_joints:
        J_all = {n: i for i, n in enumerate(all_joints)}
        W = np.hstack([W, np.zeros((len(W), len(chain_joints)))])
        for part in parts:
            anchor_i = J_all[part.chains[0].anchor]
            for vi, w in physics.chain_weights(part, v64, sk, anchor_i, J_all).items():
                W[vi] = w
    joints, weights = _top4(W)
    # Triangles joining the left leg to the right leg below the crotch: Hunyuan3D fuses legs that nearly
    # touch, and those faces stretch into a slab when the legs swing apart. They are inner webbing: drop them.
    tri = indices.reshape(-1, 3)
    tri_region = region[tri]
    tri_y = v64[tri, 1].max(1)
    h_all = float(np.ptp(v64[:, 1]))
    bridge = (tri_region == "legL").any(1) & (tri_region == "legR").any(1) & (tri_y < sk.crotch - 0.02 * h_all)
    if np.any(bridge):
        indices = tri[~bridge].reshape(-1)

    out = GlbWriter()
    g = out.gltf
    # Skin data as bytes (glTF allows normalized unsigned-byte weights and byte joint indices when there
    # are fewer than 256 joints): ~0.75 MB instead of ~3 MB on a 126k-vertex model.
    wq = np.round(weights * 255).astype(np.int32)
    wq[np.arange(len(wq)), np.argmax(wq, axis=1)] += 255 - wq.sum(1)      # each row sums to exactly 255
    joint_comp, joint_arr = (5121, joints.astype(np.uint8)) if len(all_joints) < 256 else (5123, joints)
    attrs = {"POSITION": out.accessor(pos, "VEC3", 5126, 34962, minmax=True),
             "JOINTS_0": out.accessor(joint_arr, "VEC4", joint_comp, 34962),
             "WEIGHTS_0": out.accessor(np.clip(wq, 0, 255).astype(np.uint8), "VEC4", 5121, 34962)}
    g_norm = len(out.gltf["accessors"]) - 1
    out.gltf["accessors"][g_norm]["normalized"] = True
    if uv is not None:
        attrs["TEXCOORD_0"] = out.accessor(uv, "VEC2", 5126, 34962)
    if normals is not None:
        attrs["NORMAL"] = out.accessor(normals, "VEC3", 5126, 34962)
    new_prim = {"attributes": attrs, "indices": out.accessor(indices.reshape(-1, 1), "SCALAR", 5125, 34963)}
    _carry_material(src, prim, out, new_prim)
    g["meshes"] = [{"name": "Character", "primitives": [new_prim]}]

    # Nodes: 0 = scene root, 1 = mesh (skinned), 2.. = joints (body, then chains).
    parent_of = dict(PARENT)
    pos_of = dict(sk.pos)
    for part in parts:
        for c in part.chains:
            for i, pt in enumerate(c.points):
                parent_of[f"{c.name}.{i}"] = c.anchor if i == 0 else f"{c.name}.{i - 1}"
                pos_of[f"{c.name}.{i}"] = pt
    node_of = {j: 2 + i for i, j in enumerate(all_joints)}
    nodes = [{"name": "Character", "children": [1, node_of["Hips"]]}, {"name": "Body", "mesh": 0, "skin": 0}]
    for j in all_joints:
        parent = parent_of[j]
        local = pos_of[j] - (pos_of[parent] if parent else 0)
        node = {"name": j, "translation": [float(c) for c in local]}
        kids = [node_of[c] for c in all_joints if parent_of[c] == j]
        if kids:
            node["children"] = kids
        nodes.append(node)
    g["nodes"] = nodes
    g["scenes"] = [{"nodes": [0]}]
    g["scene"] = 0
    ibm = np.stack([np.array([[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0],
                              [-pos_of[j][0], -pos_of[j][1], -pos_of[j][2], 1]], dtype=np.float32).reshape(16)
                    for j in all_joints])
    g["skins"] = [{"name": "Armature", "joints": [node_of[j] for j in all_joints], "skeleton": node_of["Hips"],
                   "inverseBindMatrices": out.accessor(ibm, "MAT4", 5126)}]
    caps = physics.colliders(v64, sk, region, parts) if parts else []

    anims = []
    for name, clip in animations(sk).items():
        t_acc = out.accessor(np.asarray(clip["_times"], dtype=np.float32).reshape(-1, 1), "SCALAR", 5126, minmax=True)
        samplers, channels = [], []
        for j in JOINTS:
            qs = np.asarray(clip[j], dtype=np.float32)
            if np.allclose(qs, [0, 0, 0, 1]):
                continue
            samplers.append({"input": t_acc, "output": out.accessor(qs, "VEC4", 5126), "interpolation": "LINEAR"})
            channels.append({"sampler": len(samplers) - 1, "target": {"node": node_of[j], "path": "rotation"}})
        hips = np.asarray(clip["_hips"], dtype=np.float32)
        samplers.append({"input": t_acc, "output": out.accessor(hips, "VEC3", 5126), "interpolation": "LINEAR"})
        channels.append({"sampler": len(samplers) - 1, "target": {"node": node_of["Hips"], "path": "translation"}})
        if parts:
            # The loose parts' motion, simulated against this clip and baked in at 30 fps.
            key_times, chain_q = physics.simulate(parts, clip, sk, JOINTS, PARENT, caps)
            k_acc = out.accessor(np.asarray(key_times, dtype=np.float32).reshape(-1, 1), "SCALAR", 5126, minmax=True)
            for jname, qs in chain_q.items():
                samplers.append({"input": k_acc, "output": out.accessor(np.asarray(qs, dtype=np.float32), "VEC4", 5126),
                                 "interpolation": "LINEAR"})
                channels.append({"sampler": len(samplers) - 1, "target": {"node": node_of[jname], "path": "rotation"}})
        anims.append({"name": name, "samplers": samplers, "channels": channels})
    g["animations"] = anims
    if parts:
        g["extensionsUsed"] = ["VRMC_springBone"]
        g["extensions"] = {"VRMC_springBone": physics.vrm_extension(parts, node_of, caps, sk)}
    report = {"pose": sk.pose, "joints": len(all_joints), "body_joints": len(JOINTS),
              "animations": [a["name"] for a in anims], "vertices": int(len(pos)),
              "physics": [{"name": p.name, "chains": len(p.chains), "vertices": int(len(p.vertices)),
                           "anchor": p.chains[0].anchor} for p in parts],
              "notes": part_notes, "leg_bridges_removed": int(bridge.sum())}
    return out.bytes(), report


# ------------------------------------------------------------------ AI skeletons (MagicArticulate)

def ai_skin_weights(v: np.ndarray, tri: np.ndarray, joints: np.ndarray, bones: np.ndarray,
                    iterations: int = 12) -> np.ndarray:
    """Weights for any skeleton: each vertex starts on its nearest bone (distance to the bone segment), then
    the weights are averaged with the vertex's mesh neighbours a few times. Averaging only spreads along
    the connected surface, so two legs (or a wing and the body) don't pull on each other unless the mesh
    joins them. Bone b is driven by its parent joint bones[b][0]; returns (N, joints) weights."""
    n = len(v)
    # Weld UV-seam duplicates so the smoothing crosses seams.
    _, weld = np.unique(np.round(v, 6), axis=0, return_inverse=True)
    weld = weld.reshape(-1)
    m = int(weld.max()) + 1
    wv = np.zeros((m, 3))
    wv[weld] = v
    wt = weld[tri]
    dist = np.stack([_seg(wv, joints[a], joints[b]) for a, b in bones], axis=1)     # (m, bones)
    owner = np.argmin(dist, axis=1)
    W = np.zeros((m, len(joints)))
    W[np.arange(m), bones[owner, 0]] = 1.0
    # Neighbour averaging over the welded mesh's edges.
    e = np.concatenate([wt[:, [0, 1]], wt[:, [1, 2]], wt[:, [2, 0]]])
    e = np.concatenate([e, e[:, ::-1]])
    deg = np.bincount(e[:, 0], minlength=m).astype(float)[:, None]
    for _ in range(iterations):
        acc = np.zeros_like(W)
        np.add.at(acc, e[:, 0], W[e[:, 1]])
        W = 0.5 * W + 0.5 * acc / np.maximum(deg, 1)
    return W[weld]


def _seg(p: np.ndarray, a: np.ndarray, b: np.ndarray) -> np.ndarray:
    ab = b - a
    t = np.clip(((p - a) @ ab) / max(float(ab @ ab), 1e-12), 0.0, 1.0)
    return np.linalg.norm(p - (a + t[:, None] * ab), axis=1)


def _ai_parents(n: int, bones: np.ndarray, root: int) -> list[int | None]:
    parent: list[int | None] = [None] * n
    kids: dict[int, list[int]] = {}
    for a, b in bones:
        kids.setdefault(int(a), []).append(int(b))
        kids.setdefault(int(b), [])
    seen = {root}
    order = [root]
    for j in order:                                          # breadth-first from the root
        for k in kids.get(j, []):
            if k not in seen:
                seen.add(k)
                parent[k] = j
                order.append(k)
    for j in range(n):                                       # stray joints hang from the root
        if j not in seen:
            parent[j] = root
    return parent


def ai_animations(v: np.ndarray, joints: np.ndarray, parent: list[int | None], root: int) -> tuple[dict, dict]:
    """Preview clips for an AI skeleton. If the mesh is also a standing humanoid, the AI joints nearest to the
    built-in humanoid joints take that joint's motion (Idle/Walk/Run/Wave; fingers and toes follow their
    hand/foot). Otherwise generic Sway and Wiggle clips. Returns (clips by name, mapping report)."""
    try:
        sk = find_joints(v)
        humanoid = animations(sk)
    except RigError:
        sk, humanoid = None, None
    if sk is not None:
        mapping: dict[str, int] = {}
        used: set[int] = set()
        h = float(np.ptp(v[:, 1]))
        for name in JOINTS:
            d = np.linalg.norm(joints - sk.pos[name], axis=1)
            for j in np.argsort(d):
                if int(j) not in used and d[j] < 0.12 * h:
                    mapping[name] = int(j)
                    used.add(int(j))
                    break
        core = ["Hips", "UpperArm.L", "UpperArm.R", "UpperLeg.L", "UpperLeg.R", "LowerLeg.L", "LowerLeg.R"]
        if all(c in mapping for c in core):
            mapping["Hips"] = root                           # the clips' hip bob drives the root
            clips = {}
            for cname, clip in humanoid.items():
                out = {"_times": clip["_times"]}
                for name, j in mapping.items():
                    out[j] = clip[name]
                out["_root"] = [joints[root] + (p - sk.pos["Hips"]) for p in clip["_hips"]]
                clips[cname] = out
            return clips, {"style": "humanoid", "mapped": len(mapping)}
    # Generic: each joint turns a little, more towards the ends of chains, with a delay along each chain.
    depth = [0] * len(joints)
    for j in range(len(joints)):
        k, d = j, 0
        while parent[k] is not None and d < 64:
            k, d = parent[k], d + 1
        depth[j] = d
    clips = {}
    for cname, (period, amp, axis_mix) in {"Sway": (3.0, 6.0, 0.4), "Wiggle": (1.2, 14.0, 0.8)}.items():
        times = np.linspace(0, period, 31)
        out = {"_times": times}
        for j in range(len(joints)):
            if parent[j] is None:
                continue
            a = amp * min(1.0, 0.35 + 0.15 * depth[j])
            ph = 0.6 * depth[j]
            out[j] = [qmul(quat((0, 0, 1), a * math.sin(2 * math.pi * t / period - ph)),
                           quat((1, 0, 0), a * axis_mix * math.sin(2 * math.pi * t / period * 0.5 - ph)))
                      for t in times]
        out["_root"] = [joints[root] for _ in times]
        clips[cname] = out
    return clips, {"style": "generic"}


def rig_ai(data: bytes, skeleton: dict) -> tuple[bytes, dict]:
    """Rig a studio GLB with an AI skeleton ({"joints", "bones", "root"} from MagicArticulate)."""
    src = Glb.read(data)
    meshes = src.gltf.get("meshes") or []
    if len(meshes) != 1 or len(meshes[0]["primitives"]) != 1:
        raise RigError("expected one mesh with one part (a model made in this studio)")
    prim = meshes[0]["primitives"][0]
    pos = src.accessor(prim["attributes"]["POSITION"]).astype(np.float32)
    v64 = pos.astype(np.float64)
    indices = src.accessor(prim["indices"]).astype(np.uint32).reshape(-1)
    joints = np.asarray(skeleton["joints"], dtype=np.float64)
    bones = np.asarray(skeleton["bones"], dtype=np.int64).reshape(-1, 2)
    root = int(skeleton.get("root", 0))
    if len(joints) < 2 or len(bones) < 1:
        raise RigError("the AI skeleton came back empty")
    parent = _ai_parents(len(joints), bones, root)
    # On a standing humanoid, drop the triangles that fuse the two legs below the crotch (as rig() does).
    bridges = 0
    try:
        sk_h = find_joints(v64)
        region = body_regions(v64, sk_h)
        tri = indices.reshape(-1, 3)
        tr = region[tri]
        bridge = (tr == "legL").any(1) & (tr == "legR").any(1) & (v64[tri, 1].max(1) < sk_h.crotch - 0.02 * float(np.ptp(v64[:, 1])))
        if np.any(bridge):
            indices = tri[~bridge].reshape(-1)
            bridges = int(bridge.sum())
    except RigError:
        pass
    # Bones as the model sees them: each joint drives the segment(s) to its children.
    drive = np.array([[parent[j], j] for j in range(len(joints)) if parent[j] is not None])
    W = ai_skin_weights(v64, indices.reshape(-1, 3), joints, drive)
    jw, ww = _top4(W)
    names = [f"Joint{j}" if j != root else "Root" for j in range(len(joints))]
    out = GlbWriter()
    g = out.gltf
    wq = np.round(ww * 255).astype(np.int32)
    wq[np.arange(len(wq)), np.argmax(wq, axis=1)] += 255 - wq.sum(1)
    attrs = {"POSITION": out.accessor(pos, "VEC3", 5126, 34962, minmax=True),
             "JOINTS_0": out.accessor(jw.astype(np.uint8) if len(joints) < 256 else jw, "VEC4",
                                      5121 if len(joints) < 256 else 5123, 34962),
             "WEIGHTS_0": out.accessor(np.clip(wq, 0, 255).astype(np.uint8), "VEC4", 5121, 34962)}
    g["accessors"][attrs["WEIGHTS_0"]]["normalized"] = True
    for key in ("TEXCOORD_0", "NORMAL"):
        if key in prim["attributes"]:
            attrs[key] = out.accessor(src.accessor(prim["attributes"][key]).astype(np.float32),
                                      "VEC2" if key == "TEXCOORD_0" else "VEC3", 5126, 34962)
    new_prim = {"attributes": attrs, "indices": out.accessor(indices.reshape(-1, 1), "SCALAR", 5125, 34963)}
    _carry_material(src, prim, out, new_prim)
    g["meshes"] = [{"name": "Model", "primitives": [new_prim]}]
    node_of = {j: 2 + j for j in range(len(joints))}
    nodes = [{"name": "Model", "children": [1, node_of[root]]}, {"name": "Body", "mesh": 0, "skin": 0}]
    for j in range(len(joints)):
        p = parent[j]
        local = joints[j] - (joints[p] if p is not None else 0)
        node = {"name": names[j], "translation": [float(c) for c in local]}
        kids = [node_of[k] for k in range(len(joints)) if parent[k] == j]
        if kids:
            node["children"] = kids
        nodes.append(node)
    g["nodes"] = nodes
    g["scenes"] = [{"nodes": [0]}]
    g["scene"] = 0
    ibm = np.stack([np.array([[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [-p[0], -p[1], -p[2], 1]],
                             dtype=np.float32).reshape(16) for p in joints])
    g["skins"] = [{"name": "Armature", "joints": [node_of[j] for j in range(len(joints))], "skeleton": node_of[root],
                   "inverseBindMatrices": out.accessor(ibm, "MAT4", 5126)}]
    clips, report = ai_animations(v64, joints, parent, root)
    anims = []
    for cname, clip in clips.items():
        t_acc = out.accessor(np.asarray(clip["_times"], dtype=np.float32).reshape(-1, 1), "SCALAR", 5126, minmax=True)
        samplers, channels = [], []
        for j in range(len(joints)):
            if j not in clip:
                continue
            qs = np.asarray(clip[j], dtype=np.float32)
            if np.allclose(qs, [0, 0, 0, 1]):
                continue
            samplers.append({"input": t_acc, "output": out.accessor(qs, "VEC4", 5126), "interpolation": "LINEAR"})
            channels.append({"sampler": len(samplers) - 1, "target": {"node": node_of[j], "path": "rotation"}})
        rootp = np.asarray(clip["_root"], dtype=np.float32)
        samplers.append({"input": t_acc, "output": out.accessor(rootp, "VEC3", 5126), "interpolation": "LINEAR"})
        channels.append({"sampler": len(samplers) - 1, "target": {"node": node_of[root], "path": "translation"}})
        anims.append({"name": cname, "samplers": samplers, "channels": channels})
    g["animations"] = anims
    return out.bytes(), {"method": "magicarticulate", "joints": int(len(joints)), "bones": int(len(bones)),
                         "animations": [a["name"] for a in anims], "vertices": int(len(pos)),
                         "leg_bridges_removed": bridges, **report}
