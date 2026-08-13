/** @type {import('tailwindcss').Config} */

// Every colour here is a `var(--…)` reference rather than a literal, so the CSS
// custom properties in src/index.css remain the single source of truth for the
// palette — the canvas renderer (which cannot use Tailwind classes) reads those
// same tokens through src/lib/theme.ts.
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  darkMode: 'class',
  theme: {
    extend: {
      // Channel triplets (`34 211 238`) rather than hex, so Tailwind's
      // `<alpha-value>` slot still works: `bg-cyan/10`, `border-alert/40`, …
      colors: {
        base: 'rgb(var(--bg-rgb) / <alpha-value>)',
        deep: 'rgb(var(--bg-deep-rgb) / <alpha-value>)',
        ink: {
          DEFAULT: 'rgb(var(--text-rgb) / <alpha-value>)',
          muted: 'rgb(var(--text-muted-rgb) / <alpha-value>)',
          faint: 'rgb(var(--text-faint-rgb) / <alpha-value>)',
        },
        cyan: 'rgb(var(--cyan-rgb) / <alpha-value>)',
        toxic: 'rgb(var(--green-rgb) / <alpha-value>)',
        alert: 'rgb(var(--red-rgb) / <alpha-value>)',
        amber: 'rgb(var(--amber-rgb) / <alpha-value>)',
        violet: 'rgb(var(--violet-rgb) / <alpha-value>)',
        steel: 'rgb(var(--slate-rgb) / <alpha-value>)',
      },
      fontFamily: {
        sans: ['Inter', 'ui-sans-serif', 'system-ui', '-apple-system', 'Segoe UI', 'Roboto', 'sans-serif'],
        mono: ['ui-monospace', 'SFMono-Regular', 'JetBrains Mono', 'Menlo', 'Consolas', 'monospace'],
      },
      fontSize: {
        '2xs': ['0.6875rem', { lineHeight: '1rem', letterSpacing: '0.04em' }],
      },
      borderRadius: {
        glass: '14px',
      },
      backdropBlur: {
        glass: '18px',
      },
      boxShadow: {
        glass: 'var(--shadow-glass)',
        'glow-cyan': 'var(--glow-cyan)',
        'glow-green': 'var(--glow-green)',
        'glow-red': 'var(--glow-red)',
        'glow-amber': 'var(--glow-amber)',
        'glow-violet': 'var(--glow-violet)',
      },
      keyframes: {
        'pulse-alert': {
          '0%, 100%': { transform: 'scale(1)', opacity: '0.65' },
          '50%': { transform: 'scale(1.35)', opacity: '0' },
        },
        'scan-sweep': {
          from: { transform: 'rotate(0deg)' },
          to: { transform: 'rotate(360deg)' },
        },
        'flow-dash': {
          from: { transform: 'translate3d(0, 0, 0)' },
          to: { transform: 'translate3d(-24px, 0, 0)' },
        },
        'grid-drift': {
          from: { transform: 'translate3d(0, 0, 0)' },
          to: { transform: 'translate3d(-40px, -40px, 0)' },
        },
        'rise-in': {
          from: { opacity: '0', transform: 'translate3d(0, 10px, 0)' },
          to: { opacity: '1', transform: 'translate3d(0, 0, 0)' },
        },
        shimmer: {
          from: { transform: 'translate3d(-100%, 0, 0)' },
          to: { transform: 'translate3d(100%, 0, 0)' },
        },
        breathe: {
          '0%, 100%': { opacity: '0.55' },
          '50%': { opacity: '1' },
        },
      },
      animation: {
        'pulse-alert': 'pulse-alert 1.9s cubic-bezier(0.4, 0, 0.6, 1) infinite',
        'scan-sweep': 'scan-sweep 7s linear infinite',
        'flow-dash': 'flow-dash 0.7s linear infinite',
        'grid-drift': 'grid-drift 28s linear infinite',
        'rise-in': 'rise-in 0.42s cubic-bezier(0.16, 1, 0.3, 1) both',
        shimmer: 'shimmer 2.4s cubic-bezier(0.4, 0, 0.2, 1) infinite',
        breathe: 'breathe 2.6s ease-in-out infinite',
      },
      transitionTimingFunction: {
        swift: 'cubic-bezier(0.16, 1, 0.3, 1)',
      },
    },
  },
  plugins: [],
};
