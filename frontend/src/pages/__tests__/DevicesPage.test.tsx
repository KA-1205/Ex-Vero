import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { MemoryRouter } from 'react-router-dom'
import { DevicesPage } from '../DevicesPage'
import * as client from '../../api/client'
import type { DeviceSummary } from '../../types'

vi.mock('../../api/client', () => ({
  fetchDevices: vi.fn(),
}))

const mockDevicesList: DeviceSummary[] = [
  {
    id: 'dev-01',
    name: 'Paramedic Tablet 01',
    kind: 'tablet',
    connectivity: 'ONLINE',
    memory_used: 120,
    memory_cap: 500,
    last_sync_at: '2026-10-23T12:00:00Z',
    activity_sparkline: [1, 2, 3],
  },
  {
    id: 'dev-02',
    name: 'Kiosk B',
    kind: 'kiosk',
    connectivity: 'OFFLINE',
    memory_used: 350,
    memory_cap: 500,
    last_sync_at: null,
    activity_sparkline: [0, 0, 0],
  },
]

describe('DevicesPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders loading state initially', () => {
    vi.mocked(client.fetchDevices).mockReturnValue(new Promise(() => {}))
    render(
      <MemoryRouter>
        <DevicesPage />
      </MemoryRouter>
    )
    expect(screen.getByText(/loading devices…/i)).toBeInTheDocument()
  })

  it('renders device tiles when data loads successfully', async () => {
    vi.mocked(client.fetchDevices).mockResolvedValueOnce(mockDevicesList)
    render(
      <MemoryRouter>
        <DevicesPage />
      </MemoryRouter>
    )

    await waitFor(() => {
      expect(screen.getByText('Paramedic Tablet 01')).toBeInTheDocument()
      expect(screen.getByText('Kiosk B')).toBeInTheDocument()
    })
    expect(screen.getByText(/DEVICES — 2 TOTAL/i)).toBeInTheDocument()
  })

  it('handles empty device list properly', async () => {
    vi.mocked(client.fetchDevices).mockResolvedValueOnce([])
    render(
      <MemoryRouter>
        <DevicesPage />
      </MemoryRouter>
    )

    await waitFor(() => {
      expect(screen.getByText(/No devices found\./i)).toBeInTheDocument()
    })
    expect(screen.queryByText(/loading devices…/i)).not.toBeInTheDocument()
  })

  it('displays error and retry controls when device fetch fails', async () => {
    vi.mocked(client.fetchDevices).mockRejectedValueOnce(new Error('Network error'))
    render(
      <MemoryRouter>
        <DevicesPage />
      </MemoryRouter>
    )

    await waitFor(() => {
      expect(screen.getByRole('alert')).toBeInTheDocument()
      expect(screen.getByText(/Device list unavailable\./i)).toBeInTheDocument()
    })

    const retryButton = screen.getByRole('button', { name: /Retry/i })
    expect(retryButton).toBeInTheDocument()

    // Test retry clicking
    vi.mocked(client.fetchDevices).mockResolvedValueOnce(mockDevicesList)
    fireEvent.click(retryButton)

    await waitFor(() => {
      expect(screen.getByText('Paramedic Tablet 01')).toBeInTheDocument()
    })
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })
})
