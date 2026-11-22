import type { MemoryRecord, Modality } from '../types'

export interface MemoryFilters {
  q?: string
  sync_state?: string
  modality?: string
  zone?: string
  limit?: number
  offset?: number
}

export function filterMemoryRecords<T extends MemoryRecord>(
  records: readonly T[],
  filters: MemoryFilters = {},
  defaultModality?: Modality,
): T[] {
  const limit = filters.limit
  const offset = filters.offset ?? 0
  if (limit != null && (!Number.isInteger(limit) || limit < 1 || limit > 1000)) {
    throw new RangeError('limit must be an integer between 1 and 1000')
  }
  if (!Number.isInteger(offset) || offset < 0) {
    throw new RangeError('offset must be a nonnegative integer')
  }

  const query = filters.q?.toLowerCase()
  const modality = filters.modality ?? defaultModality
  const filtered = records.filter((record) =>
    (!query || record.content_preview.toLowerCase().includes(query)) &&
    (!filters.sync_state || record.state === filters.sync_state) &&
    (!modality || record.modality === modality) &&
    (!filters.zone || record.zone?.toLowerCase().includes(filters.zone.toLowerCase()))
  )

  return filtered.slice(offset, limit == null ? undefined : offset + limit)
}
