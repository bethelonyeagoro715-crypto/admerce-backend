'use client';

// React hook imports; this file contains a client-side React component.
import { useState, useEffect, useCallback, useMemo } from 'react';
import { useRouter } from 'next/navigation';
import api from '../../services/api';
import {
  MdNotifications,
  MdShoppingBag,
  MdChat,
  MdStorefront,
  MdVerified,
  MdFavorite,
  MdErrorOutline,
  MdRefresh,
  MdChevronRight,
  MdDoneAll,
  MdArrowBack,
  MdLocalShipping,
  MdStar,
  MdClose,
  MdAccountBalanceWallet,
  MdOutlineCampaign,
} from 'react-icons/md';

// ─── Types ──────────────────────────────────────────────────────────
interface NotificationItem {
  id: number;
  title?: string;
  body?: string;
  created_at?: string;
  is_read?: boolean;
  read?: boolean;
  type?: string;
  url?: string;
  data?: Record<string, unknown> | string;
  [key: string]: unknown;
}

type NotificationKind =
  | 'order'
  | 'message'
  | 'store'
  | 'verified'
  | 'favorite'
  | 'delivery'
  | 'review'
  | 'wallet'
  | 'promotion'
  | 'system';

type FilterTab = 'all' | 'orders' | 'messages' | 'updates';

// ─── Helpers ────────────────────────────────────────────────────────
function timeAgo(isoString: string): string {
  try {
    const now = Date.now();
    const then = new Date(isoString).getTime();
    const seconds = Math.floor((now - then) / 1000);
    if (seconds < 60) return 'just now';
    const minutes = Math.floor(seconds / 60);
    if (minutes < 60) return `${minutes}m ago`;
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return `${hours}h ago`;
    const days = Math.floor(hours / 24);
    if (days < 7) return `${days}d ago`;
    const weeks = Math.floor(days / 7);
    if (weeks < 5) return `${weeks}w ago`;
    const months = Math.floor(days / 30);
    if (months < 12) return `${months}mo ago`;
    return `${Math.floor(days / 365)}y ago`;
  } catch {
    return '';
  }
}

function dateGroup(isoString?: string): string {
  if (!isoString) return 'Earlier';
  try {
    const d = new Date(isoString);
    if (isNaN(d.getTime())) return 'Earlier';
    const now = new Date();
    const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
    const startOfNotif = new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
    const dayDiff = Math.floor((startOfToday - startOfNotif) / 86_400_000);
    if (dayDiff <= 0) return 'Today';
    if (dayDiff === 1) return 'Yesterday';
    if (dayDiff < 7) return 'This week';
    if (dayDiff < 30) return 'This month';
    return 'Older';
  } catch {
    return 'Earlier';
  }
}

function extractDataPayload(raw: unknown): Record<string, unknown> | null {
  if (!raw) return null;
  if (typeof raw === 'object' && !Array.isArray(raw)) return raw as Record<string, unknown>;
  if (typeof raw === 'string') {
    try {
      const parsed = JSON.parse(raw);
      if (parsed && typeof parsed === 'object') return parsed as Record<string, unknown>;
    } catch {
      return null;
    }
  }
  return null;
}

function classify(n: NotificationItem): NotificationKind {
  const raw = (n.type || '').toLowerCase();
  const t = (n.title || '').toLowerCase();

  // 🚨 FIX: Correctly classify voice notes and messages
  if (raw.includes('message') || raw.includes('chat') || t.includes('message') || t.includes('voice note')) {
    return 'message';
  }
  
  if (raw.includes('wallet') || raw.includes('payment') || raw.includes('refund')) return 'wallet';
  if (raw.includes('order') || t.includes('order') || t.includes('reservation')) return 'order';
  if (raw.includes('store')) return 'store';
  if (raw.includes('verif')) return 'verified';
  if (raw.includes('favorite') || raw.includes('save')) return 'favorite';
  if (raw.includes('deliver') || raw.includes('ship') || raw.includes('courier')) return 'delivery';
  if (raw.includes('review') || raw.includes('rating')) return 'review';
  if (raw.includes('promo') || raw.includes('announce')) return 'promotion';

  if (t.includes('refund') || t.includes('wallet')) return 'wallet';
  if (t.includes('deliver')) return 'delivery';
  if (t.includes('verified')) return 'verified';
  return 'system';
}

const KIND_META: Record<
  NotificationKind,
  { color: string; soft: string; Icon: React.ComponentType<{ size: number; color: string }>; actionText: string }
