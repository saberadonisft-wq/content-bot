
import {
CircleStop,
LoaderCircle,
LogIn,
Play,
RefreshCw,
Trash2
} from "lucide-react";
import { useEffect,useState } from "react";
import {
api,
Batch,
CrawlerLoginStatus,
Keyword,
Source,
TikTokOAuthStatus
} from "../../api";

import { phaseLabel,progressPercent,sourceCanRun,sourceOperation } from "../shared/presentation";

export function Sources({
  embedded = false,
  sources,
  loading,
  selected,
  batch,
  running,
  pendingSourceId,
  canceling,
  onRunSource,
  onConnectionsChanged,
  onCancel,
}: {
  embedded?: boolean;
  sources: Source[];
  loading: boolean;
  selected: Keyword | null;
  batch: Batch | null;
  running: boolean;
  pendingSourceId: string | null;
  canceling: boolean;
  onRunSource: (sourceId: string) => void;
  onConnectionsChanged: () => Promise<void>;
  onCancel: () => void;
}) {
  const [tiktokStatus, setTikTokStatus] = useState<TikTokOAuthStatus | null>(null);
  const [tiktokUsername, setTikTokUsername] = useState("");
  const [tiktokBusy, setTikTokBusy] = useState(false);
  const [tiktokError, setTikTokError] = useState("");
  const [crawlerLogins, setCrawlerLogins] = useState<
    Record<string, CrawlerLoginStatus>
  >({});
  const [crawlerLoginBusy, setCrawlerLoginBusy] = useState<string | null>(null);
  const [crawlerConnectionIds, setCrawlerConnectionIds] = useState<Record<string, string>>({});
  const [crawlerLoginErrors, setCrawlerLoginErrors] = useState<
    Record<string, string>
  >({});
  useEffect(() => {
    if (!sources.some((source) => source.id === "tiktok")) return;
    let active = true;
    void api
      .tiktokOAuthStatus()
      .then((status) => {
        if (!active) return;
        setTikTokStatus(status);
        setTikTokError("");
        if (status.authorized_username) {
          setTikTokUsername(status.authorized_username);
        }
      })
      .catch(() => {
        if (active) {
          setTikTokStatus(null);
          setTikTokError("Không đọc được trạng thái OAuth TikTok.");
        }
      });
    return () => {
      active = false;
    };
  }, [sources]);

  const activeCrawlerLoginIds = Object.values(crawlerLogins)
    .filter((session) =>
      ["opening", "waiting_for_user", "verifying"].includes(session.state),
    )
    .map((session) => session.source_id)
    .sort()
    .join(",");
  useEffect(() => {
    if (!activeCrawlerLoginIds) return;
    let active = true;
    const sourceIds = activeCrawlerLoginIds.split(",");
    const poll = async () => {
      const results = await Promise.allSettled(
        sourceIds.map((sourceId) =>
          api.crawlerLoginStatus(sourceId, crawlerConnectionIds[sourceId] || "default"),
        ),
      );
      if (!active) return;
      setCrawlerLogins((current) => {
        const next = { ...current };
        results.forEach((result, index) => {
          if (result.status === "fulfilled") {
            next[sourceIds[index]] = result.value;
          }
        });
        return next;
      });
    };
    void poll();
    const timer = window.setInterval(() => void poll(), 1500);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, [activeCrawlerLoginIds, crawlerConnectionIds]);

  const connectTikTok = () => {
    const username = tiktokUsername.trim().replace(/^@/, "");
    if (!/^[A-Za-z0-9._]{2,24}$/.test(username)) {
      setTikTokError("Username TikTok phải dài 2–24 ký tự và chỉ gồm chữ, số, dấu chấm hoặc gạch dưới.");
      return;
    }
    setTikTokError("");
    window.location.assign(api.tiktokOAuthStartUrl(username));
  };
  const refreshTikTok = async () => {
    setTikTokBusy(true);
    try {
      setTikTokStatus(await api.refreshTikTokOAuth());
      setTikTokError("");
      await onConnectionsChanged();
    } catch (error) {
      setTikTokError(
        error instanceof Error ? error.message : "Không thể làm mới token TikTok.",
      );
    } finally {
      setTikTokBusy(false);
    }
  };
  const disconnectTikTok = async () => {
    if (!window.confirm("Thu hồi quyền TikTok và xóa token đã mã hóa?")) return;
    setTikTokBusy(true);
    try {
      await api.disconnectTikTokOAuth();
      setTikTokStatus(await api.tiktokOAuthStatus());
      setTikTokError("");
      await onConnectionsChanged();
    } catch (error) {
      setTikTokError(
        error instanceof Error ? error.message : "Không thể ngắt kết nối TikTok.",
      );
    } finally {
      setTikTokBusy(false);
    }
  };
  const startCrawlerLogin = async (sourceId: string) => {
    const connectionId = crawlerConnectionIds[sourceId]?.trim() || "default";
    setCrawlerLoginBusy(sourceId);
    try {
      const session = await api.startCrawlerLogin(sourceId, 1200, connectionId);
      setCrawlerLogins((current) => ({ ...current, [sourceId]: session }));
      setCrawlerLoginErrors((current) => ({ ...current, [sourceId]: "" }));
    } catch (error) {
      setCrawlerLoginErrors((current) => ({
        ...current,
        [sourceId]:
          error instanceof Error
            ? error.message
            : "Không thể mở profile trình duyệt v2.",
      }));
    } finally {
      setCrawlerLoginBusy(null);
    }
  };
  const stopCrawlerLogin = async (sourceId: string) => {
    const connectionId = crawlerConnectionIds[sourceId]?.trim() || "default";
    setCrawlerLoginBusy(sourceId);
    try {
      const session = await api.stopCrawlerLogin(sourceId, connectionId);
      setCrawlerLogins((current) => ({ ...current, [sourceId]: session }));
      setCrawlerLoginErrors((current) => ({ ...current, [sourceId]: "" }));
    } catch (error) {
      setCrawlerLoginErrors((current) => ({
        ...current,
        [sourceId]:
          error instanceof Error
            ? error.message
            : "Không thể đóng phiên đăng nhập.",
      }));
    } finally {
      setCrawlerLoginBusy(null);
    }
  };
  const activeBatch =
    batch &&
      selected &&
      batch.keyword_id === selected.id &&
      ["queued", "running"].includes(batch.state)
      ? batch
      : null;
  const sourceRuns = new Map(
    (batch?.source_runs ?? []).map((sourceRun) => [
      sourceRun.source_id,
      sourceRun,
    ]),
  );
  return (
    <>
      {!embedded && <section className="page-head">
        <div>
          <h1>Nguồn dữ liệu</h1>
          <p className="subtle">
            Chạy các nguồn công khai trực tiếp, hoặc mở từng nguồn cần đăng nhập.
          </p>
        </div>
      </section>}
      <section className="source-guidance" aria-label="Scan guidance">
        <div>
          <strong>
            {selected
              ? `Game đang chọn: "${selected.name}"`
              : "Chọn game trước"}
          </strong>
          <p>
            {selected
              ? "Nguồn cần đăng nhập sẽ mở trình duyệt sau 20–30 giây. Giữ batch chạy, hoàn tất QR hoặc xác nhận điện thoại trong đó; Content Bot không bao giờ vượt qua bước này."
              : "Mở Phân tích và chọn một game đang theo dõi. Các nút nguồn sẽ bị tắt cho đến khi chọn game."}
          </p>
        </div>
        {activeBatch && (
          <button
            className="button danger"
            onClick={onCancel}
            disabled={canceling}
          >
            {canceling ? (
              <LoaderCircle className="spin" size={15} />
            ) : (
              <CircleStop size={15} />
            )}
            {canceling ? "Đang hủy" : "Hủy quét"}
          </button>
        )}
      </section>
      {batch && selected && batch.keyword_id === selected.id && (
        <section
          className="source-progress"
          aria-label="Current source run"
          aria-live="polite"
        >
          <div className="source-progress-head">
            <strong>Đợt quét #{batch.id.slice(0, 8)}</strong>
            <span className={`badge ${batch.state}`}>{batch.state}</span>
          </div>
          <ul>
            {batch.source_runs.map((sourceRun) => {
              const source = sources.find(
                (candidate) => candidate.id === sourceRun.source_id,
              );
              return (
                <li key={sourceRun.id}>
                  <span>
                    <strong>{source?.label ?? sourceRun.source_id}</strong>
                    <span className="mono">
                      {phaseLabel(sourceRun)} · đã lấy {sourceRun.fetched_count} ·
                      đã lưu {sourceRun.ingested_count}
                    </span>
                  </span>
                  <div
                    className={`run-progress-track ${sourceRun.progress_mode === "indeterminate"
                        ? "indeterminate"
                        : ""
                      }`}
                    role="progressbar"
                    aria-label={`${source?.label ?? sourceRun.source_id} progress`}
                    aria-valuemin={0}
                    aria-valuemax={100}
                    {...(progressPercent(sourceRun) !== null
                      ? {
                        "aria-valuenow": Math.round(
                          progressPercent(sourceRun) ?? 0,
                        ),
                      }
                      : {})}
                  >
                    <span
                      className="run-progress-fill"
                      style={
                        progressPercent(sourceRun) !== null
                          ? { width: `${progressPercent(sourceRun)}%` }
                          : undefined
                      }
                    />
                  </div>
                  <span className="source-progress-meta">
                    <span>
                      {sourceRun.message || phaseLabel(sourceRun)}
                      {sourceRun.browser_state === "waiting_login" &&
                        " · QR/xác nhận điện thoại đang hiển thị trong Cốc Cốc"}
                    </span>
                    <span className="mono">
                      {progressPercent(sourceRun) !== null
                        ? `${Math.round(progressPercent(sourceRun) ?? 0)}%`
                        : "đang xử lý"}
                    </span>
                  </span>
                  {sourceRun.error_message && (
                    <span className="source-run-error" role="alert">
                      {sourceRun.error_code && (
                        <strong>{sourceRun.error_code}: </strong>
                      )}
                      {sourceRun.error_message}
                    </span>
                  )}
                </li>
              );
            })}
          </ul>
        </section>
      )}
      <div className="source-grid">
        {sources.map((source) => {
          const sourceRun = sourceRuns.get(source.id);
          const primary = sourceOperation(source, source.primary_operation);
          const embedOnly =
            sourceCanRun(source, "render_embed") &&
            !sourceCanRun(source, "search");
          const channelOnly =
            source.primary_operation === "scan_channel" &&
            sourceCanRun(source, "scan_channel") &&
            !sourceCanRun(source, "search");
          const sourceIsActive =
            activeBatch &&
            sourceRun &&
            ["queued", "running"].includes(sourceRun.state);
          const unavailable = !sourceCanRun(source, "search");
          const hasCbceProfile =
            source.requires_login &&
            source.provider_selection.includes(`cbce_${source.id}`);
          const crawlerLogin = crawlerLogins[source.id];
          const crawlerLoginActive =
            crawlerLogin &&
            ["opening", "waiting_for_user", "verifying"].includes(
              crawlerLogin.state,
            );
          const authLabel = primary?.auth_modes.includes("oauth")
            ? "OAUTH"
            : source.requires_login
              ? "CẦN ĐĂNG NHẬP TƯƠNG TÁC"
              : "CÔNG KHAI";
          return (
            <article className="source-card" key={source.id}>
              <div className="toolbar">
                <h3>{source.label}</h3>
                <span className={`badge ${source.state}`}>
                  {source.state.replace("_", " ")}
                </span>
              </div>
              <p id={`source-detail-${source.id}`}>
                {primary?.detail || source.coverage_disclaimer || source.detail}
              </p>
              <div className="source-card-meta">
                <span className="mono">{source.group}</span>
                <span className="mono">
                  {authLabel}
                </span>
                {primary?.budget_limits && (
                  <span className="mono">
                    Tối đa {primary.budget_limits.max_items} bài / lượt
                  </span>
                )}
              </div>
              {source.id === "tiktok" ? (
                <div className="source-oauth-controls" aria-busy={tiktokBusy}>
                  {tiktokError && (
                    <span className="source-oauth-error" role="alert">
                      {tiktokError}
                    </span>
                  )}
                  {tiktokStatus?.connected ? (
                    <>
                      <span className="source-connection-account">
                        Đã kết nối @{tiktokStatus.authorized_username}
                      </span>
                      <div className="source-oauth-actions">
                        <button
                          className="button secondary"
                          type="button"
                          disabled={tiktokBusy}
                          onClick={() => void refreshTikTok()}
                        >
                          {tiktokBusy ? (
                            <LoaderCircle className="spin" size={15} />
                          ) : (
                            <RefreshCw size={15} />
                          )}
                          Làm mới token
                        </button>
                        <button
                          className="button danger"
                          type="button"
                          disabled={tiktokBusy}
                          onClick={() => void disconnectTikTok()}
                        >
                          {tiktokBusy ? (
                            <LoaderCircle className="spin" size={15} />
                          ) : (
                            <Trash2 size={15} />
                          )}
                          Ngắt kết nối
                        </button>
                      </div>
                    </>
                  ) : (
                    <>
                      <label className="source-oauth-username" htmlFor="tiktok-oauth-username">
                        TikTok creator đã được duyệt
                      </label>
                      <input
                        id="tiktok-oauth-username"
                        className="source-oauth-input"
                        value={tiktokUsername}
                        onChange={(event) => setTikTokUsername(event.target.value)}
                        placeholder="game.dev"
                        maxLength={24}
                        autoComplete="off"
                        aria-describedby="tiktok-oauth-helper"
                        aria-invalid={Boolean(tiktokError)}
                        disabled={!tiktokStatus?.configured || tiktokBusy}
                      />
                      <small id="tiktok-oauth-helper" className="source-oauth-helper">
                        Chỉ video công khai của chính tài khoản cấp quyền được đọc.
                      </small>
                      <button
                        className="button secondary source-run-button"
                        type="button"
                        onClick={connectTikTok}
                        disabled={
                          !tiktokStatus?.configured ||
                          !tiktokUsername.trim() ||
                          tiktokBusy
                        }
                      >
                        {tiktokBusy ? (
                          <LoaderCircle className="spin" size={15} />
                        ) : (
                          <LogIn size={15} />
                        )}
                        {tiktokStatus === null
                          ? "Đang kiểm tra OAuth"
                          : tiktokStatus.configured
                            ? "Kết nối bằng TikTok"
                            : "Cần cấu hình app TikTok"}
                      </button>
                    </>
                  )}
                </div>
              ) : (
                <div className="source-oauth-controls">
                  <button
                    className="button secondary source-run-button"
                    type="button"
                    onClick={() => onRunSource(source.id)}
                    disabled={
                      !selected || unavailable || Boolean(activeBatch) || running
                    }
                    aria-describedby={`source-detail-${source.id}`}
                  >
                    {sourceIsActive || pendingSourceId === source.id ? (
                      <LoaderCircle className="spin" size={15} />
                    ) : source.requires_login ? (
                      <LogIn size={15} />
                    ) : (
                      <Play size={15} />
                    )}
                    {sourceIsActive
                      ? "Đang chạy"
                      : pendingSourceId === source.id
                        ? "Đang khởi động"
                        : embedOnly
                          ? "Chỉ xem embed"
                          : channelOnly
                            ? "Dùng mục Kênh đã lưu"
                            : unavailable
                              ? primary?.implementation === "planned"
                                ? "Đang lên kế hoạch"
                                : "Cần cấu hình"
                              : source.requires_login
                                ? "Đăng nhập & quét"
                                : "Quét nguồn"}
                  </button>
                  {hasCbceProfile && (
                    <>
                      <label className="source-oauth-username" htmlFor={`crawler-connection-${source.id}`}>
                        Connection profile (tùy chọn)
                      </label>
                      <input
                        id={`crawler-connection-${source.id}`}
                        className="source-oauth-input"
                        value={crawlerConnectionIds[source.id] || ""}
                        onChange={(event) =>
                          setCrawlerConnectionIds((current) => ({
                            ...current,
                            [source.id]: event.target.value,
                          }))
                        }
                        placeholder="default hoặc bilibili-main"
                        maxLength={128}
                        autoComplete="off"
                        disabled={crawlerLoginActive || crawlerLoginBusy === source.id}
                        aria-describedby={`crawler-connection-helper-${source.id}`}
                      />
                      <small id={`crawler-connection-helper-${source.id}`} className="source-oauth-helper">
                        Mỗi tên dùng một profile trình duyệt riêng; không phải cookie.
                      </small>
                      {crawlerLoginErrors[source.id] && (
                        <span className="source-oauth-error" role="alert">
                          {crawlerLoginErrors[source.id]}
                        </span>
                      )}
                      <small className="source-oauth-helper">
                        {crawlerLogin?.detail ||
                          "Mở profile v2 riêng để đăng nhập thủ công trước khi review contract."}
                      </small>
                      <button
                        className={`button ${crawlerLoginActive ? "danger" : "secondary"}`}
                        type="button"
                        disabled={
                          crawlerLoginBusy === source.id ||
                          Boolean(activeBatch) ||
                          running
                        }
                        onClick={() =>
                          void (crawlerLoginActive
                            ? stopCrawlerLogin(source.id)
                            : startCrawlerLogin(source.id))
                        }
                      >
                        {crawlerLoginBusy === source.id ? (
                          <LoaderCircle className="spin" size={15} />
                        ) : crawlerLoginActive ? (
                          <CircleStop size={15} />
                        ) : (
                          <LogIn size={15} />
                        )}
                        {crawlerLoginActive
                          ? crawlerLogin?.state === "verifying"
                            ? "Dừng xác minh profile"
                            : "Đóng phiên đăng nhập"
                          : "Mở profile CBCE v2"}
                      </button>
                    </>
                  )}
                </div>
              )}
            </article>
          );
        })}
        {loading && <p className="subtle">Đang kiểm tra cấu hình…</p>}
      </div>
    </>
  );
}
