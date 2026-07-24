/**
 * PhotoFinder 検索画面（React サンプル）
 *
 * 構成: 検索バー(自然言語+画像ドロップ) / フィルタチップ / 仮想スクロールグリッド / 詳細パネル
 * 依存: @tanstack/react-query, @tanstack/react-virtual
 * UI 状態は URL クエリに正規化する想定（ここでは useState で簡略化）。
 */
import { useCallback, useMemo, useRef, useState } from "react";
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useVirtualizer } from "@tanstack/react-virtual";
import {
  api, PhotoSummary, SearchFilters, SuggestedFilter,
} from "./apiClient";

// ============================================================ SearchView ===

export default function SearchView() {
  const [query, setQuery] = useState("");
  const [submitted, setSubmitted] = useState("");
  const [filters, setFilters] = useState<SearchFilters>({});
  const [selectedId, setSelectedId] = useState<number | null>(null);

  const search = useInfiniteQuery({
    queryKey: ["search", submitted, filters],
    queryFn: ({ pageParam }) => api.search(submitted, filters, pageParam),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
  });

  const items = useMemo(
    () => search.data?.pages.flatMap((p) => p.items) ?? [],
    [search.data],
  );
  const suggestions = search.data?.pages[0]?.suggested_filters ?? [];

  // 画像ドロップ → 逆検索モードへ遷移（実装は ReverseSearchView に委譲）
  const onDropImage = useCallback(async (file: File) => {
    const result = await api.searchByImage(file);
    // ここでは概略のみ: exact があれば「原本が見つかりました」バナー表示
    console.log("reverse search:", result.exact.length, "exact matches");
  }, []);

  return (
    <div className="app-layout">
      <SearchBar
        value={query}
        onChange={setQuery}
        onSubmit={() => setSubmitted(query)}
        onDropImage={onDropImage}
      />
      <FilterChips
        filters={filters}
        suggestions={suggestions}
        onApplySuggestion={(s) => setFilters((f) => applySuggestion(f, s))}
        onRemove={(key) => setFilters((f) => ({ ...f, [key]: undefined }))}
      />
      <div className="main-row">
        <PhotoGrid
          items={items}
          selectedId={selectedId}
          onSelect={setSelectedId}
          onEndReached={() => search.hasNextPage && search.fetchNextPage()}
        />
        {selectedId !== null && (
          <DetailPanel photoId={selectedId} onClose={() => setSelectedId(null)} />
        )}
      </div>
    </div>
  );
}

// ============================================================ SearchBar ====

function SearchBar(props: {
  value: string;
  onChange: (v: string) => void;
  onSubmit: () => void;
  onDropImage: (f: File) => void;
}) {
  return (
    <div
      className="search-bar"
      onDragOver={(e) => e.preventDefault()}
      onDrop={(e) => {
        e.preventDefault();
        const file = e.dataTransfer.files[0];
        if (file?.type.startsWith("image/")) props.onDropImage(file);
      }}
    >
      <input
        type="search"
        placeholder="例: 去年の秋に京都で撮ったカワセミ（画像をドロップで類似検索）"
        value={props.value}
        onChange={(e) => props.onChange(e.target.value)}
        onKeyDown={(e) => e.key === "Enter" && props.onSubmit()}
      />
    </div>
  );
}

// ========================================================== FilterChips ====

function applySuggestion(f: SearchFilters, s: SuggestedFilter): SearchFilters {
  if (s.type === "date") return { ...f, dateFrom: s.date_from, dateTo: s.date_to };
  return { ...f, place: s.place };
}

function FilterChips(props: {
  filters: SearchFilters;
  suggestions: SuggestedFilter[];
  onApplySuggestion: (s: SuggestedFilter) => void;
  onRemove: (key: keyof SearchFilters) => void;
}) {
  const active = Object.entries(props.filters).filter(([, v]) => v != null);
  return (
    <div className="filter-chips">
      {active.map(([key, value]) => (
        <span key={key} className="chip chip-active">
          {String(value)}
          <button onClick={() => props.onRemove(key as keyof SearchFilters)}>×</button>
        </span>
      ))}
      {/* クエリ解析によるフィルタ候補は点線チップで提示 — クリックで適用 */}
      {props.suggestions.map((s) => (
        <button key={s.label} className="chip chip-suggested"
                onClick={() => props.onApplySuggestion(s)}>
          + {s.label}
        </button>
      ))}
    </div>
  );
}

// ============================================================ PhotoGrid ====

const ROW_HEIGHT = 200;
const GAP = 8;