> = {
  order:    { color: 'var(--brand-primary)',  soft: 'var(--brand-soft)',  Icon: MdShoppingBag, actionText: 'View Order' },
  message:  { color: 'var(--info-fg)',        soft: 'var(--info-bg)',     Icon: MdChat, actionText: 'Reply' },
  store:    { color: 'var(--purple-fg)',      soft: 'var(--purple-bg)',   Icon: MdStorefront, actionText: 'View Store' },
  verified: { color: 'var(--success-fg)',     soft: 'var(--success-bg)',  Icon: MdVerified, actionText: 'View Profile' },
  favorite: { color: '#DB2777',               soft: 'color-mix(in srgb, #DB2777 15%, transparent)', Icon: MdFavorite, actionText: 'View Item' },
  delivery: { color: '#EA580C',               soft: 'color-mix(in srgb, #EA580C 15%, transparent)', Icon: MdLocalShipping, actionText: 'Track Package' },
  review:   { color: 'var(--warning-fg)',     soft: 'var(--warning-bg)',  Icon: MdStar, actionText: 'View Review' },
  wallet:   { color: '#059669',               soft: 'color-mix(in srgb, #059669 15%, transparent)', Icon: MdAccountBalanceWallet, actionText: 'Open Wallet' },
  promotion:{ color: '#D946EF',               soft: 'color-mix(in srgb, #D946EF 15%, transparent)', Icon: MdOutlineCampaign, actionText: 'See Offer' },
  system:   { color: 'var(--text-tertiary)',  soft: 'var(--bg-tertiary)', Icon: MdNotifications, actionText: 'View Details' },
};

function resolveDeepLink(n: NotificationItem): string | null {
  const data = extractDataPayload(n.data);
  const title = (n.title || '').toLowerCase();
  const type = (n.type || '').toLowerCase();

  // 1. Prioritize explicit URLs from backend (like instant receipts or chats with params)
  if (typeof n.url === 'string' && n.url.trim()) return n.url.trim();
  if (data) {
    const fromData = (typeof data.url === 'string' && data.url) || (typeof data.link === 'string' && data.link);
    if (fromData) return fromData;
  }

  // 2. Role-Aware Routing for Orders
  if (title.includes('new order') || title.includes('new reservation') || type.includes('store')) {
    return `/storekeeper/orders`;
  }
  
  if (data && typeof data.order_id === 'string') {
    return `/shopper/orders/receipt/${data.order_id}`;
  }

  // 3. Fallback for Chats (ensuring params are included)
  if (data && typeof data.conversation_id === 'string') {
    const otherId = data.otherUserId || '';
    const otherName = data.otherUserName || '';
    return `/chat/${data.conversation_id}?otherUserId=${otherId}&otherUserName=${otherName}`;
  }

  // 4. Fallbacks for other types
  if (data && typeof data.listing_id === 'string') return `/item-detail/${data.listing_id}`;
  if (data && typeof data.store_id === 'string') return `/store-detail/${data.store_id}`;
  
  if (classify(n) === 'wallet') return `/shopper/wallet`;

  return null;
}

function isRead(n: NotificationItem): boolean {
  if (typeof n.is_read === 'boolean') return n.is_read;
  if (typeof n.read === 'boolean') return n.read;
  return false;
}

function extractNotifications(data: unknown): NotificationItem[] {
  if (Array.isArray(data)) return data as NotificationItem[];
  if (data && typeof data === 'object') {
    const obj = data as Record<string, unknown>;
    for (const key of ['notifications', 'items', 'results', 'data', 'list']) {
      if (Array.isArray(obj[key])) return obj[key] as NotificationItem[];
    }
  }
  return [];
}

