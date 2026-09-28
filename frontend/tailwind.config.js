/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,ts,jsx,tsx}'],
  theme: {
    extend: {
      colors: {
        // Tactical ops console palette — frontend.md §2
        base: {
          DEFAULT: '#0B0D0F', // near-black background
          raised: '#101318',  // one step up, for panels
          sunken: '#07080A',  // one step down, for wells/inputs
        },
        ink: {
          DEFAULT: '#E8E9EA', // off-white text
          dim: '#8B9096',     // secondary text
          faint: '#4B4F54',   // tertiary / disabled
        },
        line: '#22262B',      // hairline border
        alert: '#FF4433',     // hot accent — urgent/critical/rejected
        good: '#3DDC84',      // cool accent — verified/synced/confirmed
        pending: '#F5A623',   // amber — queued/pending/degraded
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
