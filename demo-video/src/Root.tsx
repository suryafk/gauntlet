import { Composition } from "remotion";
import { Demo } from "./Demo";
import timeline from "./timeline.json";

export const RemotionRoot: React.FC = () => (
  <Composition
    id="GauntletDemo"
    component={Demo}
    durationInFrames={timeline.totalFrames}
    fps={timeline.fps}
    width={timeline.width}
    height={timeline.height}
  />
);
