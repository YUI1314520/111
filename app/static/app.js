'use strict';

/**
 * Twitch 掉宝助手 —— 纯静态单页前端
 * ------------------------------------------------------------
 * 无框架、无构建、无外链；只通过 fetch 调用同源 /api/*。
 * 每 3 秒轮询 /api/state，页面隐藏时暂停。
 * 所有来自接口的文本都经过 esc() 转义后再拼进 innerHTML。
 */
(function () {
  /* ============================================================
   * 常量与全局状态
   * ============================================================ */
  const POLL_MS = 3000;        // /api/state 轮询间隔
  const DEVICE_POLL_MS = 3000; // 设备码状态轮询间隔
  const LOG_LIMIT = 200;
  const TABS = ['overview', 'campaigns', 'logs', 'settings'];

  /* 面板可能挂在子路径下（例如 http://host/drops/），所有 API 都要带同样的前缀 */
  const API_BASE = (function () {
    let path = window.location.pathname || '/';
    if (!path.endsWith('/')) path = path.replace(/[^/]*$/, '');
    return path.replace(/\/+$/, '');
  })();

  const STATE_LABELS = {
    idle: '空闲',
    watching: '观看中',
    scanning: '扫描中',
    error: '出错',
    disabled: '已暂停'
  };

  const S = {
    authed: false,
    data: null,
    tab: 'overview',
    pollTimer: 0,
    polling: false,

    // 添加账号弹窗：{ root, device, deviceTimer }
    // device: { phase, deviceCode, userCode, verificationUri, expiresAt, polling, message }
    modal: null,

    // 设置页
    settingsLoaded: false,
    defaultGames: [],
    gameCache: new Map(),        // slug -> game（含搜索结果）
    gameSearch: { timer: 0, seq: 0, results: [], lastQuery: '' },

    // 活动筛选
    filters: { campaignGame: '', campaignsMine: false },
    campaignFilterSig: '',

    // 日志
    logs: {
      accountId: '',
      autoScroll: true,
      list: [],
      loading: false,
      lastFetch: 0,
      filterSig: ''
    }
  };

  /* ============================================================
   * DOM / 通用工具
   * ============================================================ */
  const $ = (selector, root) => (root || document).querySelector(selector);
  const $$ = (selector, root) =>
    Array.prototype.slice.call((root || document).querySelectorAll(selector));
  const byId = (id) => document.getElementById(id);

  const ESCAPE_MAP = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };

  /** 转义任意文本，供 innerHTML 模板使用（XSS 防护） */
  function esc(value) {
    if (value === null || value === undefined) return '';
    return String(value).replace(/[&<>"']/g, (ch) => ESCAPE_MAP[ch]);
  }

  /** 仅允许 http(s) 链接，返回转义后的 URL；非法链接返回空串 */
  function safeUrl(raw) {
    if (!raw) return '';
    try {
      const url = new URL(String(raw), window.location.origin);
      if (url.protocol !== 'http:' && url.protocol !== 'https:') return '';
      return esc(url.href);
    } catch (err) {
      return '';
    }
  }

  function num(value) {
    const n = Number(value);
    return Number.isFinite(n) ? n : 0;
  }

  function clampPercent(current, required) {
    if (!(required > 0)) return 0;
    return Math.max(0, Math.min(100, Math.round((current / required) * 100)));
  }

  /** “剩余 3天2小时” / “已结束” */
  function fmtCountdown(iso) {
    if (!iso) return '';
    const end = new Date(iso).getTime();
    if (!Number.isFinite(end)) return '';
    const diff = end - Date.now();
    if (diff <= 0) return '已结束';
    const totalMinutes = Math.floor(diff / 60000);
    const days = Math.floor(totalMinutes / 1440);
    const hours = Math.floor((totalMinutes % 1440) / 60);
    const minutes = totalMinutes % 60;
    if (days > 0) return '剩余 ' + days + '天' + hours + '小时';
    if (hours > 0) return '剩余 ' + hours + '小时' + minutes + '分';
    return '剩余 ' + Math.max(1, minutes) + '分钟';
  }

  function fmtUptime(seconds) {
    const total = Math.max(0, Math.floor(num(seconds)));
    const days = Math.floor(total / 86400);
    const hours = Math.floor((total % 86400) / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    if (days > 0) return days + ' 天 ' + hours + ' 小时';
    if (hours > 0) return hours + ' 小时 ' + minutes + ' 分';
    if (minutes > 0) return minutes + ' 分';
    return total + ' 秒';
  }

  function fmtDateTime(iso) {
    if (!iso) return '';
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return '';
    return date.toLocaleString('zh-CN', {
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      hour12: false
    });
  }

  function fmtLogTime(iso) {
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return iso ? String(iso) : '';
    const pad = (n) => String(n).padStart(2, '0');
    return pad(date.getMonth() + 1) + '-' + pad(date.getDate()) + ' ' +
      pad(date.getHours()) + ':' + pad(date.getMinutes()) + ':' + pad(date.getSeconds());
  }

  /** 右下角 toast，2 秒后消失 */
  function toast(message, type) {
    const root = byId('toast-root');
    if (!root) return;
    const el = document.createElement('div');
    el.className = 'toast' + (type === 'error' ? ' toast-error' : type === 'success' ? ' toast-success' : '');
    el.textContent = message;
    root.appendChild(el);
    window.setTimeout(function () {
      el.classList.add('is-leaving');
      window.setTimeout(function () { el.remove(); }, 220);
    }, 2000);
  }

  function setLoginError(message) {
    const el = byId('login-error');
    if (!el) return;
    el.textContent = message || '';
    el.hidden = !message;
  }

  /** 按钮 loading 文案 + 禁用；结束后恢复 */
  async function withBusy(btn, label, task, options) {
    const opts = options || {};
    if (btn) {
      if (btn.disabled) return undefined;
      btn.dataset.busyLabel = btn.textContent;
      btn.disabled = true;
      if (label) btn.textContent = label;
    }
    try {
      return await task();
    } catch (err) {
      if (opts.onError) opts.onError(err);
      else if (!opts.silent) toast((err && err.message) || '操作失败', 'error');
      return undefined;
    } finally {
      if (btn && btn.isConnected) {
        btn.disabled = false;
        if (btn.dataset.busyLabel !== undefined) {
          btn.textContent = btn.dataset.busyLabel;
          delete btn.dataset.busyLabel;
        }
      }
    }
  }

  class ApiError extends Error {
    constructor(message, status, payload) {
      super(message);
      this.name = 'ApiError';
      this.status = status || 0;
      this.payload = payload;
    }
  }

  /** 统一 fetch：始终带 same-origin cookie；401 默认回到登录页 */
  async function api(path, options) {
    const opts = options || {};
    const init = {
      method: opts.method || 'GET',
      credentials: 'same-origin',
      headers: { Accept: 'application/json' }
    };
    if (opts.body !== undefined) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(opts.body);
    }

    let res;
    try {
      res = await fetch(API_BASE + path, init);
    } catch (err) {
      throw new ApiError('网络请求失败，请检查连接', 0);
    }

    let payload = null;
    const text = await res.text();
    if (text) {
      try { payload = JSON.parse(text); } catch (err) { payload = null; }
    }

    if (res.status === 401) {
      if (!opts.raw401) onUnauthorized();
      throw new ApiError((payload && payload.error) || '登录状态已失效', 401, payload);
    }
    if (!res.ok || (payload && payload.ok === false)) {
      throw new ApiError(
        (payload && payload.error) || ('请求失败（HTTP ' + res.status + '）'),
        res.status,
        payload
      );
    }
    return payload || {};
  }

  /* ============================================================
   * 登录 / 视图切换 / 轮询
   * ============================================================ */
  function onUnauthorized() {
    if (!S.authed) return;
    S.authed = false;
    stopPolling();
    showLogin();
    toast('登录已过期，请重新登录', 'error');
  }

  function showLogin() {
    S.authed = false;
    stopPolling();
    closeModal({ cancel: false });
    S.data = null;
    S.settingsLoaded = false;
    S.defaultGames = [];
    S.logs.list = [];
    S.logs.lastFetch = 0;
    S.logs.accountId = '';
    S.logs.filterSig = '';

    byId('boot-view').classList.add('hidden');
    byId('app-view').classList.add('hidden');
    byId('login-view').classList.remove('hidden');

    const loginBtn = byId('login-btn');
    if (loginBtn) {
      loginBtn.disabled = false;
      loginBtn.textContent = '登录';
    }
    setLoginError('');
    const input = byId('password-input');
    if (input) {
      input.value = '';
      window.setTimeout(function () { input.focus(); }, 50);
    }
  }

  function enterApp() {
    S.authed = true;
    byId('boot-view').classList.add('hidden');
    byId('login-view').classList.add('hidden');
    byId('app-view').classList.remove('hidden');
  }

  function showBootError(message) {
    byId('boot-text').textContent = message || '连接失败';
    byId('boot-retry').classList.remove('hidden');
    const spinner = $('.spinner', byId('boot-view'));
    if (spinner) spinner.classList.add('hidden');
  }

  async function connect() {
    byId('login-view').classList.add('hidden');
    byId('app-view').classList.add('hidden');
    byId('boot-view').classList.remove('hidden');
    byId('boot-text').textContent = '正在连接服务器…';
    byId('boot-retry').classList.add('hidden');
    const spinner = $('.spinner', byId('boot-view'));
    if (spinner) spinner.classList.remove('hidden');

    try {
      const data = await api('/api/state', { raw401: true });
      hydrate(data, { force: true });
      enterApp();
      selectTab(tabFromHash());
      startPolling();
    } catch (err) {
      if (err.status === 401) {
        showLogin();
        return;
      }
      showBootError(err.message || '无法连接服务器');
    }
  }

  function startPolling() {
    stopPolling();
    if (!S.authed) return;
    S.pollTimer = window.setInterval(function () { tick(); }, POLL_MS);
  }

  function stopPolling() {
    if (S.pollTimer) {
      clearInterval(S.pollTimer);
      S.pollTimer = 0;
    }
  }

  async function tick() {
    if (!S.authed || S.polling || document.hidden) return;
    S.polling = true;
    try {
      const data = await api('/api/state');
      S.authed = true;
      hydrate(data);
    } catch (err) {
      // 401 已由 onUnauthorized 处理；其它错误静默等待下一轮
    } finally {
      S.polling = false;
    }
  }

  function onVisibilityChange() {
    if (document.hidden) {
      stopPolling();
      return;
    }
    if (!S.authed) return;
    tick();
    startPolling();
  }

  /** 使用最新 state 刷新当前界面 */
  function hydrate(data, options) {
    S.data = data || {};

    if (!S.settingsLoaded) {
      const settings = S.data.settings || {};
      S.defaultGames = Array.isArray(settings.default_games)
        ? settings.default_games.filter(function (slug) { return typeof slug === 'string' && slug; })
        : [];
      S.settingsLoaded = true;
    }

    const games = Array.isArray(S.data.games) ? S.data.games : [];
    games.forEach(function (game) {
      if (game && game.slug) S.gameCache.set(game.slug, game);
    });

    renderTopbar();
    const force = Boolean(options && options.force);

    if (S.tab === 'overview') {
      renderOverview(force);
    } else if (S.tab === 'campaigns') {
      renderCampaigns();
    } else if (S.tab === 'logs') {
      renderLogFilters();
      if (force || Date.now() - S.logs.lastFetch > 2000) loadLogs();
    } else if (S.tab === 'settings') {
      renderServerInfo();
      if (!isEditingSettings()) renderSettings();
    }
  }

  async function refreshState(options) {
    const data = await api('/api/state');
    S.authed = true;
    hydrate(data, options || {});
  }

  /* ============================================================
   * 顶部栏 / Tab / 通用渲染
   * ============================================================ */
  function renderTopbar() {
    const accounts = (S.data && S.data.accounts) || [];
    let online = 0;
    let mining = 0;
    accounts.forEach(function (account) {
      const state = account.status && account.status.state;
      if (account.enabled && state !== 'disabled') online += 1;
      if (state === 'watching' || state === 'scanning') mining += 1;
    });
    const el = byId('topbar-stats');
    if (el) el.textContent = '在线 ' + online + ' · 挖宝 ' + mining;
  }

  function tabFromHash() {
    const raw = String(window.location.hash || '').replace(/^#\/?/, '');
    return TABS.indexOf(raw) >= 0 ? raw : 'overview';
  }

  function selectTab(tab) {
    if (TABS.indexOf(tab) === -1) tab = 'overview';
    S.tab = tab;

    $$('.tab').forEach(function (btn) {
      const active = btn.dataset.tab === tab;
      btn.classList.toggle('is-active', active);
      btn.setAttribute('aria-selected', active ? 'true' : 'false');
    });

    TABS.forEach(function (name) {
      const view = byId('view-' + name);
      if (view) view.classList.toggle('hidden', name !== tab);
    });

    if (tab === 'overview') renderOverview(true);
    else if (tab === 'campaigns') renderCampaigns();
    else if (tab === 'logs') { renderLogFilters(); loadLogs({ showLoading: true }); }
    else if (tab === 'settings') renderSettings();
  }

  function goTab(tab) {
    if (tabFromHash() === tab) selectTab(tab);
    else window.location.hash = '#/' + tab;
  }

  function emptyState(text, icon) {
    return '<div class="empty"><div class="empty-icon" aria-hidden="true">' +
      (icon || '📭') + '</div><p>' + esc(text) + '</p></div>';
  }

  /** 图片加载失败时隐藏（封面、图标等） */
  function bindImgFallbacks(root) {
    $$('img', root || document).forEach(function (img) {
      if (img.dataset.fallbackBound) return;
      img.dataset.fallbackBound = '1';
      const hide = function () { img.classList.add('is-hidden'); };
      img.addEventListener('error', hide, { once: true });
      if (img.complete && img.naturalWidth === 0) hide();
    });
  }

  /* ============================================================
   * Tab 1：总览 / 账号卡片
   * ============================================================ */
  function findAccount(id) {
    const accounts = (S.data && S.data.accounts) || [];
    return accounts.find(function (account) { return String(account.id) === String(id); }) || null;
  }

  function renderOverview(force) {
    if (!S.data) return;
    const grid = byId('accounts-grid');
    if (!grid) return;
    // 用户正在操作下拉框或开关时，避免 3 秒轮询打断
    if (!force && grid.contains(document.activeElement)) return;

    const accounts = S.data.accounts || [];
    const enabledCount = accounts.filter(function (account) { return account.enabled; }).length;
    byId('overview-sub').textContent = accounts.length
      ? '共 ' + accounts.length + ' 个账号 · ' + enabledCount + ' 个已启用'
      : '';

    if (!accounts.length) {
      grid.innerHTML = emptyState('还没有账号，点击右上角「添加账号」开始。', '👤');
      return;
    }

    grid.innerHTML = accounts.map(accountCardHtml).join('');
    bindImgFallbacks(grid);
  }

  function accountCardHtml(account) {
    const id = num(account.id);
    const status = account.status || {};
    const stateKey = String(status.state || (account.enabled ? 'idle' : 'disabled'));
    const stateClass = ['idle', 'watching', 'scanning', 'error', 'disabled'].indexOf(stateKey) >= 0
      ? stateKey : 'idle';
    const stateLabel = STATE_LABELS[stateClass] || stateKey;
    const mode = account.mode === 'monitor' ? 'monitor' : 'farm';
    const login = account.login || account.display_name || '';
    const name = account.name || account.display_name || account.login || '未命名账号';

    const current = Math.max(0, Math.round(num(status.current_minutes)));
    const required = Math.max(0, Math.round(num(status.required_minutes)));
    const percent = clampPercent(current, required);
    const progressTitle = status.drop || status.campaign || '';

    const channelHtml = status.channel
      ? '<span class="mono">' + esc(status.channel) + '</span>'
      : '<span class="muted">—</span>';
    const gameHtml = status.game ? esc(status.game) : '<span class="muted">—</span>';

    let progressHtml;
    if (required > 0) {
      progressHtml =
        '<div class="progress-block">' +
          '<div class="progress-meta">' +
            '<span class="progress-title" title="' + esc(progressTitle) + '">' +
              esc(progressTitle || '掉宝进度') + '</span>' +
            '<span class="progress-num">' + current + '/' + required + ' 分钟</span>' +
          '</div>' +
          '<div class="bar" role="progressbar" aria-valuemin="0" aria-valuemax="' + required +
               '" aria-valuenow="' + Math.min(current, required) + '">' +
            '<span class="bar-fill" style="width:' + percent + '%"></span>' +
          '</div>' +
          (status.campaign && status.drop
            ? '<p class="muted small">活动：' + esc(status.campaign) + '</p>'
            : '') +
        '</div>';
    } else {
      const hint = account.enabled
        ? (status.drop || status.campaign ? '正在等待掉宝进度…' : '当前没有进行中的掉宝')
        : '账号已暂停';
      progressHtml = '<p class="muted small">' + esc(hint) + '</p>';
    }

    const stats = account.stats || {};
    const errorHtml = account.last_error
      ? '<p class="acct-error">最后错误：' + esc(account.last_error) + '</p>'
      : '';

    return '<article class="card account-card" data-id="' + id + '">' +
      '<div class="acct-head">' +
        '<div class="acct-ident">' +
          '<h3 class="acct-name">' + esc(name) + '</h3>' +
          '<p class="acct-login">@' + esc(login || 'unknown') + '</p>' +
        '</div>' +
        '<label class="switch" title="' + (account.enabled ? '点击暂停' : '点击启用') + '">' +
          '<input type="checkbox" data-act="toggle"' + (account.enabled ? ' checked' : '') +
                 ' aria-label="启用或暂停账号 ' + esc(name) + '">' +
          '<span class="switch-track"></span>' +
        '</label>' +
      '</div>' +
      '<div class="acct-row">' +
        '<label class="inline-field">模式' +
          '<select class="select" data-act="mode" aria-label="账号运行模式">' +
            '<option value="farm"' + (mode === 'farm' ? ' selected' : '') + '>自动挖宝</option>' +
            '<option value="monitor"' + (mode === 'monitor' ? ' selected' : '') + '>只检测</option>' +
          '</select>' +
        '</label>' +
        '<span class="badge state-' + stateClass + '">' + esc(stateLabel) + '</span>' +
      '</div>' +
      '<div class="acct-status">' +
        '<p class="acct-line">' + (account.enabled ? '正在观看 ' + channelHtml : '已暂停') + '</p>' +
        '<p class="acct-line">游戏：' + gameHtml + '</p>' +
        (status.text ? '<p class="acct-line muted">' + esc(status.text) + '</p>' : '') +
      '</div>' +
      progressHtml +
      '<div class="acct-stats">' +
        '<span>今日领取 <b>' + num(stats.claimed_24h) + '</b></span>' +
        '<span>累计领取 <b>' + num(stats.claimed_total) + '</b></span>' +
        '<span>今日观看 <b>' + Math.round(num(stats.minutes_24h)) + '</b> 分</span>' +
      '</div>' +
      errorHtml +
      '<div class="acct-actions">' +
        '<button class="btn btn-sm" type="button" data-act="refresh">刷新</button>' +
        '<button class="btn btn-sm btn-danger-ghost" type="button" data-act="delete">删除</button>' +
      '</div>' +
    '</article>';
  }

  function bindOverviewEvents() {
    const grid = byId('accounts-grid');
    if (!grid) return;

    grid.addEventListener('click', async function (event) {
      const btn = event.target.closest('button[data-act]');
      if (!btn) return;
      const card = btn.closest('[data-id]');
      if (!card) return;
      const id = num(card.dataset.id);
      if (!id) return;

      if (btn.dataset.act === 'refresh') {
        await withBusy(btn, '刷新中…', async function () {
          await api('/api/accounts/' + encodeURIComponent(id) + '/refresh', { method: 'POST' });
          toast('已请求刷新');
          await refreshState({ force: true });
        });
      } else if (btn.dataset.act === 'delete') {
        const account = findAccount(id);
        const label = account ? (account.name || account.login || ('#' + id)) : ('#' + id);
        if (!window.confirm('确定删除账号「' + label + '」吗？此操作不可恢复。')) return;
        await withBusy(btn, '删除中…', async function () {
          await api('/api/accounts/' + encodeURIComponent(id), { method: 'DELETE' });
          toast('账号已删除');
          await refreshState({ force: true });
        });
      }
    });

    grid.addEventListener('change', function (event) {
      const control = event.target;
      const card = control.closest('[data-id]');
      if (!card) return;
      const id = num(card.dataset.id);
      if (!id) return;

      if (control.dataset.act === 'toggle') {
        patchAccount(id, { enabled: control.checked }, control);
      } else if (control.dataset.act === 'mode') {
        patchAccount(id, { mode: control.value }, control);
      }
    });
  }

  async function patchAccount(id, patch, control) {
    if (control) control.disabled = true;
    try {
      await api('/api/accounts/' + encodeURIComponent(id), { method: 'PATCH', body: patch });
      toast('已更新');
      await refreshState({ force: true });
    } catch (err) {
      toast(err.message || '更新失败', 'error');
      if (control && control.isConnected) control.disabled = false;
      try { await refreshState({ force: true }); } catch (ignored) { /* 已处理 */ }
    }
  }

  /* ============================================================
   * 添加账号弹窗（设备码 / 手动 token）
   * ============================================================ */
  function openAddAccountModal() {
    closeModal({ cancel: false });
    const root = byId('modal-root');
    root.innerHTML =
      '<div class="modal-backdrop">' +
        '<div class="modal" role="dialog" aria-modal="true" aria-labelledby="add-account-title">' +
          '<div class="modal-head">' +
            '<h2 id="add-account-title">添加账号</h2>' +
            '<button class="icon-btn" type="button" data-modal-close="1" aria-label="关闭">✕</button>' +
          '</div>' +
          '<div class="modal-body">' +
            '<section class="modal-section">' +
              '<h3>设备码登录 <span class="pill">推荐</span></h3>' +
              '<p class="muted small">登录 Twitch 后输入生成的验证码，账号会自动添加到面板。</p>' +
              '<div id="device-panel" class="device-panel"></div>' +
            '</section>' +
            '<div class="divider"><span>或</span></div>' +
            '<section class="modal-section">' +
              '<h3>手动粘贴 auth-token</h3>' +
              '<form id="token-form" class="stack" novalidate>' +
                '<label class="field"><span>账号名称</span>' +
                  '<input id="token-name" class="input" maxlength="60" placeholder="例如：小号" autocomplete="off">' +
                '</label>' +
                '<label class="field"><span>auth-token</span>' +
                  '<textarea id="token-value" class="input textarea" rows="3" placeholder="粘贴 auth-token" spellcheck="false"></textarea>' +
                '</label>' +
                '<p id="token-error" class="form-error" hidden></p>' +
                '<button id="token-submit" class="btn" type="submit">添加账号</button>' +
              '</form>' +
            '</section>' +
          '</div>' +
          '<div class="modal-foot">' +
            '<button id="modal-cancel" class="btn btn-ghost" type="button">取消</button>' +
          '</div>' +
        '</div>' +
      '</div>';

    S.modal = { root: root, device: null, deviceTimer: 0 };
    document.body.classList.add('modal-open');
    renderDevicePanel();
    bindModalEvents();
    // 打开即申请设备码，减少一次点击；关闭弹窗会尽力取消该设备码
    startDeviceFlow();
  }

  function bindModalEvents() {
    const root = byId('modal-root');
    const backdrop = $('.modal-backdrop', root);
    if (backdrop) {
      backdrop.addEventListener('click', function (event) {
        if (event.target === backdrop || event.target.closest('[data-modal-close]')) closeModal();
      });
    }
    const cancelBtn = byId('modal-cancel');
    if (cancelBtn) cancelBtn.addEventListener('click', function () { closeModal(); });

    const form = byId('token-form');
    if (form) form.addEventListener('submit', submitTokenForm);

    const panel = byId('device-panel');
    if (panel) panel.addEventListener('click', onDevicePanelClick);
  }

  function renderDevicePanel() {
    const panel = byId('device-panel');
    if (!panel) return;
    const device = S.modal && S.modal.device;

    if (!device) {
      panel.innerHTML =
        '<button id="device-start" class="btn btn-primary" type="button">生成设备码</button>' +
        '<p id="device-error" class="form-error" hidden></p>';
      return;
    }

    if (device.phase === 'starting') {
      panel.innerHTML = '<p class="muted small">正在生成设备码…</p>';
      return;
    }

    if (device.phase === 'pending') {
      panel.innerHTML =
        '<div class="device-code" aria-live="polite">' + esc(device.userCode) + '</div>' +
        '<p class="muted small">在 Twitch 页面输入上方验证码：</p>' +
        '<a class="btn btn-primary" href="' +
          (safeUrl(device.verificationUri) || 'https://www.twitch.tv/activate') +
          '" target="_blank" rel="noopener noreferrer">打开 twitch.tv/activate</a>' +
        '<p class="device-status" id="device-status"><span class="dot"></span>等待授权…</p>' +
        '<button id="device-cancel" class="btn btn-ghost btn-sm" type="button">取消授权</button>';
      updateDeviceStatus();
      return;
    }

    panel.innerHTML =
      '<p class="form-error">' + esc(device.message || '设备码已失效，请重新生成。') + '</p>' +
      '<button id="device-restart" class="btn btn-primary" type="button">重新生成设备码</button>';
  }

  function updateDeviceStatus() {
    const device = S.modal && S.modal.device;
    const el = byId('device-status');
    if (!device || !el) return;
    const left = Math.max(0, Math.ceil((device.expiresAt - Date.now()) / 1000));
    const minutes = Math.floor(left / 60);
    const seconds = String(left % 60).padStart(2, '0');
    el.innerHTML = '<span class="dot"></span>等待授权…（' + minutes + ':' + seconds + ' 后过期）';
  }

  function onDevicePanelClick(event) {
    const btn = event.target.closest('button');
    if (!btn) return;
    if (btn.id === 'device-start' || btn.id === 'device-restart') {
      startDeviceFlow();
    } else if (btn.id === 'device-cancel') {
      cancelDeviceFlow({ toastMessage: '已取消设备码授权' });
    }
  }

  async function startDeviceFlow() {
    if (!S.modal) return;
    stopDeviceFlowTimer();
    S.modal.device = { phase: 'starting' };
    renderDevicePanel();

    try {
      const res = await api('/api/accounts/device', { method: 'POST' });
      if (!res || !res.device_code || !res.user_code) {
        throw new Error('服务器返回的设备码数据不完整');
      }
      if (!S.modal) {
        // 用户已关闭弹窗，尽力取消刚创建出来的设备码
        api('/api/accounts/device/' + encodeURIComponent(res.device_code) + '/cancel', { method: 'POST' })
          .catch(function () { /* 忽略 */ });
        return;
      }
      const expiresIn = num(res.expires_in) > 0 ? num(res.expires_in) : 1800;
      S.modal.device = {
        phase: 'pending',
        deviceCode: String(res.device_code),
        userCode: String(res.user_code),
        verificationUri: res.verification_uri || 'https://www.twitch.tv/activate',
        expiresAt: Date.now() + expiresIn * 1000,
        polling: false
      };
      renderDevicePanel();
      startDevicePolling();
    } catch (err) {
      if (!S.modal || err.status === 401) return;
      S.modal.device = { phase: 'error', message: err.message || '生成设备码失败' };
      renderDevicePanel();
    }
  }

  function startDevicePolling() {
    stopDeviceFlowTimer();
    if (!S.modal) return;
    S.modal.deviceTimer = window.setInterval(pollDeviceFlow, DEVICE_POLL_MS);
  }

  function stopDeviceFlowTimer() {
    if (S.modal && S.modal.deviceTimer) {
      clearInterval(S.modal.deviceTimer);
      S.modal.deviceTimer = 0;
    }
  }

  async function pollDeviceFlow() {
    const modal = S.modal;
    const device = modal && modal.device;
    if (!device || device.phase !== 'pending' || device.polling) return;

    if (Date.now() >= device.expiresAt) {
      stopDeviceFlowTimer();
      device.phase = 'expired';
      device.message = '设备码已过期，请重新生成。';
      renderDevicePanel();
      return;
    }

    device.polling = true;
    try {
      const res = await api('/api/accounts/device/' + encodeURIComponent(device.deviceCode));
      if (!S.modal || S.modal.device !== device) return;

      if (res.status === 'pending') {
        updateDeviceStatus();
        return;
      }
      if (res.status === 'done') {
        stopDeviceFlowTimer();
        closeModal({ cancel: false });
        toast('账号已添加');
        await refreshState({ force: true });
        return;
      }

      stopDeviceFlowTimer();
      device.phase = res.status === 'expired' ? 'expired' : 'error';
      device.message = res.error || (res.status === 'expired'
        ? '设备码已过期，请重新生成。'
        : '授权失败，请重新生成设备码。');
      renderDevicePanel();
    } catch (err) {
      if (!S.modal || S.modal.device !== device) return;
      if (err.status === 401) return;
      const statusEl = byId('device-status');
      if (statusEl) statusEl.textContent = '等待授权…（网络波动，自动重试中）';
    } finally {
      device.polling = false;
    }
  }

  async function cancelDeviceFlow(options) {
    const opts = options || {};
    const modal = S.modal;
    const device = modal && modal.device;
    if (!device || !device.deviceCode) {
      if (modal) {
        modal.device = null;
        renderDevicePanel();
      }
      return;
    }

    stopDeviceFlowTimer();
    try {
      await api('/api/accounts/device/' + encodeURIComponent(device.deviceCode) + '/cancel',
        { method: 'POST' });
    } catch (err) {
      if (err.status !== 401) toast(err.message || '取消设备码失败', 'error');
    }
    if (S.modal) {
      S.modal.device = null;
      renderDevicePanel();
    }
    if (opts.toastMessage) toast(opts.toastMessage);
  }

  function closeModal(options) {
    const opts = options || {};
    const modal = S.modal;
    if (!modal) return;

    if (modal.deviceTimer) {
      clearInterval(modal.deviceTimer);
      modal.deviceTimer = 0;
    }
    const device = modal.device;
    if (opts.cancel !== false && device && device.phase === 'pending' && device.deviceCode) {
      // 用户主动关闭：尽力通知后端取消设备码，不阻塞界面
      api('/api/accounts/device/' + encodeURIComponent(device.deviceCode) + '/cancel', { method: 'POST' })
        .catch(function () { /* 忽略 */ });
    }

    S.modal = null;
    const root = byId('modal-root');
    if (root) root.innerHTML = '';
    document.body.classList.remove('modal-open');
  }

  async function submitTokenForm(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const nameInput = byId('token-name');
    const tokenInput = byId('token-value');
    const errorEl = byId('token-error');
    const submitBtn = byId('token-submit');
    if (!form || !nameInput || !tokenInput || !submitBtn) return;

    const token = tokenInput.value.trim();
    const name = nameInput.value.trim();
    errorEl.hidden = true;
    errorEl.textContent = '';

    if (!token) {
      errorEl.textContent = '请粘贴 auth-token';
      errorEl.hidden = false;
      tokenInput.focus();
      return;
    }

    await withBusy(submitBtn, '添加中…', async function () {
      await api('/api/accounts/token', { method: 'POST', body: { token: token, name: name } });
      toast('账号已添加');
      closeModal({ cancel: false });
      await refreshState({ force: true });
    }, {
      onError: function (err) {
        errorEl.textContent = err.message || '添加失败';
        errorEl.hidden = false;
      }
    });
  }

  /* ============================================================
   * Tab 2：活动
   * ============================================================ */
  function myGameSlugs() {
    const set = new Set(S.defaultGames);
    const accounts = (S.data && S.data.accounts) || [];
    accounts.forEach(function (account) {
      (account.games || []).forEach(function (slug) { if (slug) set.add(slug); });
    });
    return set;
  }

  function renderCampaigns() {
    if (!S.data) return;
    renderCampaignFilters();

    const all = Array.isArray(S.data.campaigns) ? S.data.campaigns.slice() : [];
    const mine = myGameSlugs();
    const filtered = all.filter(function (campaign) {
      const slug = campaign.game && campaign.game.slug;
      if (S.filters.campaignGame && slug !== S.filters.campaignGame) return false;
      if (S.filters.campaignsMine) {
        const hasAccounts = Array.isArray(campaign.accounts) && campaign.accounts.length > 0;
        if (!(mine.has(slug) || hasAccounts)) return false;
      }
      return true;
    });

    filtered.sort(function (a, b) {
      const rank = function (campaign) { return campaign.status === 'UPCOMING' ? 1 : 0; };
      if (rank(a) !== rank(b)) return rank(a) - rank(b);
      return new Date(a.ends_at || 0).getTime() - new Date(b.ends_at || 0).getTime();
    });

    const sub = byId('campaigns-sub');
    if (sub) {
      sub.textContent = all.length
        ? '显示 ' + filtered.length + ' / ' + all.length + ' 个活动'
        : '';
    }

    const list = byId('campaigns-list');
    if (!list) return;
    if (!all.length) {
      list.innerHTML = emptyState('暂无掉宝活动，稍后再来看看。', '📺');
      return;
    }
    if (!filtered.length) {
      list.innerHTML = emptyState('没有符合筛选条件的活动。', '🔍');
      return;
    }

    list.innerHTML = filtered.map(campaignCardHtml).join('');
    bindImgFallbacks(list);
  }

  function renderCampaignFilters() {
    const gameSelect = byId('campaign-game-filter');
    if (!gameSelect) return;

    const map = new Map();
    ((S.data && S.data.games) || []).forEach(function (game) {
      if (game && game.slug) map.set(game.slug, game.name || game.slug);
    });
    ((S.data && S.data.campaigns) || []).forEach(function (campaign) {
      const game = campaign.game || {};
      if (game.slug && !map.has(game.slug)) map.set(game.slug, game.name || game.slug);
    });

    const entries = Array.from(map.entries()).sort(function (a, b) {
      return String(a[1]).localeCompare(String(b[1]), 'zh-CN');
    });
    const signature = entries.map(function (entry) { return entry[0] + '::' + entry[1]; }).join('|');

    if (signature !== S.campaignFilterSig) {
      gameSelect.innerHTML = '<option value="">全部游戏</option>' + entries.map(function (entry) {
        return '<option value="' + esc(entry[0]) + '">' + esc(entry[1]) + '</option>';
      }).join('');
      S.campaignFilterSig = signature;
    }

    if (S.filters.campaignGame && !map.has(S.filters.campaignGame)) {
      S.filters.campaignGame = '';
    }
    gameSelect.value = S.filters.campaignGame || '';

    const scopeAll = byId('scope-all');
    const scopeMine = byId('scope-mine');
    if (scopeAll) {
      scopeAll.classList.toggle('is-active', !S.filters.campaignsMine);
      scopeAll.setAttribute('aria-pressed', String(!S.filters.campaignsMine));
    }
    if (scopeMine) {
      scopeMine.classList.toggle('is-active', S.filters.campaignsMine);
      scopeMine.setAttribute('aria-pressed', String(S.filters.campaignsMine));
    }
  }

  function campaignCardHtml(campaign) {
    const game = campaign.game || {};
    const drops = Array.isArray(campaign.drops) ? campaign.drops : [];
    const totalDrops = num(campaign.total_drops) || drops.length;
    const claimedDrops = campaign.claimed_drops !== undefined && campaign.claimed_drops !== null
      ? num(campaign.claimed_drops)
      : drops.filter(function (drop) { return drop.claimed; }).length;

    const art = game.box_art
      ? '<img class="box-art" src="' + safeUrl(game.box_art) + '" alt="" loading="lazy">'
      : '<div class="box-art placeholder" aria-hidden="true">🎮</div>';

    const isUpcoming = campaign.status === 'UPCOMING';
    const startLine = isUpcoming && campaign.starts_at
      ? '<p class="muted small">开始：' + esc(fmtDateTime(campaign.starts_at)) + '</p>'
      : '';

    const linkUrl = safeUrl(campaign.link_url);
    const linkHtml = linkUrl
      ? ' <a href="' + linkUrl + '" target="_blank" rel="noopener noreferrer">前往绑定 ↗</a>'
      : '';
    const warning = campaign.linked === false
      ? '<div class="warn-banner">⚠ 需先在 Twitch 绑定该游戏账号，否则无法获得奖励。' +
          linkHtml + '</div>'
      : '';

    const dropsHtml = drops.length
      ? drops.map(dropHtml).join('')
      : '<p class="muted small">暂无掉宝条目</p>';

    return '<article class="card campaign-card">' +
      '<div class="camp-head">' +
        art +
        '<div class="camp-info">' +
          '<h3 class="camp-name">' + esc(campaign.name || '未命名活动') + '</h3>' +
          '<p class="camp-game">' + esc(game.name || game.slug || '未知游戏') + '</p>' +
          '<div class="camp-meta">' +
            '<span class="badge badge-' + (isUpcoming ? 'upcoming' : 'active') + '">' +
              (isUpcoming ? '即将开始' : '进行中') + '</span>' +
            '<span class="countdown">' + esc(fmtCountdown(campaign.ends_at)) + '</span>' +
          '</div>' +
        '</div>' +
        '<div class="camp-claims" title="已领取 / 总掉宝">' + claimedDrops + '/' + totalDrops + '</div>' +
      '</div>' +
      startLine +
      warning +
      '<div class="drops">' + dropsHtml + '</div>' +
    '</article>';
  }

  function dropHtml(drop) {
    const current = Math.max(0, Math.round(num(drop.current_minutes)));
    const required = Math.max(0, Math.round(num(drop.required_minutes)));
    const percent = required > 0
      ? clampPercent(current, required)
      : (drop.claimed || drop.claimable ? 100 : 0);

    let statusText;
    let statusClass;
    if (drop.claimed) {
      statusText = '已领取 ✔';
      statusClass = 'ok';
    } else if (drop.claimable) {
      statusText = '可领取';
      statusClass = 'ready';
    } else if (required > 0) {
      statusText = '进行中 ' + current + '/' + required + ' 分钟';
      statusClass = 'progress';
    } else {
      statusText = '进行中';
      statusClass = 'progress';
    }

    const rewardNames = (drop.rewards || [])
      .map(function (reward) { return reward && reward.name; })
      .filter(Boolean)
      .join(' · ');
    const sub = rewardNames && rewardNames !== drop.name
      ? '<p class="drop-reward">奖励：' + esc(rewardNames) + '</p>'
      : '';

    return '<div class="drop-row">' +
      '<div class="drop-line">' +
        '<span class="drop-name">' + esc(drop.name || '掉宝奖励') + '</span>' +
        '<span class="drop-status ' + statusClass + '">' + statusText + '</span>' +
      '</div>' +
      sub +
      '<div class="bar" role="progressbar" aria-valuemin="0" aria-valuemax="' + (required || 100) +
        '" aria-valuenow="' + (required > 0 ? Math.min(current, required) : (drop.claimed || drop.claimable ? 100 : 0)) + '">' +
        '<span class="bar-fill' + (drop.claimed ? ' is-done' : '') + '" style="width:' + percent + '%"></span>' +
      '</div>' +
    '</div>';
  }

  /* ============================================================
   * Tab 3：日志
   * ============================================================ */
  function renderLogFilters() {
    const select = byId('logs-account-filter');
    if (!select) return;

    const accounts = (S.data && S.data.accounts) || [];
    const signature = accounts.map(function (account) {
      return account.id + ':' + (account.name || account.login || '');
    }).join('|');

    if (signature !== S.logs.filterSig) {
      select.innerHTML = '<option value="">全部账号</option>' + accounts.map(function (account) {
        return '<option value="' + esc(String(account.id)) + '">' +
          esc(account.name || account.login || ('账号 #' + account.id)) + '</option>';
      }).join('');
      S.logs.filterSig = signature;
    }

    const exists = accounts.some(function (account) {
      return String(account.id) === String(S.logs.accountId);
    });
    if (S.logs.accountId && !exists) S.logs.accountId = '';
    select.value = S.logs.accountId || '';
  }

  async function loadLogs(options) {
    const opts = options || {};
    if (S.logs.loading || !S.data) return;
    S.logs.loading = true;

    const list = byId('log-list');
    if (opts.showLoading && list) {
      list.innerHTML = '<p class="muted small log-empty">正在加载日志…</p>';
    }

    try {
      const params = new URLSearchParams({ limit: String(LOG_LIMIT) });
      if (S.logs.accountId) params.set('account_id', S.logs.accountId);
      const res = await api('/api/logs?' + params.toString());
      // 后端通常已倒序返回，这里再按时间倒序兜底，保证最新日志在最上方
      S.logs.list = (Array.isArray(res.logs) ? res.logs : []).slice().sort(function (a, b) {
        const ta = new Date(a && a.ts).getTime() || 0;
        const tb = new Date(b && b.ts).getTime() || 0;
        return tb - ta;
      });
      S.logs.lastFetch = Date.now();
      renderLogs();
    } catch (err) {
      if (err.status === 401) return;
      if (list) {
        list.innerHTML = '<p class="form-error log-empty">' + esc(err.message || '日志加载失败') + '</p>';
      }
    } finally {
      S.logs.loading = false;
    }
  }

  function renderLogs() {
    const list = byId('log-list');
    if (!list) return;
    const logs = S.logs.list;
    const sub = byId('logs-sub');
    if (sub) sub.textContent = logs.length ? '最近 ' + logs.length + ' 条' : '最近 200 条';

    if (!logs.length) {
      list.innerHTML = '<p class="muted small log-empty">暂无日志。</p>';
      return;
    }

    const rows = logs.map(function (log) {
      const level = String(log.level || 'info').toLowerCase();
      const levelClass = level === 'error' ? 'lv-error'
        : (level === 'warn' || level === 'warning') ? 'lv-warn' : 'lv-info';
      const account = log.account_id !== null && log.account_id !== undefined
        ? '#' + esc(String(log.account_id))
        : '-';
      return '<div class="log-row ' + levelClass + '">' +
        '<span class="log-time">' + esc(fmtLogTime(log.ts)) + '</span>' +
        '<span class="log-account">' + account + '</span>' +
        '<span class="log-level">' + esc(level.toUpperCase()) + '</span>' +
        '<span class="log-msg">' + esc(log.message || '') + '</span>' +
      '</div>';
    });

    const previousScroll = list.scrollTop;
    list.innerHTML = rows.join('');
    list.scrollTop = S.logs.autoScroll ? 0 : previousScroll;
  }

  /* ============================================================
   * Tab 4：设置（默认游戏多选 + 搜索）
   * ============================================================ */
  function isEditingSettings() {
    const view = byId('view-settings');
    if (!view) return false;
    const active = document.activeElement;
    return Boolean(active && active !== document.body && view.contains(active));
  }

  function gameMeta(slug) {
    if (!slug) return { slug: '', name: '', box_art: '' };
    const games = (S.data && S.data.games) || [];
    const found = games.find(function (game) { return game.slug === slug; });
    if (found) return found;
    const cached = S.gameCache.get(slug);
    if (cached) return cached;
    return { slug: slug, name: slug, box_art: '' };
  }

  function renderSettings() {
    if (!S.data) return;
    renderGamePool();
    renderSelectedGames();
    renderGameSearchResults();
    renderServerInfo();
  }

  function renderGamePool() {
    const pool = byId('game-pool');
    if (!pool) return;
    const games = (S.data && S.data.games) || [];

    if (!games.length) {
      pool.innerHTML = '<p class="muted small empty-inline">服务器暂未提供游戏列表，可直接在下方搜索添加。</p>';
      return;
    }

    pool.innerHTML = games.map(function (game) {
      const selected = S.defaultGames.indexOf(game.slug) >= 0;
      const art = game.box_art
        ? '<img class="chip-art" src="' + safeUrl(game.box_art) + '" alt="" loading="lazy">'
        : '<span class="chip-art placeholder" aria-hidden="true">🎮</span>';
      return '<button type="button" class="game-chip' + (selected ? ' is-selected' : '') +
        '" data-slug="' + esc(game.slug) + '" aria-pressed="' + selected + '">' +
        art +
        '<span class="chip-name">' + esc(game.name || game.slug) + '</span>' +
        '<span class="chip-check" aria-hidden="true">' + (selected ? '✓' : '+') + '</span>' +
      '</button>';
    }).join('');
    bindImgFallbacks(pool);

    pool.onclick = function (event) {
      const chip = event.target.closest('.game-chip');
      if (!chip) return;
      toggleDefaultGame(chip.dataset.slug);
    };
  }

  function renderSelectedGames() {
    const box = byId('game-tags');
    const line = byId('game-slug-line');
    if (!box || !line) return;

    line.textContent = S.defaultGames.length
      ? S.defaultGames.join(', ')
      : '（未选择，将扫描全部游戏）';

    if (!S.defaultGames.length) {
      box.innerHTML = '<p class="muted small empty-inline">尚未选择默认游戏</p>';
      return;
    }

    box.innerHTML = S.defaultGames.map(function (slug) {
      const meta = gameMeta(slug);
      const art = meta.box_art
        ? '<img class="tag-art" src="' + safeUrl(meta.box_art) + '" alt="" loading="lazy">'
        : '';
      return '<span class="tag">' + art +
        '<span>' + esc(meta.name || slug) + '</span>' +
        '<button type="button" class="tag-remove" data-slug="' + esc(slug) +
          '" aria-label="移除 ' + esc(meta.name || slug) + '">✕</button>' +
      '</span>';
    }).join('');
    bindImgFallbacks(box);

    box.onclick = function (event) {
      const btn = event.target.closest('.tag-remove');
      if (btn) toggleDefaultGame(btn.dataset.slug);
    };
  }

  function renderGameSearchResults() {
    const box = byId('game-search-results');
    if (!box) return;
    const results = S.gameSearch.results;

    if (!results.length) {
      box.innerHTML = S.gameSearch.lastQuery
        ? '<p class="muted small">没有找到与「' + esc(S.gameSearch.lastQuery) + '」匹配的游戏。</p>'
        : '';
      return;
    }

    box.innerHTML = results.map(function (game) {
      const selected = S.defaultGames.indexOf(game.slug) >= 0;
      const art = game.box_art
        ? '<img class="result-art" src="' + safeUrl(game.box_art) + '" alt="" loading="lazy">'
        : '<span class="result-art placeholder" aria-hidden="true">🎮</span>';
      return '<button type="button" class="search-result' + (selected ? ' is-selected' : '') +
        '" data-slug="' + esc(game.slug) + '">' +
        art +
        '<span class="result-name">' + esc(game.name || game.slug) + '</span>' +
        '<span class="result-slug mono">' + esc(game.slug) + '</span>' +
        '<span class="result-action">' + (selected ? '已添加' : '添加') + '</span>' +
      '</button>';
    }).join('');
    bindImgFallbacks(box);

    box.onclick = function (event) {
      const result = event.target.closest('.search-result');
      if (!result) return;
      const slug = result.dataset.slug;
      if (!slug || S.defaultGames.indexOf(slug) >= 0) return;
      const found = S.gameSearch.results.find(function (game) { return game.slug === slug; });
      if (found) S.gameCache.set(slug, found);
      toggleDefaultGame(slug);
    };
  }

  function toggleDefaultGame(slug) {
    if (!slug) return;
    const index = S.defaultGames.indexOf(slug);
    if (index >= 0) S.defaultGames.splice(index, 1);
    else S.defaultGames.push(slug);
    renderGamePool();
    renderSelectedGames();
    renderGameSearchResults();
  }

  function onGameSearchInput(event) {
    const q = event.target.value.trim();
    clearTimeout(S.gameSearch.timer);
    S.gameSearch.seq += 1; // 丢弃仍在飞行中的旧请求
    if (!q) {
      S.gameSearch.results = [];
      S.gameSearch.lastQuery = '';
      renderGameSearchResults();
      return;
    }
    S.gameSearch.timer = window.setTimeout(function () { runGameSearch(q); }, 350);
  }

  async function runGameSearch(q) {
    if (!q) return;
    clearTimeout(S.gameSearch.timer);
    const seq = ++S.gameSearch.seq;
    const box = byId('game-search-results');
    if (box) box.innerHTML = '<p class="muted small">搜索中…</p>';

    try {
      const res = await api('/api/games/search?q=' + encodeURIComponent(q));
      if (seq !== S.gameSearch.seq) return;
      const games = Array.isArray(res.games) ? res.games : [];
      games.forEach(function (game) {
        if (game && game.slug) S.gameCache.set(game.slug, game);
      });
      S.gameSearch.results = games;
      S.gameSearch.lastQuery = q;
      renderGameSearchResults();
    } catch (err) {
      if (seq !== S.gameSearch.seq || err.status === 401) return;
      if (box) {
        box.innerHTML = '<p class="form-error">' + esc(err.message || '搜索失败') + '</p>';
      }
    }
  }

  async function saveSettings(event) {
    const btn = event.currentTarget;
    await withBusy(btn, '保存中…', async function () {
      await api('/api/settings', {
        method: 'POST',
        body: { default_games: S.defaultGames.slice() }
      });
      if (S.data && S.data.settings) {
        S.data.settings.default_games = S.defaultGames.slice();
      }
      toast('设置已保存');
      renderSettings();
    });
  }

  function renderServerInfo() {
    const dl = byId('server-info');
    if (!dl) return;
    const data = S.data || {};
    const server = data.server || {};
    const settings = data.settings || {};

    const rows = [
      ['版本', server.version ? 'v' + server.version : '—'],
      ['运行时长', fmtUptime(server.uptime_s)],
      ['账号数', String(num(server.accounts))],
      ['正在挖宝', String(num(server.mining))],
      ['扫描间隔', settings.scan_interval_min ? num(settings.scan_interval_min) + ' 分钟' : '—']
    ];

    dl.innerHTML = rows.map(function (row) {
      return '<div class="kv-row"><dt>' + esc(row[0]) + '</dt><dd>' + esc(row[1]) + '</dd></div>';
    }).join('');
  }

  /* ============================================================
   * 登录 / 退出
   * ============================================================ */
  async function onLoginSubmit(event) {
    event.preventDefault();
    const input = byId('password-input');
    const btn = byId('login-btn');
    const password = input.value;

    setLoginError('');
    if (!password) {
      setLoginError('请输入密码');
      input.focus();
      return;
    }
    if (btn.disabled) return;

    btn.dataset.busyLabel = btn.textContent;
    btn.disabled = true;
    btn.textContent = '登录中…';

    try {
      await api('/api/login', { method: 'POST', body: { password: password }, raw401: true });

      S.settingsLoaded = false;
      S.defaultGames = [];
      S.logs.list = [];
      S.logs.lastFetch = 0;
      S.logs.accountId = '';

      const data = await api('/api/state', { raw401: true });
      hydrate(data, { force: true });
      enterApp();
      input.value = '';
      selectTab(tabFromHash());
      startPolling();
    } catch (err) {
      if (err.status === 401) setLoginError('密码错误');
      else setLoginError(err.message || '登录失败，请稍后重试');
      input.focus();
    } finally {
      if (btn.isConnected) {
        btn.disabled = false;
        if (btn.dataset.busyLabel !== undefined) {
          btn.textContent = btn.dataset.busyLabel;
          delete btn.dataset.busyLabel;
        }
      }
    }
  }

  async function onLogout() {
    const btn = byId('logout-btn');
    await withBusy(btn, '退出中…', async function () {
      try {
        await api('/api/logout', { method: 'POST' });
      } catch (err) {
        if (err.status !== 401) throw err;
      }
      S.data = null;
      S.settingsLoaded = false;
      S.defaultGames = [];
      S.logs.list = [];
      S.logs.lastFetch = 0;
      S.logs.filterSig = '';
      stopPolling();
      showLogin();
      toast('已退出登录');
    });
  }

  /* ============================================================
   * 事件绑定 / 启动
   * ============================================================ */
  function bindSettingsEvents() {
    const searchInput = byId('game-search');
    const searchBtn = byId('game-search-btn');
    const saveBtn = byId('save-settings');

    if (searchInput) {
      searchInput.addEventListener('input', onGameSearchInput);
      searchInput.addEventListener('keydown', function (event) {
        if (event.key === 'Enter') {
          event.preventDefault();
          const q = searchInput.value.trim();
          if (q) runGameSearch(q);
        }
      });
    }
    if (searchBtn) {
      searchBtn.addEventListener('click', function () {
        const q = byId('game-search').value.trim();
        if (!q) {
          S.gameSearch.results = [];
          S.gameSearch.lastQuery = '';
          renderGameSearchResults();
          return;
        }
        runGameSearch(q);
      });
    }
    if (saveBtn) saveBtn.addEventListener('click', saveSettings);
  }

  function bindStaticEvents() {
    byId('login-form').addEventListener('submit', onLoginSubmit);
    byId('password-input').addEventListener('input', function () { setLoginError(''); });
    byId('logout-btn').addEventListener('click', onLogout);
    byId('boot-retry').addEventListener('click', connect);
    byId('add-account-btn').addEventListener('click', openAddAccountModal);

    $$('.tab').forEach(function (btn) {
      btn.addEventListener('click', function () { goTab(btn.dataset.tab); });
    });

    byId('campaign-game-filter').addEventListener('change', function (event) {
      S.filters.campaignGame = event.target.value;
      renderCampaigns();
    });
    byId('scope-all').addEventListener('click', function () {
      S.filters.campaignsMine = false;
      renderCampaigns();
    });
    byId('scope-mine').addEventListener('click', function () {
      S.filters.campaignsMine = true;
      renderCampaigns();
    });

    byId('logs-refresh').addEventListener('click', function (event) {
      withBusy(event.currentTarget, '加载中…', function () {
        return loadLogs({ showLoading: true });
      });
    });
    byId('logs-account-filter').addEventListener('change', function (event) {
      S.logs.accountId = event.target.value;
      S.logs.lastFetch = 0;
      loadLogs({ showLoading: true });
    });
    byId('logs-autoscroll').addEventListener('change', function (event) {
      S.logs.autoScroll = event.target.checked;
      if (S.logs.autoScroll) byId('log-list').scrollTop = 0;
    });

    document.addEventListener('keydown', function (event) {
      if (event.key === 'Escape' && S.modal) closeModal();
    });
    document.addEventListener('visibilitychange', onVisibilityChange);
    window.addEventListener('hashchange', function () {
      if (S.authed) selectTab(tabFromHash());
    });

    bindOverviewEvents();
    bindSettingsEvents();
  }

  function boot() {
    bindStaticEvents();
    S.tab = tabFromHash();
    connect();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
