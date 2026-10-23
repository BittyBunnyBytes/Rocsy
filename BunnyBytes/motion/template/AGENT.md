# Rocsy Motion project (Remotion 4.0.527, React 19)

You write the video as React code; `$R motion render NAME` renders it on the GPU.

- `src/Root.tsx` registers compositions (`<Composition id width height fps durationInFrames defaultProps>`).
  Width/height even and 128-4096 px. Render one with `--composition ID` (default `Main`).
- Animate only from `useCurrentFrame()` / `useVideoConfig()` with `interpolate`, `spring`,
  `<Sequence from durationInFrames>`, `<Series>`. Never CSS transitions/animations, `setTimeout` or
  `Math.random()` (use `random("seed")` from remotion): every frame must be deterministic.
- Media: put files in `public/` and use `staticFile("name.png")` with `<Img>`, `<OffthreadVideo>` (or
  `<Video>` from @remotion/media), `<Audio>`. Get studio outputs with `$R fetch krea1,mus1,vid2`, then copy
  them into `public/`. Audio in the composition ends up in the video.
- Installed: remotion, @remotion/transitions (TransitionSeries, fade/slide/wipe/flip/clockWipe),
  @remotion/captions (createTikTokStyleCaptions), @remotion/media-utils (audio visualisation:
  useAudioData + visualizeAudio), @remotion/media, @remotion/google-fonts (loadFont; fetched online),
  @remotion/shapes, @remotion/noise, @remotion/paths, @remotion/layout-utils (fitText, measureText),
  @remotion/motion-blur (Trail, CameraMotionBlur), @remotion/lottie (+ lottie-web). No other npm
  packages: you can't install any. Local fonts: DejaVu Sans, Liberation, Noto (incl. CJK, colour emoji).
- Props: `--props '{"title":"..."}'` overrides defaultProps for one render.
- Check it compiles: `$R motion check NAME`. Look at a frame: `$R motion still NAME --frame 60`
  (a PNG path for your vision tool). Then `$R motion render NAME`, `$R wait`.
- The finished render becomes a normal video job (motion1): `postedit`, `captions`, `montage` and
  `--ref` work on it.

## Drawing over an existing video (captions, a ball bouncing over the words, stickers)

1. `$R motion assets NAME --ref vid3` copies the finished video into `public/vid3.mp4` and tells you
   its length, size and fps.
2. Make a composition exactly that long and that size, the video as the bottom layer:

```tsx
// Root.tsx: <Composition id="Main" component={Main} fps={30} width={1920} height={1080}
//                        durationInFrames={Math.round(37.5 * 30)} />
import { AbsoluteFill, OffthreadVideo, staticFile, useCurrentFrame, useVideoConfig, spring } from "remotion";
export const Main = () => {
  const frame = useCurrentFrame();
  const { fps, width } = useVideoConfig();
  const beat = (frame % 15) / 15;                       // one bounce every half second
  const y = 760 - Math.sin(beat * Math.PI) * 120;
  return (
    <AbsoluteFill>
      <OffthreadVideo src={staticFile("vid3.mp4")} />   {/* keeps the video's own sound */}
      <div style={{ position: "absolute", left: (frame * 6) % width, top: y, width: 56, height: 56,
                    borderRadius: 28, background: "radial-gradient(circle at 30% 30%, #fff6a0, #ff9f1c)",
                    boxShadow: "0 10px 25px rgba(0,0,0,.4)" }} />
    </AbsoluteFill>
  );
};
```

To follow the words, put the caption timings in props (`--props '{"words":[[0.4,0.9,"Fresh"],...]}'`,
seconds) and move the ball to the word whose start/end holds `frame / fps`.
The render becomes a new video job and appears in the chat by itself.
