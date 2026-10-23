import { AbsoluteFill, interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";

export const mainSchemaDefaults = { title: "Hello from Rocsy", subtitle: "Made with Remotion" };

export const Main: React.FC<typeof mainSchemaDefaults> = ({ title, subtitle }) => {
  const frame = useCurrentFrame();
  const { fps, durationInFrames } = useVideoConfig();
  const pop = spring({ frame, fps, config: { damping: 12 } });
  const fadeOut = interpolate(frame, [durationInFrames - 20, durationInFrames], [1, 0], { extrapolateLeft: "clamp" });
  return (
    <AbsoluteFill style={{ background: "linear-gradient(135deg, #1b1035, #3a1c71 55%, #d76d77)", justifyContent: "center", alignItems: "center", opacity: fadeOut }}>
      <h1 style={{ fontFamily: "DejaVu Sans, sans-serif", fontSize: 140, color: "white", margin: 0, transform: `scale(${pop})` }}>{title}</h1>
      <p style={{ fontFamily: "DejaVu Sans, sans-serif", fontSize: 54, color: "#ffe14d", opacity: interpolate(frame, [20, 40], [0, 1], { extrapolateRight: "clamp" }) }}>{subtitle}</p>
    </AbsoluteFill>
  );
};
