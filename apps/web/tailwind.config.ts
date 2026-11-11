import type { Config } from "tailwindcss";

const config: Config = {
  content: [
    "./src/pages/**/*.{js,ts,jsx,tsx,mdx}",
    "./src/components/**/*.{js,ts,jsx,tsx,mdx}",
    "./src/app/**/*.{js,ts,jsx,tsx,mdx}",
  ],
  theme: {
    extend: {
      colors: {
        canvas: "#FAFAF8",
        surface: "#FFFFFF",
        subtle: "#F3F3EE",
        border: "#E5E5E0",
        ink: {
          DEFAULT: "#1F2933",
          muted: "#5F6B76",
        },
        accent: {
          DEFAULT: "#0F5C6E",
          hover: "#0B4654",
          soft: "#EBF4F6",
        },
        status: {
          success: "#15803D",
          successBg: "#ECFDF5",
          warning: "#B45309",
          warningBg: "#FFFBEB",
          danger: "#B91C1C",
          dangerBg: "#FEF2F2",
        },
      },
      fontFamily: {
        sans: ["var(--font-inter)", "system-ui", "sans-serif"],
        serif: ["var(--font-source-serif)", "Georgia", "serif"],
      },
      borderRadius: {
        DEFAULT: "6px",
        md: "6px",
        lg: "8px",
      },
    },
  },
  plugins: [],
};

export default config;
