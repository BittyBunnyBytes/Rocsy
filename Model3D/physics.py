"""Spring-bone physics for loose parts of a rigged character (chainmail skirts, capes, long hair, ...).

A *part* is a set of vertices that should swing on its own: picked by a preset (skirt / cape / hair) or by a
brush painted on the character's front picture. Each part becomes several short bone chains hanging from the
body bone it is attached to (the Hips for a skirt, the Chest for a cape, the Head for hair). The part's
vertices are re-weighted onto those chains.

Motion is simulated (Verlet particles: gravity, a spring back towards the rest shape, drag, segment lengths
kept, capsule colliders for the legs and torso) against every animation clip and *baked* into the clip as
keyframes, so the swing shows in any viewer or engine without a physics system. The same chains are also
described as VRM spring bones (VRMC_springBone) for engines that simulate them live.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

# Material presets: simulation values + the matching VRM spring-bone values.
MATERIALS = {
    "chainmail": {"label": "Chainmail (heavy, stiff)", "stiffness": 0.55, "gravity": 1.0, "drag": 0.55},
    "cloth": {"label": "Cloth (light, loose)", "stiffness": 0.25, "gravity": 0.6, "drag": 0.35},
    "leather": {"label": "Leather (stiff)", "stiffness": 0.8, "gravity": 0.7, "drag": 0.6},
    "hair": {"label": "Hair (bouncy)", "stiffness": 0.6, "gravity": 0.3, "drag": 0.25},
}
PRESETS = {"skirt": "Skirt / chainmail around the hips", "cape": "Cape on the back", "hair": "Long hair"}


@dataclass
class Chain:
    anchor: str                       # body joint the chain hangs from
    points: np.ndarray                # (n, 3) bind-pose joint positions, root first, tip last
    name: str = ""


@dataclass
class Part:
    name: str
    material: dict
    vertices: np.ndarray              # indices of the part's vertices
    chains: list[Chain] = field(default_factory=list)


# ------------------------------------------------------------------ picking vertices

def leg_radius(v: np.ndarray, sk, region: np.ndarray) -> float:
    """Typical thigh radius: median distance of thigh vertices to the thigh bone."""
    rs = []
    for side in ("L", "R"):
        a, b = sk.pos[f"UpperLeg.{side}"], sk.pos[f"LowerLeg.{side}"]
        sel = (region == f"leg{side}") & (v[:, 1] < a[1]) & (v[:, 1] > b[1])
        if np.any(sel):
            rs.append(float(np.median(_seg_dist(v[sel], a, b))))
    return float(np.mean(rs)) if rs else 0.06 * float(np.ptp(v[:, 1]))


def _seg_dist(p: np.ndarray, a: np.ndarray, b: np.ndarray) -> np.ndarray:
    ab = b - a
    t = np.clip(((p - a) @ ab) / max(float(ab @ ab), 1e-12), 0.0, 1.0)
    return np.linalg.norm(p - (a + t[:, None] * ab), axis=1)


def preset_vertices(kind: str, v: np.ndarray, sk, region: np.ndarray) -> tuple[np.ndarray, str]:
    """(vertex indices, anchor joint) of a preset part."""
    h = float(np.ptp(v[:, 1]))
    hips, chest, neck, head = (sk.pos[n] for n in ("Hips", "Chest", "Neck", "Head"))
    if kind == "skirt":
        # From the waist (between hips and spine) down to the hem. The hem is where the gap between the
        # legs starts (find_joints' crotch): above it the skirt covers the gap, below it everything belongs
        # to the legs. Taking leg vertices would leave them hanging on the chains while the legs swing.
        waist = hips[1] + 0.5 * (sk.pos["Spine"][1] - hips[1])
        hem = sk.crotch - 0.01 * h
        sel = (v[:, 1] < waist) & (v[:, 1] > hem) & np.isin(region, ["torso", "legL", "legR"])
        return np.nonzero(sel)[0], "Hips"
    if kind == "cape":
        # Behind the body, from the shoulders down: farther back than the torso's own back surface.
        torso = region == "torso"
        back_z = float(np.percentile(v[torso, 2], 8)) if np.any(torso) else chest[2] - 0.05 * h
        sel = (v[:, 1] < neck[1]) & (v[:, 1] > v[:, 1].min() + 0.08 * h) & (v[:, 2] < back_z + 0.005 * h) \
            & ~np.isin(region, ["armL", "armR"])
        return np.nonzero(sel)[0], "Chest"
    if kind == "hair":
        # Head-region vertices behind and below the middle of the head (a ponytail / long hair).
        sel = (region == "head") & (v[:, 2] < head[2] - 0.01 * h) & (v[:, 1] < head[1] + 0.03 * h)
        below = (v[:, 1] < neck[1]) & (v[:, 1] > chest[1]) & (v[:, 2] < chest[2] - 0.06 * h) & (region == "torso")
        return np.nonzero(sel | below)[0], "Head"
    raise ValueError(f"unknown preset {kind}")


def hugs_legs(idx: np.ndarray, v: np.ndarray, sk, region: np.ndarray) -> np.ndarray:
    """Mask over idx: vertices below the crotch that sit on a leg (closer to a leg bone than the leg's own
    radius + 15 %)."""
    if len(idx) == 0:
        return np.zeros(0, dtype=bool)
    h = float(np.ptp(v[:, 1]))
    r = leg_radius(v, sk, region) * 1.15
    p = v[idx]
    near = np.zeros(len(idx), dtype=bool)
    for side in ("L", "R"):
        for a, b in ((f"UpperLeg.{side}", f"LowerLeg.{side}"), (f"LowerLeg.{side}", f"Foot.{side}")):
            near |= _seg_dist(p, sk.pos[a], sk.pos[b]) < r
    return near & (p[:, 1] < sk.crotch - 0.01 * h)


def brush_vertices(mask: np.ndarray, box: tuple[int, int, int, int], v: np.ndarray) -> np.ndarray:
    """Vertices under a brush mask painted on the front picture. box = the object's bounding box in that
    picture; the picture is matched to the mesh's front view by height and centre (as painting does)."""
    lo, hi = v.min(0), v.max(0)
    x0, y0, x1, y1 = box
    s = (y1 - y0) / max(float(hi[1] - lo[1]), 1e-9)
    cxm, cym = (lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2
    cxi, cyi = (x0 + x1) / 2, (y0 + y1) / 2
    px = np.round(cxi + (v[:, 0] - cxm) * s).astype(int)
    py = np.round(cyi - (v[:, 1] - cym) * s).astype(int)
    H, W = mask.shape
    inside = (px >= 0) & (px < W) & (py >= 0) & (py < H)
    sel = np.zeros(len(v), dtype=bool)
    sel[inside] = mask[py[inside], px[inside]]
    return np.nonzero(sel)[0]


def anchor_for(vertices: np.ndarray, v: np.ndarray, sk) -> str:
    """The body joint a brushed part hangs from: the torso/head joint nearest to the part's top."""
    top = v[vertices][v[vertices, 1] > np.percentile(v[vertices, 1], 90)].mean(0)
    names = ["Hips", "Spine", "Chest", "Neck", "Head"]
    return min(names, key=lambda n: float(np.linalg.norm(sk.pos[n] - top)))


# ------------------------------------------------------------------ chains

def build_chains(name: str, idx: np.ndarray, anchor: str, v: np.ndarray, sk, segments: int = 3,
                 max_chains: int = 8) -> list[Chain]:
    """Chains hanging down through the part: its vertices are split by angle around the anchor's vertical
    axis; each angular slice with enough vertices gets a chain from the slice's top to its bottom."""
    pv = v[idx]
    a = sk.pos[anchor]
    ang = np.arctan2(pv[:, 0] - a[0], pv[:, 2] - a[2])        # 0 = front (+Z)
    bins = max_chains
    which = np.floor((ang + math.pi) / (2 * math.pi) * bins).astype(int) % bins
    h = float(np.ptp(v[:, 1]))
    chains = []
    for b in range(bins):
        sv = pv[which == b]
        if len(sv) < 40:
            continue
        top, bottom = float(np.percentile(sv[:, 1], 97)), float(np.percentile(sv[:, 1], 1))
        if top - bottom < 0.04 * h:
            continue
        pts = []
        for k in range(segments + 1):
            y = top + (bottom - top) * k / segments
            near = sv[np.abs(sv[:, 1] - y) < max(0.02 * h, (top - bottom) / (2 * segments))]
            if len(near) == 0:
                near = sv
            # The chain runs along the part's outer surface (a little outside its middle).
            centre = np.median(near, axis=0)
            pts.append(np.array([centre[0], y, centre[2]]))
        chains.append(Chain(anchor=anchor, points=np.array(pts), name=f"{name}.{len(chains) + 1}"))
    return chains


def chain_weights(part: Part, v: np.ndarray, sk, anchor_index: int, joint_index: dict[str, int]) -> dict[int, np.ndarray]:
    """{vertex: weights over all joints} for the part's vertices: blended between the two nearest chains
    (by angle) and along each chain by height; the top rows stay on the anchor bone."""
    out: dict[int, np.ndarray] = {}
    if not part.chains:
        return out
    a = sk.pos[part.chains[0].anchor]
    chain_ang = np.array([math.atan2(c.points[:, 0].mean() - a[0], c.points[:, 2].mean() - a[2]) for c in part.chains])
    pv = v[part.vertices]
    ang = np.arctan2(pv[:, 0] - a[0], pv[:, 2] - a[2])
    n_j = max(joint_index.values()) + 1
    for row, vi in enumerate(part.vertices):
        d = np.abs((chain_ang - ang[row] + math.pi) % (2 * math.pi) - math.pi)
        order = np.argsort(d)[:2]
        if len(order) == 1:
            pick = [(order[0], 1.0)]
        else:
            d0, d1 = d[order[0]], d[order[1]]
            w0 = d1 / max(d0 + d1, 1e-9)
            pick = [(order[0], w0), (order[1], 1 - w0)]
        w = np.zeros(n_j)
        y = pv[row, 1]
        for ci, cw in pick:
            c = part.chains[ci]
            top, bottom = c.points[0, 1], c.points[-1, 1]
            u = np.clip((top - y) / max(top - bottom, 1e-9), 0.0, 1.0) * (len(c.points) - 1)
            seg = min(int(u), len(c.points) - 2)
            f = u - seg
            # Bone `seg` runs from point seg to seg+1: weight it, fading into the next bone.
            names = [f"{c.name}.{k}" for k in range(len(c.points))]
            bw = np.zeros(n_j)
            bw[joint_index[names[seg]]] += 1 - f
            bw[joint_index[names[min(seg + 1, len(c.points) - 2)]]] += f
            # The first stretch blends from the body bone into the chain, so the part stays attached.
            attach = float(np.clip(1 - u / 0.6, 0.0, 1.0))
            w += cw * (1 - attach) * bw
            w[anchor_index] += cw * attach
        w = np.clip(w, 0, None)
        out[int(vi)] = w / max(w.sum(), 1e-9)
    return out


# ------------------------------------------------------------------ quaternions / FK

def q_mul(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    x1, y1, z1, w1 = a
    x2, y2, z2, w2 = b
    return np.array([w1 * x2 + x1 * w2 + y1 * z2 - z1 * y2, w1 * y2 - x1 * z2 + y1 * w2 + z1 * x2,
                     w1 * z2 + x1 * y2 - y1 * x2 + z1 * w2, w1 * w2 - x1 * x2 - y1 * y2 - z1 * z2])


def q_conj(q: np.ndarray) -> np.ndarray:
    return np.array([-q[0], -q[1], -q[2], q[3]])


def q_rot(q: np.ndarray, v: np.ndarray) -> np.ndarray:
    u = q[:3]
    s = q[3]
    return 2 * np.dot(u, v) * u + (s * s - np.dot(u, u)) * v + 2 * s * np.cross(u, v)


def q_between(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    a = a / max(np.linalg.norm(a), 1e-12)
    b = b / max(np.linalg.norm(b), 1e-12)
    c = np.cross(a, b)
    d = float(np.dot(a, b))
    if d < -0.999999:
        axis = np.cross([1.0, 0, 0], a)
        if np.linalg.norm(axis) < 1e-6:
            axis = np.cross([0, 1.0, 0], a)
        axis /= np.linalg.norm(axis)
        return np.array([axis[0], axis[1], axis[2], 0.0])
    q = np.array([c[0], c[1], c[2], 1 + d])
    return q / np.linalg.norm(q)


def q_nlerp(a: np.ndarray, b: np.ndarray, t: float) -> np.ndarray:
    if np.dot(a, b) < 0:
        b = -b
    q = a * (1 - t) + b * t
    return q / np.linalg.norm(q)


def sample(times: np.ndarray, keys: list, t: float, kind: str = "q") -> np.ndarray:
    """A clip channel at time t (looping)."""
    period = float(times[-1]) or 1.0
    t = t % period
    i = int(np.searchsorted(times, t, side="right")) - 1
    i = max(0, min(i, len(times) - 2))
    f = (t - times[i]) / max(times[i + 1] - times[i], 1e-9)
    a, b = np.asarray(keys[i], float), np.asarray(keys[i + 1], float)
    return q_nlerp(a, b, f) if kind == "q" else a * (1 - f) + b * f


def body_pose(clip: dict, t: float, sk, joints: list[str], parent: dict) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    """{joint: (world position, world rotation)} of the body at time t of a clip."""
    out: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    times = np.asarray(clip["_times"], float)
    for j in joints:
        local_q = sample(times, clip[j], t) if j in clip else np.array([0, 0, 0, 1.0])
        p = parent[j]
        if p is None:
            pos = sample(times, clip["_hips"], t, "v") if "_hips" in clip else sk.pos[j]
            out[j] = (pos, local_q)
        else:
            ppos, pq = out[p]
            out[j] = (ppos + q_rot(pq, sk.pos[j] - sk.pos[p]), q_mul(pq, local_q))
    return out


# ------------------------------------------------------------------ simulation

@dataclass
class Capsule:
    joint: str
    a: np.ndarray                     # bind-pose world endpoints
    b: np.ndarray
    radius: float


def colliders(v: np.ndarray, sk, region: np.ndarray, parts: list[Part]) -> list[Capsule]:
    """Leg and torso capsules, shrunk so no chain point starts inside one."""
    h = float(np.ptp(v[:, 1]))
    r_leg = leg_radius(v, sk, region)
    caps = []
    for side in ("L", "R"):
        caps.append(Capsule(f"UpperLeg.{side}", sk.pos[f"UpperLeg.{side}"], sk.pos[f"LowerLeg.{side}"], r_leg))
        caps.append(Capsule(f"LowerLeg.{side}", sk.pos[f"LowerLeg.{side}"], sk.pos[f"Foot.{side}"], r_leg * 0.85))
    torso = region == "torso"
    r_torso = float(np.median(_seg_dist(v[torso], sk.pos["Hips"], sk.pos["Neck"]))) * 0.8 if np.any(torso) else 0.1 * h
    caps.append(Capsule("Spine", sk.pos["Hips"], sk.pos["Chest"], r_torso))
    pts = [p for part in parts for c in part.chains for p in c.points[1:]]
    for cap in caps:
        if pts:
            nearest = float(_seg_dist(np.array(pts), cap.a, cap.b).min())
            cap.radius = max(0.2 * cap.radius, min(cap.radius, nearest * 0.92))
    return caps


def q_mat(q: np.ndarray) -> np.ndarray:
    x, y, z, w = q
    return np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                     [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                     [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])


def simulate(parts: list[Part], clip: dict, sk, joints: list[str], parent: dict, caps: list[Capsule],
             fps: int = 30) -> tuple[np.ndarray, dict[str, list[np.ndarray]]]:
    """Run the chains against one clip; return (key times, {chain joint: [local rotation per key]}).
    All chains step together as arrays (chains x points x 3)."""
    times = np.asarray(clip["_times"], float)
    period = float(times[-1])
    dt = 1.0 / 90.0
    h = float(sk.pos["Head"][1] - min(sk.pos["Foot.L"][1], sk.pos["Foot.R"][1])) / 0.9
    g_ref = 9.8 * h / 1.8                                    # gravity in model units (h = body height)
    key_times = np.linspace(0, period, max(2, int(round(period * fps)) + 1))
    chains = [(part, c) for part in parts for c in part.chains]
    if not chains:
        return key_times, {}
    n_pts = max(len(c.points) for _, c in chains)
    C = len(chains)
    rest = np.zeros((C, n_pts, 3))
    seg = np.zeros((C, n_pts - 1))
    k = np.zeros((C, 1, 1))
    grav = np.zeros((C, 1, 3))
    damp = np.zeros((C, 1, 1))
    anchors = []
    for ci, (part, c) in enumerate(chains):
        m = part.material
        rest[ci] = c.points - sk.pos[c.anchor]               # bind offsets from the anchor joint
        seg[ci] = np.linalg.norm(np.diff(c.points, axis=0), axis=1)
        # Spring strength relative to the chain's own segment length, so a material sags the same share
        # of a segment at rest on any model size (stiff chainmail ~18 %, loose cloth ~27 %).
        sag = max(0.05, 0.35 - 0.3 * m["stiffness"])
        k[ci] = g_ref / (sag * max(float(seg[ci].mean()), 1e-4))
        grav[ci, 0, 1] = -g_ref * m["gravity"]
        damp[ci] = 1.0 - min(0.2, 0.02 + 0.12 * m["drag"])
        anchors.append(c.anchor)
    anchor_names = sorted(set(anchors))
    anchor_idx = np.array([anchor_names.index(a) for a in anchors])
    x = np.array([c.points for _, c in chains], dtype=float)
    prev = x.copy()
    recorded: dict[str, list[np.ndarray]] = {f"{c.name}.{i}": [] for _, c in chains for i in range(len(c.points) - 1)}
    warm = 3                                                 # loops before recording, so motion has settled
    total_steps = int(round(period * (warm + 1) / dt))
    rec_from = period * warm
    next_key = 0
    for step in range(total_steps + 1):
        t = step * dt
        pose = body_pose(clip, t, sk, joints, parent)
        A_pos = np.array([pose[a][0] for a in anchor_names])
        A_rot = np.array([q_mat(pose[a][1]) for a in anchor_names])
        target = A_pos[anchor_idx][:, None, :] + np.einsum("cij,cpj->cpi", A_rot[anchor_idx], rest)
        caps_now = []
        for cap in caps:
            ppos, pq = pose[cap.joint]
            R = q_mat(pq)
            caps_now.append((ppos + R @ (cap.a - sk.pos[cap.joint]), ppos + R @ (cap.b - sk.pos[cap.joint]), cap.radius))
        # Verlet step for every point except the roots, which ride on the anchor bone.
        vel = (x - prev) * damp
        acc = grav + k * (target - x)
        prev = x.copy()
        x = x + vel + acc * dt * dt
        x[:, 0] = target[:, 0]
        prev[:, 0] = target[:, 0]
        for _ in range(3):
            for i in range(1, n_pts):
                d = x[:, i] - x[:, i - 1]
                n = np.linalg.norm(d, axis=1, keepdims=True)
                x[:, i] = x[:, i - 1] + d / np.maximum(n, 1e-9) * seg[:, i - 1:i]
                for a, b, r in caps_now:
                    ab = b - a
                    tt = np.clip((x[:, i] - a) @ ab / max(float(ab @ ab), 1e-12), 0.0, 1.0)
                    closest = a + tt[:, None] * ab
                    off = x[:, i] - closest
                    dist = np.linalg.norm(off, axis=1, keepdims=True)
                    inside = dist[:, 0] < r
                    if np.any(inside):
                        safe = np.where(dist > 1e-9, off / np.maximum(dist, 1e-9), np.array([0.0, 0.0, 1.0]))
                        x[inside, i] = closest[inside] + safe[inside] * r
        if next_key < len(key_times) and t + 1e-9 >= rec_from + key_times[next_key]:
            for ci, (_, c) in enumerate(chains):
                world_q = pose[c.anchor][1]
                for i in range(len(c.points) - 1):
                    rest_dir = c.points[i + 1] - c.points[i]
                    cur = x[ci, i + 1] - x[ci, i]
                    want = q_mul(q_between(q_rot(world_q, rest_dir), cur), world_q)
                    local = q_mul(q_conj(world_q), want)
                    recorded[f"{c.name}.{i}"].append(local / np.linalg.norm(local))
                    world_q = want
            next_key += 1
    # The loop's last key equals its first (seamless loop).
    for name, qs in recorded.items():
        while len(qs) < len(key_times):
            qs.append(qs[-1] if qs else np.array([0, 0, 0, 1.0]))
        qs[-1] = qs[0]
    return key_times, recorded


# ------------------------------------------------------------------ VRM spring bones

def vrm_extension(parts: list[Part], node_of: dict[str, int], caps: list[Capsule], sk) -> dict:
    colliders_json, group = [], []
    for cap in caps:
        colliders_json.append({"node": node_of[cap.joint], "shape": {"capsule": {
            "offset": [float(c) for c in cap.a - sk.pos[cap.joint]],
            "radius": float(cap.radius),
            "tail": [float(c) for c in cap.b - sk.pos[cap.joint]]}}})
        group.append(len(colliders_json) - 1)
    springs = []
    h = float(sk.pos["Head"][1] - min(sk.pos["Foot.L"][1], sk.pos["Foot.R"][1]))
    for part in parts:
        m = part.material
        for c in part.chains:
            joints = [{"node": node_of[f"{c.name}.{i}"], "hitRadius": round(0.012 * h, 4),
                       "stiffness": round(0.5 + 3.5 * m["stiffness"], 3), "gravityPower": round(m["gravity"], 3),
                       "gravityDir": [0, -1, 0], "dragForce": round(min(1.0, 0.2 + 0.7 * m["drag"]), 3)}
                      for i in range(len(c.points) - 1)]
            joints.append({"node": node_of[f"{c.name}.{len(c.points) - 1}"]})
            springs.append({"name": c.name, "joints": joints, "colliderGroups": [0]})
    return {"specVersion": "1.0", "colliders": colliders_json,
            "colliderGroups": [{"name": "Body", "colliders": group}], "springs": springs}