// ─── Main Component ─────────────────────────────────────────────────
export default function NotificationsPage() {
  const router = useRouter();

  const [notifications, setNotifications] = useState<NotificationItem[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [markingAll, setMarkingAll] = useState(false);
  
  const [activeTab, setActiveTab] = useState<FilterTab>('all');
  const [selectedNotif, setSelectedNotif] = useState<NotificationItem | null>(null);

  const loadNotifications = useCallback(async () => {
    setIsLoading(true);
    setError(null);
    try {
      const data = await api.getNotifications();
      setNotifications(extractNotifications(data));
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Failed to load notifications');
    } finally {
      setIsLoading(false);
    }
  }, []);

  useEffect(() => {
    const t = setTimeout(() => loadNotifications(), 0);
    return () => clearTimeout(t);
  }, [loadNotifications]);

  const markAsRead = useCallback(async (id: number) => {
    try {
      await api.markNotificationRead(id);
      setNotifications((prev) => prev.map((n) => (n.id === id ? { ...n, is_read: true } : n)));
    } catch {}
  }, []);

  const handleNotificationTap = async (n: NotificationItem) => {
    if (!isRead(n)) await markAsRead(n.id);
    setSelectedNotif(n);
  };

  const handleViewContent = () => {
    if (!selectedNotif) return;
    const url = resolveDeepLink(selectedNotif);
    if (url) router.push(url);
    setSelectedNotif(null);
  };

  const handleMarkAll = async () => {
    const unread = notifications.filter((n) => !isRead(n));
    if (unread.length === 0 || markingAll) return;
    setMarkingAll(true);
    try {
      await Promise.all(unread.map((n) => api.markNotificationRead(n.id).catch(() => null)));
      setNotifications((prev) => prev.map((n) => ({ ...n, is_read: true })));
    } finally {
      setMarkingAll(false);
    }
  };

  const unreadCount = useMemo(() => notifications.filter((n) => !isRead(n)).length, [notifications]);

  const filteredNotifications = useMemo(() => {
    return notifications.filter((n) => {
      if (activeTab === 'all') return true;
      const kind = classify(n);
      if (activeTab === 'orders') return kind === 'order' || kind === 'delivery' || kind === 'wallet';
      if (activeTab === 'messages') return kind === 'message';
      if (activeTab === 'updates') return kind === 'system' || kind === 'promotion' || kind === 'verified' || kind === 'favorite' || kind === 'review';
      return true;
    });
  }, [notifications, activeTab]);

  const groups = useMemo(() => {
    const out: { label: string; items: NotificationItem[] }[] = [];
    for (const n of filteredNotifications) {
      const label = dateGroup(n.created_at);
      const last = out[out.length - 1];
      if (last && last.label === label) last.items.push(n);
      else out.push({ label, items: [n] });
    }
    return out;
  }, [filteredNotifications]);

  const tabs: { id: FilterTab; label: string }[] = [
    { id: 'all', label: 'All' },
    { id: 'orders', label: 'Orders & Wallet' },
    { id: 'messages', label: 'Messages' },
    { id: 'updates', label: 'Updates' },
  ];

  return (
    <main className="nf-root">
      <style>{CSS}</style>

      <header className="nf-header">
        <button className="nf-backBtn" onClick={() => router.back()} aria-label="Go back">
          <MdArrowBack size={22} color="var(--text-primary)" />
        </button>
        <div className="nf-headerText">
          <h1 className="nf-title">Notifications</h1>
          {unreadCount > 0 && <span className="nf-unread">{unreadCount}</span>}
        </div>
        <button className="nf-iconBtn" onClick={loadNotifications} aria-label="Refresh" disabled={isLoading}>
          <MdRefresh size={20} color="var(--brand-primary)" />
        </button>
      </header>

      <div className="nf-tabsContainer">
        <div className="nf-tabsScroll">
          {tabs.map((tab) => (
            <button
              key={tab.id}
              className={`nf-tabBtn ${activeTab === tab.id ? 'nf-tabBtnActive' : ''}`}
              onClick={() => setActiveTab(tab.id)}
            >
              {tab.label}
            </button>
          ))}
        </div>
        {unreadCount > 0 && !isLoading && (
          <button className="nf-markAllBtn" onClick={handleMarkAll} disabled={markingAll}>
            <MdDoneAll size={16} color="var(--brand-primary)" />
            <span>Mark all</span>
          </button>
        )}
      </div>

      <div className="nf-body">
        {isLoading ? (
          <>
            {[0, 1, 2, 3].map((i) => (
              <div key={i} className="nf-skeletonCard" aria-hidden="true">
                <div className="nf-skel nf-skelIcon" />
                <div className="nf-skelBody">
                  <div className="nf-skel nf-skelLine" />
                  <div className="nf-skel nf-skelLine" style={{ width: '65%' }} />
                  <div className="nf-skel nf-skelLineShort" style={{ width: '30%' }} />
                </div>
              </div>
            ))}
          </>
        ) : error ? (
          <div className="nf-center">
            <MdErrorOutline size={48} color="var(--danger-fg)" />
            <p className="nf-centerText">{error}</p>
            <button type="button" className="nf-retryBtn" onClick={loadNotifications}>Retry</button>
          </div>
        ) : filteredNotifications.length === 0 ? (
          <div className="nf-center">
            <div className="nf-emptyIconWrap" aria-hidden="true">
              <MdNotifications size={40} color="var(--brand-primary)" />
            </div>
            <h2 className="nf-centerTitle">No notifications here</h2>
            <p className="nf-centerText">When you get updates in this category, they&apos;ll appear here.</p>
          </div>
        ) : (
          groups.map((group) => (
            <section key={group.label} className="nf-group">
              <div className="nf-groupLabel">{group.label}</div>
              {group.items.map((n) => {
                const read = isRead(n);
                const kind = classify(n);
                const meta = KIND_META[kind];
                const Icon = meta.Icon;
                const hasLink = Boolean(resolveDeepLink(n));
                const timeText = n.created_at ? timeAgo(n.created_at) : '';

                return (
                  <button
                    key={n.id}
                    type="button"
                    className={`nf-card ${read ? 'nf-cardRead' : 'nf-cardUnread'}`}
                    onClick={() => handleNotificationTap(n)}
                  >
                    <span className="nf-iconWrap" style={{ backgroundColor: meta.soft }} aria-hidden="true">
                      <Icon size={20} color={meta.color} />
                    </span>
                    <span className="nf-content">
                      <span className="nf-topRow">
                        <span className="nf-titleText">{n.title || 'Notification'}</span>
                        {!read && <span className="nf-dot" aria-hidden="true" />}
                      </span>
                      {n.body && <span className="nf-bodyText">{n.body}</span>}
                      <span className="nf-timeText">{timeText}</span>
                    </span>
                    {hasLink && <MdChevronRight size={20} color="var(--border-strong)" className="nf-chevron" />}
                  </button>
                );
              })}
            </section>
          ))
        )}
      </div>

      {selectedNotif && (
        <div className="nf-modalOverlay" onClick={() => setSelectedNotif(null)}>
          <div className="nf-modalSheet" onClick={(e) => e.stopPropagation()}>
            <div className="nf-modalGrabber" />
            
            <button className="nf-modalClose" onClick={() => setSelectedNotif(null)}>
              <MdClose size={24} color="var(--text-muted)" />
            </button>

            {(() => {
              const kind = classify(selectedNotif);
              const meta = KIND_META[kind];
              const Icon = meta.Icon;
              const hasLink = Boolean(resolveDeepLink(selectedNotif));

              return (
                <div className="nf-modalContent">
                  <div className="nf-modalIconWrap" style={{ backgroundColor: meta.soft }}>
                    <Icon size={36} color={meta.color} />
                  </div>
                  
                  <h2 className="nf-modalTitle">{selectedNotif.title || 'Notification'}</h2>
                  
                  {selectedNotif.created_at && (
                    <span className="nf-modalTime">{timeAgo(selectedNotif.created_at)}</span>
                  )}

                  <div className="nf-modalBody">
                    {selectedNotif.body || 'No further details provided.'}
                  </div>

                  {hasLink ? (
                    <button className="nf-modalActionBtn" onClick={handleViewContent}>
                      {meta.actionText} <MdChevronRight size={20} />
                    </button>
                  ) : (
                    <button className="nf-modalActionBtnSecondary" onClick={() => setSelectedNotif(null)}>
                      Dismiss
                    </button>
                  )}
                </div>
              );
            })()}
          </div>
        </div>
      )}
    </main>
  );
}

// ─── CSS (Keep as previously provided, it hasn't changed) ─────────────
const CSS = `
  @keyframes nf-shimmer {
    0% { background-position: -400px 0; }
    100% { background-position: 400px 0; }
  }
  @keyframes nf-slideUp {
    from { transform: translateY(100%); }
    to { transform: translateY(0); }
  }
  @keyframes nf-fadeIn {
    from { opacity: 0; transform: translateY(4px); }
    to { opacity: 1; transform: translateY(0); }
  }

  .nf-root {
    display: flex;
    flex-direction: column;
    min-height: 100vh;
    background: var(--bg-primary);
  }

  .nf-header {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 12px 16px;
    background: var(--bg-primary);
    position: sticky;
    top: 0;
    z-index: 20;
  }
  .nf-backBtn {
    width: 36px; height: 36px; border-radius: 50%; border: none;
    background: transparent; cursor: pointer; display: inline-flex;
    align-items: center; justify-content: center; flex: 0 0 36px;
  }
  .nf-backBtn:hover { background: var(--bg-hover); }
  .nf-headerText { flex: 1; min-width: 0; display: flex; align-items: center; gap: 8px; }
  .nf-title { font-size: 22px; font-weight: 800; color: var(--text-primary); margin: 0; letter-spacing: -0.03em; }
  .nf-unread { padding: 2px 8px; border-radius: 999px; background: var(--brand-primary); color: #fff; font-size: 12px; font-weight: 800; }
  .nf-iconBtn { width: 36px; height: 36px; border-radius: 50%; border: none; background: transparent; cursor: pointer; display: inline-flex; align-items: center; justify-content: center; flex: 0 0 36px; }
  .nf-iconBtn:hover { background: var(--bg-hover); }

  .nf-tabsContainer {
    display: flex; align-items: center; justify-content: space-between; padding: 0 16px;
    border-bottom: 1px solid var(--border-default); background: var(--bg-primary);
    position: sticky; top: 60px; z-index: 15;
  }
  .nf-tabsScroll { display: flex; gap: 16px; overflow-x: auto; scrollbar-width: none; -ms-overflow-style: none; flex: 1; padding: 12px 0; }
  .nf-tabsScroll::-webkit-scrollbar { display: none; }
  .nf-tabBtn { background: transparent; border: none; padding: 0; font-size: 15px; font-weight: 600; color: var(--text-muted); cursor: pointer; white-space: nowrap; position: relative; font-family: inherit; transition: color 0.2s; }
  .nf-tabBtn:hover { color: var(--text-primary); }
  .nf-tabBtnActive { color: var(--text-primary); font-weight: 800; }
  .nf-tabBtnActive::after { content: ''; position: absolute; bottom: -12px; left: 50%; transform: translateX(-50%); width: 100%; height: 3px; background: var(--brand-primary); border-radius: 3px 3px 0 0; }
  .nf-markAllBtn { display: inline-flex; align-items: center; gap: 4px; padding: 6px 10px; border-radius: 8px; border: 1px solid var(--border-default); background: var(--bg-secondary); color: var(--brand-primary); font-size: 12px; font-weight: 700; cursor: pointer; font-family: inherit; white-space: nowrap; margin-left: 12px; }
  .nf-markAllBtn:hover { background: var(--brand-soft); }

  .nf-body { flex: 1; padding: 16px; }
  .nf-group { margin-bottom: 24px; }
  .nf-groupLabel { font-size: 12px; font-weight: 800; letter-spacing: 0.5px; color: var(--text-primary); margin-bottom: 12px; }

  .nf-card { display: flex; align-items: flex-start; gap: 14px; width: 100%; padding: 16px; margin-bottom: 10px; border-radius: 16px; border: 1px solid var(--border-default); background: var(--bg-secondary); cursor: pointer; text-align: left; font-family: inherit; transition: all 0.15s ease; animation: nf-fadeIn 0.2s ease; }
  .nf-card:hover { border-color: var(--border-strong); transform: translateY(-1px); box-shadow: 0 4px 12px rgba(0,0,0,0.05); }
  .nf-cardUnread { border-color: color-mix(in srgb, var(--brand-primary) 30%, transparent); background: color-mix(in srgb, var(--brand-primary) 4%, var(--bg-secondary)); box-shadow: inset 3px 0 0 var(--brand-primary); }
  .nf-iconWrap { width: 44px; height: 44px; flex: 0 0 44px; display: inline-flex; align-items: center; justify-content: center; border-radius: 14px; }
  .nf-content { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 4px; }
  .nf-topRow { display: flex; align-items: flex-start; gap: 8px; min-width: 0; }
  .nf-titleText { font-size: 15px; font-weight: 700; color: var(--text-primary); flex: 1; line-height: 1.3; }
  .nf-cardRead .nf-titleText { font-weight: 600; color: var(--text-secondary); }
  .nf-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--brand-primary); flex: 0 0 8px; margin-top: 6px; }
  .nf-bodyText { font-size: 14px; color: var(--text-secondary); line-height: 1.45; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
  .nf-timeText { font-size: 12px; color: var(--text-muted); font-weight: 500; margin-top: 2px; }
  .nf-chevron { flex: 0 0 20px; align-self: center; opacity: 0.5; }

  .nf-center { display: flex; flex-direction: column; align-items: center; justify-content: center; padding: 80px 24px; text-align: center; }
  .nf-emptyIconWrap { width: 80px; height: 80px; border-radius: 30px; background: var(--bg-secondary); border: 1px solid var(--border-default); display: inline-flex; align-items: center; justify-content: center; margin-bottom: 16px; }
  .nf-centerTitle { font-size: 20px; font-weight: 800; color: var(--text-primary); margin: 0 0 8px; }
  .nf-centerText { font-size: 14px; color: var(--text-tertiary); margin: 0; max-width: 280px; line-height: 1.5; }
  .nf-retryBtn { margin-top: 16px; padding: 10px 24px; border-radius: 12px; background: var(--brand-primary); color: #fff; border: none; font-weight: 700; cursor: pointer; }

  .nf-skeletonCard { display: flex; gap: 14px; padding: 16px; margin-bottom: 10px; border-radius: 16px; background: var(--bg-secondary); border: 1px solid var(--border-default); }
  .nf-skel { background: var(--skeleton); background-size: 800px 100%; animation: nf-shimmer 1.4s infinite linear; border-radius: 8px; }
  .nf-skelIcon { width: 44px; height: 44px; flex: 0 0 44px; border-radius: 14px; }
  .nf-skelBody { flex: 1; display: flex; flex-direction: column; gap: 8px; }
  .nf-skelLine { height: 14px; width: 90%; }
  .nf-skelLineShort { height: 12px; }

  .nf-modalOverlay { position: fixed; inset: 0; background: rgba(0, 0, 0, 0.6); backdrop-filter: blur(4px); z-index: 100; display: flex; align-items: flex-end; justify-content: center; animation: nf-fadeIn 0.2s ease-out; }
  .nf-modalSheet { width: 100%; max-width: 600px; background: var(--bg-secondary); border-top-left-radius: 32px; border-top-right-radius: 32px; padding: 12px 24px 40px; position: relative; animation: nf-slideUp 0.3s cubic-bezier(0.22, 1, 0.36, 1); box-shadow: 0 -10px 40px rgba(0,0,0,0.2); }
  .nf-modalGrabber { width: 40px; height: 5px; background: var(--border-strong); border-radius: 10px; margin: 0 auto 24px; }
  .nf-modalClose { position: absolute; top: 20px; right: 20px; background: var(--bg-tertiary); border: none; border-radius: 50%; width: 36px; height: 36px; display: flex; align-items: center; justify-content: center; cursor: pointer; transition: background 0.2s; }
  .nf-modalClose:hover { background: var(--border-default); }
  .nf-modalContent { display: flex; flex-direction: column; align-items: center; text-align: center; padding-top: 8px; }
  .nf-modalIconWrap { width: 80px; height: 80px; border-radius: 50%; display: flex; align-items: center; justify-content: center; margin-bottom: 20px; box-shadow: 0 4px 12px rgba(0,0,0,0.05); }
  .nf-modalTitle { font-size: 24px; font-weight: 800; color: var(--text-primary); margin: 0 0 8px; letter-spacing: -0.02em; line-height: 1.2; }
  .nf-modalTime { font-size: 13px; font-weight: 600; color: var(--text-muted); margin-bottom: 24px; background: var(--bg-tertiary); padding: 6px 14px; border-radius: 999px; }
  .nf-modalBody { font-size: 16px; line-height: 1.6; color: var(--text-secondary); margin-bottom: 32px; width: 100%; text-align: left; background: var(--bg-primary); padding: 20px; border-radius: 16px; border: 1px solid var(--border-default); }
  .nf-modalActionBtn { width: 100%; padding: 18px; border-radius: 16px; border: none; background: var(--brand-primary); color: #fff; font-size: 16px; font-weight: 800; cursor: pointer; display: flex; align-items: center; justify-content: center; gap: 8px; transition: transform 0.1s; box-shadow: var(--shadow-brand); }
  .nf-modalActionBtn:active { transform: scale(0.98); }
  .nf-modalActionBtnSecondary { width: 100%; padding: 18px; border-radius: 16px; background: var(--bg-tertiary); color: var(--text-primary); border: 1px solid var(--border-default); font-size: 16px; font-weight: 800; cursor: pointer; }

  @media (min-width: 1024px) {
    .nf-modalOverlay { align-items: center; }
    .nf-modalSheet { border-radius: 32px; padding-bottom: 32px; max-width: 500px; }
    .nf-header, .nf-tabsContainer, .nf-body { max-width: 760px; margin: 0 auto; width: 100%; }
  }
`;