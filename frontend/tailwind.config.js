/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,ts,jsx,tsx}'],
  theme: {
    extend: {
      colors: {
        // Aegis Edge light field-console palette
        base: {
          DEFAULT: '#E1EBEB', // page background
          raised: '#FFFFFF',  // panels
          sunken: '#E7F0F7',  // inputs and inset areas
        },
        ink: {
          DEFAULT: '#102A43', // deep navy text
          dim: '#45627C',     // secondary text
          faint: '#7890A5',   // tertiary / disabled
        },
        line: '#C9D7E2',      // cool blue-gray border
        alert: '#D64545',     // urgent / critical
        good: '#147D91',      // verified / synced
        pending: '#C47A12',   // queued / pending
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
