-- Preserve durable IOO node links on saved collection cards for lifecycle analytics.
ALTER TABLE collection_items
  ADD COLUMN IF NOT EXISTS node_id UUID REFERENCES ioo_nodes(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS collection_items_node_idx
  ON collection_items(node_id);
