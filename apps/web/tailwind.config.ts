import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}"],
  darkMode: "class",
  theme: {
    extend: {
      // Design tokens. The frontend-dashboard worker may extend these
      // sparingly. Resist the urge to introduce more accents.
      colors: {
        // Near-black canvas; not pure #000 so the brain shader has headroom.
        canvas: "#0a0b0d",
        surface: "#101216",
        line: "#1d2026",
        // Foreground neutrals.
        ink: {
          50: "#f4f5f7",
          100: "#dadce2",
          200: "#a9adb8",
          300: "#7c818d",
          400: "#5b606b",
          500: "#3f444e",
          600: "#2a2e36",
        },
        // Single accent. Warm amber — used sparingly for activation peaks
        // and one-call-to-action moments. Do not use for general UI chrome.
        accent: {
          DEFAULT: "#f5a524",
          dim: "#a96e10",
          glow: "rgba(245,165,36,0.18)",
        },
      },
      fontFamily: {
        sans: ['"Inter"', "ui-sans-serif", "system-ui", "sans-serif"],
        serif: ['"Fraunces"', "ui-serif", "Georgia", "serif"],
        mono: ['"JetBrains Mono"', "ui-monospace", "SFMono-Regular", "monospace"],
      },
      fontFeatureSettings: {
        nums: '"tnum", "lnum"',
      },
      letterSpacing: {
        tightish: "-0.012em",
      },
      borderRadius: {
        // Intentionally tame. The whole project resists the rounded-2xl look.
        none: "0",
        sm: "2px",
        DEFAULT: "3px",
        md: "4px",
        lg: "6px",
      },
    },
  },
  plugins: [],
};

export default config;
