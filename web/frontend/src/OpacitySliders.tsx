import { useAppStore } from "./state";

function OpacitySlider({
  label,
  value,
  onChange,
  disabled,
}: {
  label: string;
  value: number;
  onChange: (value: number) => void;
  disabled?: boolean;
}) {
  return (
    <label className="opacityRow">
      <span className="opacityLabel">
        {label}
        <span className="opacityValue">{Math.round(value * 100)}%</span>
      </span>
      <input
        type="range"
        min={0}
        max={1}
        step={0.01}
        value={value}
        disabled={disabled}
        onChange={(event) => onChange(Number(event.target.value))}
      />
    </label>
  );
}

export function OpacitySliders() {
  const osmOpacity = useAppStore((s) => s.osmOpacity);
  const esriOpacity = useAppStore((s) => s.esriOpacity);
  const hillshadeOpacity = useAppStore((s) => s.hillshadeOpacity);
  const overlayOpacity = useAppStore((s) => s.overlayOpacity);
  const setOsmOpacity = useAppStore((s) => s.setOsmOpacity);
  const setEsriOpacity = useAppStore((s) => s.setEsriOpacity);
  const setHillshadeOpacity = useAppStore((s) => s.setHillshadeOpacity);
  const setOverlayOpacity = useAppStore((s) => s.setOverlayOpacity);
  const hasHillshade = Boolean(useAppStore((s) => s.baseLayerId));
  const hasOverlay = Boolean(useAppStore((s) => s.overlayLayerId));
  return (
    <>
      <OpacitySlider label="OpenStreetMap" value={osmOpacity} onChange={setOsmOpacity} />
      <OpacitySlider label="Esri imagery" value={esriOpacity} onChange={setEsriOpacity} />
      <OpacitySlider
        label="Hillshade"
        value={hillshadeOpacity}
        onChange={setHillshadeOpacity}
        disabled={!hasHillshade}
      />
      <OpacitySlider
        label="Feature overlay"
        value={overlayOpacity}
        onChange={setOverlayOpacity}
        disabled={!hasOverlay}
      />
    </>
  );
}
