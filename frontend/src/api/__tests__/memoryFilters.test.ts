import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fetchMemoryRecords as fetchNumericMemoryRecords, fetchMemoryDetail as fetchNumericMemoryDetail } from '../client'
import { fetchMemoryRecords as fetchImageMemoryRecords, fetchMemoryDetail as fetchImageMemoryDetail } from '../../image/api/client'
import { mockMemoryRecords } from '../mock'

vi.mock('../mock', async () => {
  const actual = await vi.importActual<typeof import('../mock')>('../mock')
  return { ...actual, USE_MOCKS: true }
})

vi.mock('../../image/api/mock', async () => {
  const actual = await vi.importActual<typeof import('../../image/api/mock')>('../../image/api/mock')
  return { ...actual, USE_MOCKS: true }
})

describe('mock memory API behavior', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('applies filters and pagination to numeric memory records', async () => {
    const expected = mockMemoryRecords('dev-01')
      .filter((record) => record.modality === 'text')
      .slice(1, 3)

    const records = await fetchNumericMemoryRecords('dev-01', {
      modality: 'text',
      limit: 2,
      offset: 1,
    })

    expect(records.map((record) => record.id)).toEqual(expected.map((record) => record.id))
  })

  it('defaults image memory records to vision and paginates them', async () => {
    const expected = mockMemoryRecords('cam-01')
      .filter((record) => record.modality === 'vision')
      .slice(1, 2)

    const records = await fetchImageMemoryRecords('cam-01', { limit: 1, offset: 1 })

    expect(records.map((record) => record.id)).toEqual(expected.map((record) => record.id))
  })

  it('rejects invalid mock pagination values', async () => {
    await expect(fetchNumericMemoryRecords('dev-01', { limit: 0 })).rejects.toThrow(RangeError)
    await expect(fetchNumericMemoryRecords('dev-01', { offset: -1 })).rejects.toThrow(RangeError)
  })

  it('does not substitute another record when a mock detail is missing', async () => {
    await expect(fetchNumericMemoryDetail('dev-01', 'missing')).rejects.toThrow('Memory record not found')
    await expect(fetchImageMemoryDetail('cam-01', 'missing')).rejects.toThrow('Memory record not found')
  })
})
