"use client";

import * as RadixSlider from "@radix-ui/react-slider";

export function Slider({
  value,
  min,
  max,
  step,
  onValueChange,
  label,
}: {
  value: number;
  min: number;
  max: number;
  step: number;
  onValueChange: (v: number) => void;
  label?: string;
}) {
  return (
    <RadixSlider.Root
      value={[value]}
      min={min}
      max={max}
      step={step}
      onValueChange={(v) => onValueChange(v[0])}
      aria-label={label}
      style={{
        position: "relative",
        display: "flex",
        alignItems: "center",
        width: "100%",
        height: 18,
        cursor: "pointer",
        touchAction: "none",
      }}
    >
      <RadixSlider.Track
        style={{
          position: "relative",
          flexGrow: 1,
          height: 4,
          borderRadius: 3,
          background: "var(--line-14)",
        }}
      >
        <RadixSlider.Range
          style={{
            position: "absolute",
            height: "100%",
            borderRadius: 3,
            background: "var(--color-accent)",
          }}
        />
      </RadixSlider.Track>
      <RadixSlider.Thumb
        className="argus-thumb"
        style={{
          display: "block",
          width: 18,
          height: 18,
          borderRadius: "50%",
          background: "var(--color-accent)",
          border: "3px solid var(--color-ground)",
          boxShadow: "0 0 0 1px var(--accent-50)",
        }}
      />
    </RadixSlider.Root>
  );
}
