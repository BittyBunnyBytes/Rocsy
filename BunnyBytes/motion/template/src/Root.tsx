import { Composition } from "remotion";
import { Main, mainSchemaDefaults } from "./Main";

// Every <Composition> here can be rendered by id (default "Main").
// width/height must be even, 128-4096 px (GPU encode limits); 30 or 60 fps.
export const RemotionRoot: React.FC = () => {
  return (
    <Composition
      id="Main"
      component={Main}
      durationInFrames={150}
      fps={30}
      width={1920}
      height={1080}
      defaultProps={mainSchemaDefaults}
    />
  );
};