function PhotoGrid(props: {
  items: PhotoSummary[];
  selectedId: number | null;
  onSelect: (id: number) => void;
  onEndReached: () => void;
}) {
  const parentRef = useRef<HTMLDivElement>(null);
  const columns = 5; // 実装時は ResizeObserver で可変に

  const rows = Math.ceil(props.items.length / columns);
  const virtualizer = useVirtualizer({
    count: rows,
    getScrollElement: () => parentRef.current,
    estimateSize: () => ROW_HEIGHT + GAP,
    overscan: 4,
  });

  return (
    <div
      ref={parentRef}
      className="photo-grid-scroll"
      onScroll={(e) => {
        const el = e.currentTarget;
        if (el.scrollHeight - el.scrollTop - el.clientHeight < 800) props.onEndReached();
      }}
    >
      <div style={{ height: virtualizer.getTotalSize(), position: "relative" }}>
        {virtualizer.getVirtualItems().map((vRow) => (
          <div
            key={vRow.key}
            className="grid-row"
            style={{
              position: "absolute", top: 0, left: 0, width: "100%",
              transform: `translateY(${vRow.start}px)`,
              display: "grid",
              gridTemplateColumns: `repeat(${columns}, 1fr)`,
              gap: GAP, height: ROW_HEIGHT,
            }}
          >
            {props.items
              .slice(vRow.index * columns, (vRow.index + 1) * columns)
              .map((photo) => (
                <Thumb
                  key={photo.id}
                  photo={photo}
                  selected={photo.id === props.selectedId}
                  onClick={() => props.onSelect(photo.id)}
                />
              ))}
          </div>
        ))}
      </div>
    </div>
  );
}

function Thumb({ photo, selected, onClick }: {
  photo: PhotoSummary; selected: boolean; onClick: () => void;
}) {
  return (
    <figure className={`thumb ${selected ? "thumb-selected" : ""}`} onClick={onClick}>
      <img src={photo.thumb_url} loading="lazy" decoding="async"
           alt={photo.top_tags.join(", ")} />
      <figcaption className="thumb-tags">
        {photo.top_tags.slice(0, 2).map((t) => <span key={t}>{t}</span>)}
      </figcaption>
    </figure>
  );
}

// =========================================================== DetailPanel ===

function DetailPanel({ photoId, onClose }: { photoId: number; onClose: () => void }) {
  const qc = useQueryClient();
  const { data: photo } = useQuery({
    queryKey: ["photo", photoId],
    queryFn: () => api.photo(photoId),
  });
  const invalidate = () => qc.invalidateQueries({ queryKey: ["photo", photoId] });

  const addTag = useMutation({
    mutationFn: (name: string) => api.addTag(photoId, name),
    onSuccess: invalidate,
  });
  const verifyTag = useMutation({
    mutationFn: (v: { tagId: number; verified: 1 | -1 }) =>
      api.verifyTag(photoId, v.tagId, v.verified),
    onSuccess: invalidate,
  });

  if (!photo) return <aside className="detail-panel">読み込み中…</aside>;

  return (
    <aside className="detail-panel">
      <header>
        <h2>{photo.path.split("\\").pop()}</h2>
        <button onClick={() => api.openInExplorer(photoId)}>場所を開く</button>
        <button onClick={onClose}>×</button>
      </header>

      <img className="detail-preview" src={`/api/photos/${photoId}/preview`} alt="" />

      <section>
        <h3>撮影情報</h3>
        <dl>
          <dt>日時</dt><dd>{photo.taken_at ?? "不明"}</dd>
          <dt>カメラ</dt><dd>{photo.exif.camera_model ?? "—"}</dd>
          <dt>レンズ</dt><dd>{photo.exif.lens_model ?? "—"}</dd>
          <dt>設定</dt>
          <dd>
            {photo.exif.focal_length_mm}mm f/{photo.exif.f_number}{" "}
            {photo.exif.shutter_speed}s ISO{photo.exif.iso}
          </dd>
          {photo.geo && (
            <><dt>場所</dt>
            <dd>{photo.geo.prefecture} {photo.geo.city} {photo.geo.poi_name}</dd></>
          )}
        </dl>
      </section>

      <section>
        <h3>タグ</h3>
        <div className="tag-list">
          {photo.tags.map((tag) => (
            <span key={tag.id}
                  className={`tag tag-${tag.kind} ${tag.verified === 1 ? "tag-verified" : ""}`}>
              {tag.name}
              {tag.conf != null && <small> {(tag.conf * 100) | 0}%</small>}
              {/* 自動タグは ✓(確定) / ✗(否認→検索除外) をワンクリックで */}
              {tag.source !== "user" && tag.verified === 0 && (
                <>
                  <button title="確定"
                          onClick={() => verifyTag.mutate({ tagId: tag.id, verified: 1 })}>✓</button>
                  <button title="否認"
                          onClick={() => verifyTag.mutate({ tagId: tag.id, verified: -1 })}>✗</button>
                </>
              )}
            </span>
          ))}
        </div>
        <TagInput onAdd={(name) => addTag.mutate(name)} />
      </section>
    </aside>
  );
}

function TagInput({ onAdd }: { onAdd: (name: string) => void }) {
  const [value, setValue] = useState("");
  const { data: suggestions } = useQuery({
    queryKey: ["tag-suggest", value],
    queryFn: () => api.suggestTags(value),
    enabled: value.length >= 1,
  });
  return (
    <div className="tag-input">
      <input
        value={value}
        placeholder="タグを追加…"
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && value.trim()) {
            onAdd(value.trim());
            setValue("");
          }
        }}
        list="tag-candidates"
      />
      <datalist id="tag-candidates">
        {suggestions?.map((s) => <option key={s.id} value={s.name} />)}
      </datalist>
    </div>
  );
}
