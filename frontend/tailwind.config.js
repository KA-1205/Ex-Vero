/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,ts,jsx,tsx}'],
  theme: {
    extend: {
      colors: {
        // Silver, petrol teal, and warm champagne.
        base: {
          DEFAULT: '#E5E7E9', // page background
          raised: '#F8F9F5',  // panels
          sunken: '#E8EEEC',  // inputs and inset areas
        },
        ink: {
          DEFAULT: '#233239', // deep navy text
          dim: '#596C70',     // secondary text
          faint: '#6E7D80',   // tertiary / disabled
        },
        line: '#CDD6D7',      // cool blue-gray border
        alert: '#D64545',     // urgent / critical
        good: '#236A70',      // verified / synced
        pending: '#9B701F',   // queued / pending
      },
      fontFamily: {
        mono: ['"JetBrains Mono"', '"IBM Plex Mono"', 'ui-monospace', 'monospace'],
        sans: ['Inter', 'ui-sans-serif', 'system-ui', 'sans-serif'],
      },
      borderRadius: {
        none: '0px',
        sm: '1px',
        DEFAULT: '2px',
      },
    },
  },
  plugins: [],
}
