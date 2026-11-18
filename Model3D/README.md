# 3D Models (`/Model3D`)

Rocsy's image-to-3D studio. A picture (or front/left/back/right views) becomes a GLB mesh with
**Hunyuan3D**, rendered by the shared ComfyUI with its built-in Hunyuan3D v2 nodes (no custom node
pack needed) and taking GPU turns through bunny-lock as app `Model3D`.

## What a job does

1. **Text prompt** (optional): the Image studio draws the picture as a hidden job (`/api/internal/generate`,
   its own GPU turn) before this job queues, so the two never wait on each other.
2. **Shape**: Hunyuan3D -> voxels -> mesh.
3. **Paint** (on by default): DecimateMesh -> UnwrapMesh -> RenderMesh silhouettes from a 4-camera orthographic
   orbit (front, yaw 90, back, yaw 270). Each picture is scaled/moved onto its silhouette (left/right go on
   whichever side fits better), then BakeTextureFromViews projects them onto a UV texture, growing colour
   into areas no picture sees, and ApplyTextureToMesh -> SaveGLB. Both passes share the shape nodes, so
   ComfyUI's cache reuses the mesh. A single picture paints the front well; the back is filled in from
   the visible colours (give the MV model a back view for a real one).

## Redo, edit, repaint

Hunyuan3D only makes shapes from pictures; it can't edit a mesh. Editing goes through the picture:

- **Redo** (`/api/jobs/{id}/redo`): the same job again as a new one, with a new shape seed, a newly drawn
  picture (prompt/edit jobs) and changed detail/steps/texture/paint.
- **Edit** (`/api/jobs/{id}/edit`): the Image studio (Qwen Image 2.1, the picture as reference) edits the
  model's picture from an instruction; then either only the colours are repainted onto the same mesh
  (`keep_shape`, ~10 s of GPU after the edit) or the model is rebuilt from the edited picture.
- **Repaint** an uploaded `.glb/.obj/.stl` (`mesh` + `mesh_up` y|z on `POST /api/jobs`): painted from a front
  picture, or from a prompt that the Image studio paints over a clay render of the model. Bounds are read
  from the file (GLB node transforms, OBJ, ASCII/binary STL) to frame the cameras.
- `/Model3D/?job=<id>` opens a given model in the viewer.

## Characters, full texture, skeleton

- **Character** (prompt jobs): the Image studio is asked for a clothed, front-facing T-pose (`CHARACTER_PROMPT` +
  `CHARACTER_NEGATIVE`; an early version without them returned an unclothed anatomy figure).
- **Paint all sides**: Qwen Image 2.1 redraws the front picture from the back, left and right (`draw_views`), and
  all four are baked; left/right go on whichever side camera their silhouette fits.
- **Rig** (`rig.py`, CPU/numpy, ~0.3 s): joints from the mesh's height profile (arm line, neck, crotch gap, leg
  clusters), 17-joint glTF skeleton, region-limited inverse-distance skin weights, and Idle/Walk/Wave clips.
  The un-rigged model is kept as `static.glb`; `POST /api/jobs/{id}/rig` (re)rigs any finished model from it.
  Needs a standing T-pose (or clear A-pose) humanoid; anything else gets a "No skeleton: ..." note instead.
- Skin weights are smooth ramps along each limb (chest->upper arm, elbow, wrist; hip->thigh, knee, ankle), not
  inverse distance, so shoulders and elbows don't pinch. Triangles bridging the two legs below the crotch
  (Hunyuan3D fuses legs that nearly touch) are dropped so the legs separate cleanly. Clips: Idle, Walk, Run, Wave.
- **Physics** (`physics.py`, `POST /api/jobs/{id}/physics`): loose parts from presets (skirt = waist to the
  detected hem, cape, long hair) and/or a brush mask painted on the front picture (projected through the model;
  leg-hugging vertices below the crotch stay on the legs). Each part gets up to 8 chains of 3 bones hanging from
  its anchor joint; a vectorized Verlet simulation (gravity, a spring relative to segment length, drag, leg/torso
  capsules) runs against every clip and is baked in at 30 fps (~4 s per rig). The same chains are written as
  `VRMC_springBone` springs + capsule colliders. Materials: chainmail, cloth, leather, hair.
- Viewer links: `?job=<id>`, `&anim=Walk`, `&orbit=180`, `&paint=off`.

## Engines

| Model | Notes |
|---|---|
| Hunyuan3D 2.1 / 2 MV | shape only; the studio paints it (projection, optional drawn side views) |
| **TRELLIS.2** | `trellis_2_int8_convrot` + shared VAEs + `dino_v3_L_naf`: shape **and** PBR textures (colour, metal, roughness, normal, AO) from one picture, ~2 min |
| **Pixal3D** | `pixal3d_int8_convrot` (TencentARC, on TRELLIS.2): pixel-aligned, uses MoGe-2 for the camera FOV, ~1.5 min |

TRELLIS.2/Pixal3D follow Comfy-Org's template `3d_pixal3d_trellis2_image_to_model` (`trellis_nodes`): BiRefNet
background removal, structure -> shape -> upsample (detail 1024/1536/2048) -> texture, then remesh, decimate,
unwrap and bake. They appear in the model list once their files are in place.

## Rigging methods

- **Built-in humanoid** (`rig.py`): instant, CPU; Idle/Walk/Run/Wave; physics.
- **MagicArticulate AI** (`comfy_nodes/MagicArticulate`, `rig.rig_ai`): an AI skeleton for any shape (fingers,
  toes, animals, props) in a GPU turn; nearest-bone weights smoothed over the surface. Humanoids get the same
  four clips (joints matched by position), anything else gets Sway/Wiggle. No physics on AI rigs yet.

Painting saves the unwrapped mesh after pass A (`unwrap.glb`) so pass B doesn't regenerate the shape
(ComfyUI runs with `--cache-none`).

## Models

Put either checkpoint in ComfyUI's `models/checkpoints/` (Models → Manager can download them):

| File | Input |
|---|---|
| `hunyuan_3d_v2.1.safetensors` (Comfy-Org/hunyuan3D_2.1_repackaged) | one picture |
| `hunyuan3d-dit-v2-mv_fp16.safetensors` (tencent/Hunyuan3D-2mv) | front + optional left/back/right |

Both bundle their image encoder and VAE. Any checkpoint whose name contains `hunyuan` and `3d`
is offered; `-mv` / `_mv` in the name marks a multi-view model. "Remove the background first"
appears when ComfyUI-Easy-Use is installed (RMBG-1.4, downloaded on first use).

## Files

- `app.py` - FastAPI app (jobs, ComfyUI workflow, bunny-lock lease).
- `static/` - the page; `static/lib/model-viewer.min.js` is @google/model-viewer 4.0.0
  (BSD-3-Clause), vendored because the gateway's CSP only allows same-origin scripts.
- `data/jobs/<id>/` - `job.json`, the prepared input pictures and `model.glb`.

## Deploy

`docker compose up -d --build --no-deps model3d` (from `BunnyBytes/`) after `app.py` edits;
`static/` is bind-mounted, so page edits only need a reload.
