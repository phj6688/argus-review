"use client";

import { useEffect, useRef } from "react";

/* A single floating tooltip for the whole console. Any element with a
   `data-tip` attribute shows it on hover/focus. Positioned synchronously
   with `fixed` so it never clips inside cards or the table. */
export function TipProvider() {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const tip = ref.current;
    if (!tip) return;

    const closestTipped = (target: EventTarget | null): HTMLElement | null => {
      if (!(target instanceof Element)) return null;
      return target.closest<HTMLElement>("[data-tip]");
    };

    const show = (el: HTMLElement) => {
      const text = el.getAttribute("data-tip");
      if (!text) return;
      tip.textContent = text;
      const r = el.getBoundingClientRect();
      const tr = tip.getBoundingClientRect();
      let left = r.left + r.width / 2 - tr.width / 2;
      left = Math.max(8, Math.min(left, window.innerWidth - tr.width - 8));
      let top = r.top - tr.height - 9;
      if (top < 8) top = r.bottom + 9;
      tip.style.left = `${left}px`;
      tip.style.top = `${top}px`;
      tip.style.opacity = "1";
      tip.style.transform = "translateY(0)";
    };

    const hide = () => {
      tip.style.opacity = "0";
      tip.style.transform = "translateY(4px)";
    };

    const onOver = (e: Event) => {
      const el = closestTipped(e.target);
      if (el) show(el);
    };
    const onOut = (e: Event) => {
      if (closestTipped(e.target)) hide();
    };

    document.addEventListener("mouseover", onOver);
    document.addEventListener("mouseout", onOut);
    document.addEventListener("focusin", onOver);
    document.addEventListener("focusout", onOut);
    window.addEventListener("scroll", hide, true);
    return () => {
      document.removeEventListener("mouseover", onOver);
      document.removeEventListener("mouseout", onOut);
      document.removeEventListener("focusin", onOver);
      document.removeEventListener("focusout", onOut);
      window.removeEventListener("scroll", hide, true);
    };
  }, []);

  return (
    <div
      ref={ref}
      aria-hidden
      style={{
        position: "fixed",
        zIndex: 99999,
        left: 0,
        top: 0,
        maxWidth: 270,
        padding: "9px 12px",
        borderRadius: 9,
        background: "var(--tip-bg)",
        border: "1px solid var(--accent-22)",
        color: "var(--color-text)",
        fontFamily: "var(--font-sans)",
        fontSize: "12.5px",
        lineHeight: 1.5,
        boxShadow: "var(--shadow-tip)",
        pointerEvents: "none",
        opacity: 0,
        transform: "translateY(4px)",
        transition: "opacity .12s ease, transform .12s ease",
      }}
    />
  );
}
