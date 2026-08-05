// frontend/lib/motion.ts
//
// Shared motion constants for the DevGuard AI app. Both app/page.tsx and
// app/result/page.tsx pull from here instead of hard-coding their own
// easing curves and durations — that's what makes the hand-off between
// the two pages feel like a single continuous product rather than two
// screens that happen to be styled similarly. If a curve or duration ever
// needs to change, it changes once, here, for the whole app.

import type { Transition, Variants } from "framer-motion";

/** Standard product easing — smooth "ease-out-expo"-like deceleration.
 *  Used anywhere motion should feel deliberate and settle firmly, rather
 *  than linger or overshoot: page transitions, CTA micro-interactions,
 *  staggered reveals. */
export const EASE_PRODUCT: number[] = [0.22, 1, 0.36, 1];

/** Full-page enter/exit variants for use with <AnimatePresence>.
 *  Fade + a slight vertical drift (12px in, 24px out on exit) so the exit
 *  reads as "pulling back" rather than a plain fade — mirrors the Page 1
 *  → Page 2 hand-off so it holds up regardless of which page is entering
 *  or leaving. */
export const PAGE_TRANSITION: Variants = {
  initial: { opacity: 0, y: 12 },
  animate: {
    opacity: 1,
    y: 0,
    transition: { duration: 0.35, ease: EASE_PRODUCT },
  },
  exit: {
    opacity: 0,
    y: -24,
    scale: 0.98,
    transition: { duration: 0.35, ease: EASE_PRODUCT },
  },
};

/** Transition for a staggered child at a given index (e.g. metric cards on
 *  the result page). 90ms per step is fast enough that a row of 4-6 cards
 *  finishes settling well under half a second, but still visibly sequences
 *  rather than popping in together. */
export function STAGGER_CHILD(index: number): Transition {
  const STAGGER_STEP_S = 0.09; // 90ms between siblings
  return {
    duration: 0.3,
    ease: EASE_PRODUCT,
    delay: index * STAGGER_STEP_S,
  };
}

/** Short transition for hover/press glow-intensity micro-interactions
 *  (e.g. CTA buttons, badges). Deliberately quicker than page-level motion
 *  since these fire on every hover/click and must feel instantaneous. */
export const GLOW_TRANSITION: Transition = {
  duration: 0.18,
  ease: EASE_PRODUCT,
};
