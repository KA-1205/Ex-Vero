import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { ConflictTheater } from '../ConflictTheater'
import * as client from '../../api/client'
import * as context from '../../context/NetworkModeContext'

vi.mock('../../api/client', () => ({
  fetchDevices: vi.fn(),
  subscribeConsensusEvents: vi.fn(() => () => {}),
  injectConflict: vi.fn(),
  mapConsensusEvent: vi.fn(),
  setRogueMode: vi.fn(),
}))

vi.mock('../../context/NetworkModeContext', () => ({
  useNetworkMode: vi.fn(),
}))

describe('ConflictTheater', () => {
  const mockSetMode = vi.fn()

  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(client.fetchDevices).mockResolvedValue([
      {
        id: 'dev-01',
        name: 'Paramedic Tablet 01',
        kind: 'tablet',
        connectivity: 'OFFLINE',
        memory_used: 100,
        memory_cap: 500,
        last_sync_at: null,
        activity_sparkline: [],
      },
      {
        id: 'dev-03',
        name: 'Field Pi Node 03',
        kind: 'node',
        connectivity: 'OFFLINE',
        memory_used: 100,
        memory_cap: 500,
        last_sync_at: null,
        activity_sparkline: [],
      },
    ])
    vi.mocked(context.useNetworkMode).mockReturnValue({
      mode: 'offline',
      setMode: mockSetMode,
    })
  })

  it('renders conflict theater initial state with offline nodes', async () => {
    render(<ConflictTheater />)

    expect(screen.getByText('CONFLICT THEATER')).toBeInTheDocument()
    await waitFor(() => {
      expect(screen.getAllByText('OFFLINE').length).toBeGreaterThanOrEqual(2)
    })
    expect(screen.getByText(/both devices offline/i)).toBeInTheDocument()
  })

  it('handles reconnect network failure without falsely reporting success', async () => {
    mockSetMode.mockImplementation(() => {
      throw new Error('Simulation network error: connection timed out')
    })

    render(<ConflictTheater />)

    const reconnectBtn = screen.getByRole('button', { name: /RECONNECT/i })
    fireEvent.click(reconnectBtn)

    await waitFor(() => {
      expect(screen.getByText(/Simulation network error: connection timed out/i)).toBeInTheDocument()
    })

    // Assert that the devices are still OFFLINE, NOT falsely flipped to ONLINE
    expect(screen.getAllByText('OFFLINE').length).toBeGreaterThanOrEqual(2)
    expect(screen.queryByText('ONLINE')).not.toBeInTheDocument()

    // Assert retry reconnect button is provided
    expect(screen.getByRole('button', { name: /Retry Reconnect/i })).toBeInTheDocument()
  })

  it('sets reconnected to true when network reconnect succeeds', async () => {
    mockSetMode.mockReturnValue(undefined)

    render(<ConflictTheater />)

    const reconnectBtn = screen.getByRole('button', { name: /RECONNECT/i })
    fireEvent.click(reconnectBtn)

    await waitFor(() => {
      expect(screen.getAllByText('ONLINE').length).toBeGreaterThanOrEqual(2)
    })
  })
})
