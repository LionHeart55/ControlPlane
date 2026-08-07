/**
 * Collection inventory.
 *
 * Rows tagged `source: "snapshot"` came from the last stored snapshot rather
 * than from Milvus just now, which means the collection may already be gone.
 * That is flagged per row instead of being merged silently into the live list.
 */

import { formatNumber } from '../lib/format'
import { Panel, StatusPill } from './Panel'
import type { Section, CollectionsLive } from '../api/types'

interface Props {
  section: Section<CollectionsLive> | undefined
  isLoading: boolean
  error: unknown
}

export function CollectionsTable({ section, isLoading, error }: Props) {
  const live = section?.data
  const collections = live?.collections ?? []

  return (
    <Panel
      title="Collections"
      section={section}
      isLoading={isLoading}
      error={error}
      isEmpty={collections.length === 0}
      emptyText="no collections — run make demo"
      subtitle={
        live ? (
          <span className="hint">
            {live.count} total
            {live.snapshot_only > 0 && (
              <span className="hint--warn"> · {live.snapshot_only} from snapshot</span>
            )}
          </span>
        ) : undefined
      }
    >
      <div className="tablewrap">
        <table className="table">
          <thead>
            <tr>
              <th>collection</th>
              <th className="num">rows</th>
              <th className="num">dim</th>
              <th>index</th>
              <th>metric</th>
              <th>load state</th>
            </tr>
          </thead>
          <tbody>
            {collections.map((collection) => (
              <tr key={collection.collection_name} className="row">
                <td className="mono">
                  {collection.collection_name}
                  {collection.source === 'snapshot' && (
                    <span className="badge badge--snapshot" title="not reported by Milvus now">
                      snapshot
                    </span>
                  )}
                  {collection.error_code && (
                    <span className="badge badge--bad" title={collection.error_message ?? ''}>
                      {collection.error_code}
                    </span>
                  )}
                </td>
                <td className="num">{formatNumber(collection.row_count)}</td>
                <td className="num">{formatNumber(collection.dimension)}</td>
                <td>{collection.index_type ?? <span className="hint">none</span>}</td>
                <td>{collection.metric_type ?? '—'}</td>
                <td>
                  <StatusPill
                    status={collection.load_state ?? (collection.is_loaded ? 'loaded' : 'unknown')}
                  />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Panel>
  )
}
