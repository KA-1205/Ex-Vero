import { render, screen, waitFor } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { MemoryRouter } from 'react-router-dom'
import { OverviewDashboard } from '../OverviewDashboard'
import * as client from '../../api/client'

vi.mock('../../api/client', () => ({
  fetchDevices: vi.fn(),
  fetchCloudState: vi.fn(),
  fetchActivity: vi.fn(),
  fetchSyncStatus: vi.fn(),
  fetchTelemetry: vi.fn(),
  fetchWeather: vi.fn(),
}))

describe('OverviewDashboard', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(client.fetchDevices).mockResolvedValue([
      {
        id: 'dev-01',
        name: 'Paramedic Tablet 01',
        kind: 'tablet',
        connectivity: 'ONLINE',
        memory_used: 120,
        memory_cap: 500,
        last_sync_at: '2026-10-23T12:00:00Z',
        activity_sparkline: [],
      },
    ])
    vi.mocked(client.fetchCloudState).mockResolvedValue([])
    vi.mocked(client.fetchActivity).mockResolvedValue([])
    vi.mocked(client.fetchSyncStatus).mockResolvedValue(null as any)
    vi.mocked(client.fetchTelemetry).mockResolvedValue([])
    vi.mocked(client.fetchWeather).mockResolvedValue(null)
  })

  it('renders verified sync rate placeholder and label instead of unverified 96.4%', async () => {
    render(
      <MemoryRouter>
        <OverviewDashboard />
      </MemoryRouter>
    )

    await waitFor(() => {
      expect(screen.getByText('SYNC SUCCESS RATE')).toBeInTheDocument()
      expect(screen.getByText('No verified fleet-wide rate available')).toBeInTheDocument()
    })

    // Assert that the misleading 96.4% and MEASURED SYNC RATE are not present
    expect(screen.queryByText('96.4%')).not.toBeInTheDocument()
    expect(screen.queryByText('MEASURED SYNC RATE')).not.toBeInTheDocument()
  })

  it('handles empty fleet data without crashing', async () => {
    vi.mocked(client.fetchDevices).mockResolvedValue([])

    render(
      <MemoryRouter>
        <OverviewDashboard />
      </MemoryRouter>
    )

    await waitFor(() => {
      expect(screen.getByText('TOTAL DEVICES')).toBeInTheDocument()
      expect(screen.getByText('0 offline · 0 online')).toBeInTheDocument()
    })
  })
})
