/**
 * PhotoFinder API クライアント（型付き）
 * docs/api-spec.md に対応。fetch ベース・依存ゼロ。
 */

const BASE = "http://127.0.0.1:8686/api";

// ------------------------------------------------------------------ types --

export interface SearchFilters {
  dateFrom?: string;          // ISO8601
  dateTo?: string;
  place?: string;
  camera?: string[];
  ext?: string[];
  tags?: string[];
  bbox?: [number, number, number, number]; // minLon,minLat,maxLon,maxLat
}

export interface PhotoSummary {
  id: number;
  thumb_url: string;
  taken_at: string | null;
  width: number;
  height: number;
  ext: string;
  score?: number;
  top_tags: string[];
}

export interface SuggestedFilter {
  type: "date" | "place";
  label: string;
  date_from?: string;
  date_to?: string;
  place?: string;
}

export interface SearchResponse {
  items: PhotoSummary[];
  total_estimate: number;
  next_cursor: string | null;
  suggested_filters: SuggestedFilter[];
}

export interface ReverseSearchResponse {
  exact: (PhotoSummary & { hamming: number; path?: string })[];
  similar: PhotoSummary[];
}

export interface PhotoTag {
  id: number;
  name: string;
  kind: "auto" | "manual" | "species" | "place" | "ocr";
  source: string;
  conf: number | null;
  verified: -1 | 0 | 1;
}

export interface PhotoDetail {
  id: number;
  path: string;
  taken_at: string | null;
  size: number;
  width: number;
  height: number;
  exif: {
    camera_make?: string;
    camera_model?: string;
    lens_model?: string;
    focal_length_mm?: number;
    f_number?: number;
    shutter_speed?: string;
    iso?: number;
    gps?: { lat: number; lon: number; direction?: number };
  };
  geo?: { prefecture?: string; city?: string; poi_name?: string; poi_conf?: number };
  detections: {
    label: string;
    conf: number;
    bbox: [number, number, number, number];
    bird?: { species_ja: string; species_sci: string; conf: number; confirmed: boolean;
             topk: [string, number][] };
  }[];
  ocr: { text: string; conf: number }[];
  tags: PhotoTag[];
}

export interface IndexStatus {
  running: boolean;
  phase: "scan" | "ml" | "flush" | "idle";
  queue: { pending: number; meta_done: number; ml_done: number };
  done_total: number;
  rate_per_min: number;
  current_root?: string;
}

export interface ExportRequest {
  crop?: { x: number; y: number; w: number; h: number };
  watermark?: { text?: string; logo_path?: string; position: string; opacity: number };
  strip_gps: boolean;
  format: "jpeg" | "png" | "webp";
  quality?: number;
  max_edge?: number;
  out_dir?: string;
}

// ----------------------------------------------------------------- client --

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, init);
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    throw new Error(body?.error?.message ?? `HTTP ${res.status}`);
  }
  return res.json() as Promise<T>;
}

export const api = {
  search(q: string, filters: SearchFilters = {}, cursor?: string): Promise<SearchResponse> {
    const p = new URLSearchParams();
    if (q) p.set("q", q);
    if (filters.dateFrom) p.set("date_from", filters.dateFrom);
    if (filters.dateTo) p.set("date_to", filters.dateTo);
    if (filters.place) p.set("place", filters.place);
    if (filters.camera?.length) p.set("camera", filters.camera.join(","));
    if (filters.ext?.length) p.set("ext", filters.ext.join(","));
    if (filters.tags?.length) p.set("tags", filters.tags.join(","));
    if (filters.bbox) p.set("bbox", filters.bbox.join(","));
    if (cursor) p.set("cursor", cursor);
    return req(`/search?${p}`);
  },

  searchByImage(file: File, limit = 30): Promise<ReverseSearchResponse> {
    const form = new FormData();
    form.append("image", file);
    return req(`/search/by-image?limit=${limit}`, { method: "POST", body: form });
  },

  photo: (id: number) => req<PhotoDetail>(`/photos/${id}`),

  addTag: (photoId: number, name: string) =>
    req<PhotoTag>(`/photos/${photoId}/tags`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    }),

  removeTag: (photoId: number, tagId: number) =>
    req<void>(`/photos/${photoId}/tags/${tagId}`, { method: "DELETE" }),

  /** 自動タグの確定(1)/否認(-1)。否認タグは検索から除外される */
  verifyTag: (photoId: number, tagId: number, verified: 1 | -1) =>
    req<PhotoTag>(`/photos/${photoId}/tags/${tagId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ verified }),
    }),

  suggestTags: (q: string) => req<{ id: number; name: string }[]>(`/tags?q=${encodeURIComponent(q)}`),

  exportPhoto: (id: number, opts: ExportRequest) =>
    req<{ out_path: string }>(`/photos/${id}/export`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(opts),
    }),

  openInExplorer: (id: number) =>
    req<void>(`/photos/${id}/open-in-explorer`, { method: "POST" }),

  indexStatus: () => req<IndexStatus>(`/index/status`),

  startScan: (rootId?: number) =>
    req<void>(`/index/scan`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(rootId ? { root_id: rootId } : {}),
    }),

  /** SSE でインデックス進捗を購読。戻り値は unsubscribe 関数 */
  subscribeIndexEvents(handlers: {
    onProgress?: (s: Pick<IndexStatus, "queue" | "done_total" | "rate_per_min">) => void;
    onPhotoIndexed?: (p: { id: number; thumb_url: string }) => void;
    onScanComplete?: (s: { root_id: number; added: number; updated: number }) => void;
  }): () => void {
    const es = new EventSource(`${BASE}/index/events`);
    if (handlers.onProgress)
      es.addEventListener("progress", (e) => handlers.onProgress!(JSON.parse(e.data)));
    if (handlers.onPhotoIndexed)
      es.addEventListener("photo_indexed", (e) => handlers.onPhotoIndexed!(JSON.parse(e.data)));
    if (handlers.onScanComplete)
      es.addEventListener("scan_complete", (e) => handlers.onScanComplete!(JSON.parse(e.data)));
    return () => es.close();
  },
};
