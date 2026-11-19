// ===========================================================================
// SecurePR Dashboard
// No page in this app asks the user to type an internal UUID. Repository,
// pull request, and review selection all happen through dropdowns/clicks;
// internal ids are kept as JS state (selectedX variables / dataset attrs),
// never as a text field the user has to fill in.
// ===========================================================================

const state = {
  repos: [],          // [{id, full_name, name, owner_login, pr_count}]
  selectedPrRepoId: null,
  selectedEvRepoId: null,
  selectedEvPrId: null,
  selectedEligRepoId: null,
};

const authState = { client: null, session: null };
let dashboardInitialized = false;
const frontendConfig = window.SECUREPR_CONFIG || {};
const API_BASE_URL = (frontendConfig.apiBaseUrl || document.getElementById('apiBase').value).replace(/\/$/, '');
document.getElementById('apiBase').value = API_BASE_URL;

function apiBase() { return API_BASE_URL; }

function showAuthGate(message = '') {
  document.getElementById('app').hidden = true;
  document.getElementById('authGate').hidden = false;
  document.getElementById('authMessage').textContent = message;
  document.getElementById('loginPassword').value = '';
}

function showDashboard() {
  document.getElementById('authGate').hidden = true;
  document.getElementById('app').hidden = false;
  document.getElementById('signedInUser').textContent =
    authState.session?.user?.email || 'Authenticated';
  if (!dashboardInitialized) {
    document.getElementById('cbScope').addEventListener('change', onCbScopeChange);
    dashboardInitialized = true;
  }
  loadOverview();
  handleInitialHash();
}

async function initializeSupabaseAuth() {
  const signInButton = document.getElementById('signInButton');
  if (!window.supabase?.createClient) {
    signInButton.disabled = true;
    showAuthGate('The Supabase Auth client could not be loaded. Check the network connection and reload.');
    return;
  }
  if (!frontendConfig.supabaseUrl || !frontendConfig.supabaseAnonKey) {
    signInButton.disabled = true;
    showAuthGate('Configure the Supabase URL and public anon key in dashboard/config.js.');
    return;
  }

  authState.client = window.supabase.createClient(
    frontendConfig.supabaseUrl,
    frontendConfig.supabaseAnonKey,
    { auth: { persistSession: true, autoRefreshToken: true, detectSessionInUrl: true } },
  );
  const { data, error } = await authState.client.auth.getSession();
  if (error) {
    showAuthGate('Could not restore the Supabase session. Please sign in again.');
    return;
  }
  authState.session = data.session;
  if (authState.session) showDashboard();
  else showAuthGate();
}

async function signOutAndShowLogin(message) {
  if (authState.client) {
    const { error } = await authState.client.auth.signOut();
    if (error) console.warn('Could not clear the local Supabase session.');
  }
  authState.session = null;
  showAuthGate(message);
}

async function fetchProtected(path, opts, accessToken) {
  const headers = new Headers(opts.headers || {});
  headers.set('Content-Type', 'application/json');
  headers.set('Authorization', `Bearer ${accessToken}`);
  return fetch(apiBase() + path, { ...opts, headers });
}

async function api(path, opts = {}) {
  if (!authState.client) {
    showAuthGate('Supabase authentication is not configured.');
    throw new Error('Supabase authentication is not configured.');
  }
  const { data: sessionData, error: sessionError } = await authState.client.auth.getSession();
  if (sessionError || !sessionData.session) {
    await signOutAndShowLogin('Your session expired. Please sign in again.');
    throw new Error('Authentication required.');
  }

  authState.session = sessionData.session;
  let res = await fetchProtected(path, opts, authState.session.access_token);
  if (res.status === 401) {
    const { data: refreshData, error: refreshError } = await authState.client.auth.refreshSession();
    if (refreshError || !refreshData.session) {
      await signOutAndShowLogin('Your session expired. Please sign in again.');
      throw new Error('Authentication required.');
    }
    authState.session = refreshData.session;
    res = await fetchProtected(path, opts, authState.session.access_token);
    if (res.status === 401) {
      await signOutAndShowLogin('Your session is no longer valid. Please sign in again.');
      throw new Error('Authentication required.');
    }
  }
  if (!res.ok) {
    let detail = '';
    try { detail = (await res.json()).detail || ''; } catch (e) { detail = await res.text().catch(() => ''); }
    toast('API error ' + res.status + (detail ? ': ' + detail : ''), true);
    throw new Error(detail || String(res.status));
  }
  return res.json();
}

function toast(msg, isError) {
  const el = document.createElement('div');
  el.className = 'toast';
  if (isError) el.style.borderColor = 'var(--red)';
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 4000);
}

function badge(value) { return value ? `<span class="badge ${escapeHtml(value)}">${escapeHtml(value.replace(/_/g, ' '))}</span>` : '—'; }
function ciBadge(ci) {
  if (!ci) return `<span class="badge unknown">CI status unknown</span>`;
  const map = {
    pending: '… In progress',
    passed: '✓ Passed',
    failed: '✕ Failed',
    neutral: '— CI neutral',
    no_checks: 'No checks discovered',
    unavailable: 'CI status unavailable',
    non_success: 'CI needs attention',
    unknown: 'CI status unknown',
  };
  const titles = {
    PASSED: 'All discovered executions passed.',
    FAILED: 'At least one discovered CI execution failed.',
    IN_PROGRESS: 'AI review is independent and published; CI monitoring continues.',
    NO_CHECKS_FOUND: 'No check runs or commit statuses were discovered for this revision.',
    NOT_CONFIGURED: 'No check runs or commit statuses were discovered for this revision.',
    UNAVAILABLE: ci.error || 'CI discovery is unavailable; bounded retries continue.',
    UNKNOWN: 'CI discovery is pending or returned an unrecognized status.',
    NON_SUCCESS: 'An execution ended without success (cancelled, neutral, skipped, or stale).',
  };
  return `<span class="badge ${escapeHtml(ci.label || 'unknown')}" title="${escapeHtml(titles[ci.state] || ci.state || '')}">${escapeHtml(map[ci.label] || 'CI status unknown')}</span>`;
}
function reviewStatusBadge(status) {
  const labels = {
    COMPLETED: 'AI review completed',
    PROCESSING: 'AI review processing',
    QUEUED: 'AI review queued',
    FAILED: 'AI review failed',
    NOT_STARTED: 'AI review not started',
  };
  return `<span class="badge">${escapeHtml(labels[status] || 'AI review status unknown')}</span>`;
}
function ciCheckResult(check) {
  if (check.status !== 'completed') return 'Running (' + (check.status || 'pending') + ')';
  const labels = {
    success: 'Successful',
    failure: 'Failed',
    error: 'Failed',
    cancelled: 'Cancelled',
    timed_out: 'Timed out',
    startup_failure: 'Startup failure',
    neutral: 'Neutral',
    skipped: 'Skipped',
    action_required: 'Action required',
    stale: 'Stale',
  };
  return labels[check.conclusion] || 'Unavailable';
}
function safeHttpUrl(value) {
  try {
    const url = new URL(value);
    return url.protocol === 'https:' ? url.href : null;
  } catch (e) {
    return null;
  }
}
function timeAgo(iso) {
  if (!iso) return '—';
  const d = new Date(iso + (iso.endsWith('Z') ? '' : 'Z'));
  const diffMs = Date.now() - d.getTime();
  const mins = Math.floor(diffMs / 60000);
  if (mins < 1) return 'just now';
  if (mins < 60) return `${mins} minute${mins === 1 ? '' : 's'} ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs} hour${hrs === 1 ? '' : 's'} ago`;
  const days = Math.floor(hrs / 24);
  return `${days} day${days === 1 ? '' : 's'} ago`;
}
function initials(login) {
  if (!login) return '?';
  return escapeHtml(login.slice(0, 2).toUpperCase());
}
function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

// ------------------------------------------------------------------- Nav ---
function switchTab(tabName) {
  document.querySelectorAll('.nav-link').forEach(b => b.classList.toggle('active', b.dataset.tab === tabName));
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.id === 'tab-' + tabName));
  if (tabName === 'overview') loadOverview();
  if (tabName === 'circuit-breaker') loadBreakers();
  if (tabName === 'impact-simulator') loadImpactReviewOptions();
  if (tabName === 'policies') loadPolicies();
}
document.querySelectorAll('.nav-link').forEach(btn => btn.addEventListener('click', () => switchTab(btn.dataset.tab)));

// ------------------------------------------------------------- Donut chart -
const PALETTE = { APPROVED: '#2fd480', BLOCKED: '#ff5c72', MANUAL_REVIEW: '#f6b33c',
                  PENDING: '#6d7891', LOW: '#2fd480', MEDIUM: '#f6b33c', HIGH: '#ff5c72', NOT_EVALUATED: '#6d7891' };

function renderDonut(containerId, data, centerLabel) {
  const el = document.getElementById(containerId);
  const entries = Object.entries(data).filter(([, v]) => v > 0);
  const total = entries.reduce((s, [, v]) => s + v, 0);
  if (!total) { el.innerHTML = '<div class="empty-state">No data yet.</div>'; return; }

  let gradientParts = [];
  let cursor = 0;
  entries.forEach(([key, val]) => {
    const pct = (val / total) * 100;
    const color = PALETTE[key] || '#4f8dfd';
    gradientParts.push(`${color} ${cursor}% ${cursor + pct}%`);
    cursor += pct;
  });

  const legendRows = entries.map(([key, val]) => `
    <div class="legend-row">
      <div class="legend-left"><span class="legend-swatch" style="background:${PALETTE[key] || '#4f8dfd'}"></span>${escapeHtml(key.replace(/_/g, ' '))}</div>
      <div class="legend-value">${val} <span style="color:var(--muted);font-weight:400;">(${Math.round(val / total * 100)}%)</span></div>
    </div>`).join('');

  el.innerHTML = `
    <div class="donut-row">
      <div class="donut" style="background: conic-gradient(${gradientParts.join(',')});">
        <div class="donut-center"><div class="n">${total}</div><div class="l">${escapeHtml(centerLabel)}</div></div>
      </div>
      <div class="legend">${legendRows}</div>
    </div>`;
}

function renderLineChart(containerId, points) {
  const el = document.getElementById(containerId);
  if (!points || !points.length) { el.innerHTML = '<div class="empty-state">No data yet.</div>'; return; }
  const w = 320, h = 130, pad = 18;
  const max = Math.max(1, ...points.map(p => p.count));
  const stepX = (w - pad * 2) / Math.max(1, points.length - 1);
  const coords = points.map((p, i) => {
    const x = pad + i * stepX;
    const y = h - pad - (p.count / max) * (h - pad * 2);
    return [x, y];
  });
  const linePath = coords.map((c, i) => (i === 0 ? 'M' : 'L') + c[0].toFixed(1) + ',' + c[1].toFixed(1)).join(' ');
  const areaPath = linePath + ` L${coords[coords.length - 1][0]},${h - pad} L${coords[0][0]},${h - pad} Z`;
  const dots = coords.map((c, i) =>
    `<circle cx="${c[0].toFixed(1)}" cy="${c[1].toFixed(1)}" r="3" fill="#4f8dfd"><title>${escapeHtml(points[i].date)}: ${points[i].count}</title></circle>`
  ).join('');
  const firstLabel = points[0].date.slice(5);
  const lastLabel = points[points.length - 1].date.slice(5);

  el.innerHTML = `
    <svg viewBox="0 0 ${w} ${h + 16}" width="100%" height="150">
      <defs><linearGradient id="trendFill" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0%" stop-color="#4f8dfd" stop-opacity="0.35"/>
        <stop offset="100%" stop-color="#4f8dfd" stop-opacity="0"/>
      </linearGradient></defs>
      <path d="${areaPath}" fill="url(#trendFill)" stroke="none"/>
      <path d="${linePath}" fill="none" stroke="#4f8dfd" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/>
      ${dots}
      <text x="${pad}" y="${h + 14}" font-size="10" fill="#6d7891">${escapeHtml(firstLabel)}</text>
      <text x="${w - pad}" y="${h + 14}" font-size="10" fill="#6d7891" text-anchor="end">${escapeHtml(lastLabel)}</text>
    </svg>`;
}

// -------------------------------------------------------- Repo dropdowns ---
async function loadRepositories() {
  state.repos = await api('/api/repositories');
  const selects = ['overviewRepoFilter', 'prRepoSelect', 'cbRepoSelect', 'evRepoSelect', 'eligRepoSelect'];
  selects.forEach(id => {
    const sel = document.getElementById(id);
    if (!sel) return;
    const keepFirst = id === 'overviewRepoFilter' || id === 'prRepoSelect' || id === 'evRepoSelect' || id === 'eligRepoSelect';
    const firstOption = sel.querySelector('option');
    sel.innerHTML = '';
    if (keepFirst && firstOption) sel.appendChild(firstOption);
    state.repos.forEach(r => {
      const opt = document.createElement('option');
      opt.value = r.id;
      opt.textContent = r.full_name;
      sel.appendChild(opt);
    });
  });
  return state.repos;
}

// -------------------------------------------------------------- Overview --
async function loadOverview() {
  await loadRepositories();
  try {
    const data = await api('/api/overview');
    const approved = data.by_decision.APPROVED || 0;
    const blocked = data.by_decision.BLOCKED || 0;
    const manual = data.by_decision.MANUAL_REVIEW || 0;
    document.getElementById('overviewCards').innerHTML = `
      <div class="kpi-card">
        <div class="kpi-top"><div class="kpi-icon" style="background:var(--accent-soft);color:var(--accent);">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="3"/><path d="M8 12h8M8 16h5M8 8h3"/></svg>
        </div></div>
        <div class="kpi-value">${data.total_reviews}</div>
        <div class="kpi-label">Total PRs Analyzed</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-top"><div class="kpi-icon" style="background:var(--green-soft);color:var(--green);">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 6L9 17l-5-5"/></svg>
        </div></div>
        <div class="kpi-value">${approved}</div>
        <div class="kpi-label">Approved</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-top"><div class="kpi-icon" style="background:var(--red-soft);color:var(--red);">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M15 9l-6 6M9 9l6 6"/></svg>
        </div></div>
        <div class="kpi-value">${blocked}</div>
        <div class="kpi-label">Blocked</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-top"><div class="kpi-icon" style="background:var(--yellow-soft);color:var(--yellow);">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 3"/></svg>
        </div></div>
        <div class="kpi-value">${manual}</div>
        <div class="kpi-label">Pending Review</div>
      </div>
    `;

    renderDonut('decisionDonut', { APPROVED: approved, BLOCKED: blocked, MANUAL_REVIEW: manual }, 'PRs');
    renderDonut('riskDonut', {
      LOW: data.by_risk_category.LOW || 0, MEDIUM: data.by_risk_category.MEDIUM || 0,
      HIGH: data.by_risk_category.HIGH || 0,
    }, 'PRs');

    document.getElementById('repoListWrap').innerHTML = data.repositories.length ? `
      <table><tr><th>Repository</th><th>Status</th><th>PRs Analyzed</th><th>Last Analysis</th></tr>
        ${data.repositories.map(r => `<tr>
          <td><span class="avatar-chip"><span class="avatar-circle">${initials(r.owner_login)}</span>${escapeHtml(r.full_name)}</span></td>
          <td><span class="badge APPROVED">Active</span></td>
          <td>${r.pr_count}</td>
          <td>${timeAgo(r.last_analysis_at)}</td>
        </tr>`).join('')}
      </table>` : `<div class="empty-state">No repositories connected yet. Install the SecurePR GitHub App to get started — see docs/SETUP_GUIDE.md.</div>`;
  } catch (e) { console.error(e); }

  try {
    const trend = await api('/api/overview/trend?days=7');
    renderLineChart('trendChart', trend);
  } catch (e) { console.error(e); }

  try {
    const recent = await api('/api/pull-requests/recent?limit=8');
    document.getElementById('recentPrTableWrap').innerHTML = recent.length ? `
      <table><tr><th>#</th><th>Title</th><th>Repository</th><th>Author</th><th>Risk Score</th><th>AI Review</th><th>Decision</th><th>CI Status</th><th>Updated</th><th></th></tr>
        ${recent.map(p => `<tr class="clickable" onclick="jumpToPrFromOverview('${p.repository_id}','${p.id}')">
          <td>#${p.number}</td>
          <td>${escapeHtml(p.title || '')}</td>
          <td><span class="avatar-chip"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="color:var(--muted)"><circle cx="6" cy="6" r="2.5"/><circle cx="6" cy="18" r="2.5"/><circle cx="18" cy="9" r="2.5"/></svg>${escapeHtml(p.repository_full_name || '')}</span></td>
          <td><span class="avatar-chip"><span class="avatar-circle">${initials(p.author)}</span>${escapeHtml(p.author || '')}</span></td>
          <td>${p.latest_score != null ? '<span class="badge ' + (p.latest_score < 40 ? 'LOW' : p.latest_score < 70 ? 'MEDIUM' : 'HIGH') + '">' + p.latest_score.toFixed(2) + '</span>' : '—'}</td>
          <td>${reviewStatusBadge(p.review_status)}</td>
          <td>${badge(p.latest_decision)}</td>
          <td>${ciBadge(p.ci_status)}</td>
          <td>${timeAgo(p.updated_at)}</td>
          <td><button class="btn secondary" style="padding:5px 10px;">View →</button></td>
        </tr>`).join('')}
      </table>` : `<div class="empty-state">No pull requests analyzed yet.</div>`;
  } catch (e) { console.error(e); }
}

function jumpToPrFromOverview(repoId, prId) {
  switchTab('pull-requests');
  const sel = document.getElementById('prRepoSelect');
  sel.value = repoId;
  onPrRepoChange().then(() => showPrDetail(prId));
}

// ---------------------------------------------------------- Repos & PRs ---
async function onPrRepoChange() {
  const repoId = document.getElementById('prRepoSelect').value;
  state.selectedPrRepoId = repoId;
  document.getElementById('prDetailPanel').style.display = 'none';
  if (!repoId) {
    document.getElementById('prListPanel').innerHTML = '<div class="empty-state">Select a repository above to load its pull requests.</div>';
    return;
  }
  document.getElementById('prListPanel').innerHTML = '<div class="empty-state">Loading…</div>';
  const prs = await api(`/api/repositories/${repoId}/pull-requests`);
  document.getElementById('prListPanel').innerHTML = prs.length ? `
    <table><tr><th>#</th><th>Title</th><th>Author</th><th>Branch</th><th>Risk</th><th>AI Review</th><th>Decision</th><th>CI</th><th>Files</th><th>Findings</th><th>Updated</th></tr>
      ${prs.map(p => `<tr class="clickable" onclick="showPrDetail('${p.id}')">
        <td>#${p.number}</td><td>${escapeHtml(p.title || '')}</td>
        <td><span class="avatar-chip"><span class="avatar-circle">${initials(p.author)}</span>${escapeHtml(p.author || '')}</span></td>
        <td><code>${escapeHtml(p.head_branch || '')} → ${escapeHtml(p.base_branch || '')}</code></td>
        <td>${p.latest_score != null ? p.latest_score.toFixed(1) : '—'}</td>
        <td>${reviewStatusBadge(p.review_status)}</td>
        <td>${badge(p.latest_decision)}</td>
        <td>${ciBadge(p.ci_status)}</td>
        <td>${p.changed_files_count}</td>
        <td>${p.findings_count}</td>
        <td>${timeAgo(p.updated_at)}</td>
      </tr>`).join('')}
    </table>` : `<div class="empty-state">No pull requests found for this repository yet.</div>`;
}

async function showPrDetail(prId) {
  const panel = document.getElementById('prDetailPanel');
  panel.style.display = 'block';
  panel.innerHTML = '<div class="empty-state">Loading…</div>';
  const pr = await api(`/api/pull-requests/${prId}`);
  const reviews = await api(`/api/pull-requests/${prId}/reviews`);
  const latest = reviews.find(r => !r.is_ci_wait_placeholder) || reviews[0];
  panel.innerHTML = `
    <div class="panel-header">
      <div>
        <h2 class="section-title">#${pr.number} — ${escapeHtml(pr.title || '')}</h2>
        <p class="section-sub">${escapeHtml(pr.repository ? pr.repository.full_name : '')} · opened by ${escapeHtml(pr.author || '')} · ${reviews.length} review(s) on record</p>
      </div>
      ${reviewStatusBadge(pr.review_status)}
    </div>
    ${latest ? `
      <p>Latest risk score: <strong>${latest.overall_score}</strong> (${escapeHtml(latest.risk_category)}) — ${badge(latest.decision)}</p>
    ` : '<div class="empty-state">No completed review yet.</div>'}
    <button class="btn" onclick="jumpToEvidence('${pr.repository.id}','${prId}')">View Full Evidence &amp; Replay →</button>
  `;
}

function jumpToEvidence(repoId, prId) {
  switchTab('evidence-replay');
  document.getElementById('evRepoSelect').value = repoId;
  onEvRepoChange().then(() => {
    document.getElementById('evPrSelect').value = prId;
    onEvPrChange();
  });
}

// --------------------------------------------------------- Circuit Breaker
function onCbScopeChange() {
  const scope = document.getElementById('cbScope').value;
  document.getElementById('cbRepoWrap').style.display = scope === 'REPOSITORY' ? 'block' : 'none';
  document.getElementById('cbUserWrap').style.display = scope === 'USER' ? 'block' : 'none';
}

async function loadCbUserDatalist() {
  const list = document.getElementById('cbUserList');
  list.innerHTML = '';
  const seen = new Set();
  for (const r of state.repos.slice(0, 5)) {
    try {
      const authors = await api(`/api/repositories/${r.id}/authors`);
      authors.forEach(a => {
        if (!seen.has(a.login)) {
          seen.add(a.login);
          const opt = document.createElement('option');
          opt.value = a.login;
          list.appendChild(opt);
        }
      });
    } catch (e) { /* ignore */ }
  }
}

async function loadBreakers() {
  await loadRepositories();
  loadCbUserDatalist();
  const cbs = await api('/api/circuit-breakers');
  const el = document.getElementById('cbCardsWrap');
  if (!cbs.length) { el.innerHTML = '<div class="empty-state">No circuit breakers configured yet.</div>'; return; }
  el.innerHTML = cbs.map(c => `
    <div class="panel" style="margin-bottom:10px;">
      <div class="panel-header">
        <div>
          <div style="font-weight:700;font-size:14px;">${escapeHtml(c.scope)}${c.repository_full_name ? ' — ' + escapeHtml(c.repository_full_name) : ''}${c.user_login ? ' — ' + escapeHtml(c.user_login) : ''}</div>
          <div class="section-sub" style="margin-top:4px;">${escapeHtml(c.reason || 'No reason given')}</div>
        </div>
        ${c.is_tripped ? '<span class="badge BLOCKED">🔴 Tripped</span>' : '<span class="badge APPROVED">🟢 Inactive</span>'}
      </div>
      <div class="section-sub">
        ${c.tripped_at ? 'Tripped ' + timeAgo(c.tripped_at) + (c.tripped_by ? ' by ' + escapeHtml(c.tripped_by) : '') : ''}
        ${c.auto_resume_at ? ' · auto-resumes ' + new Date(c.auto_resume_at).toLocaleString() : ''}
      </div>
      ${c.is_tripped ? `<button class="btn secondary" style="margin-top:10px;" data-resume-circuit-breaker data-resume-scope="${escapeHtml(c.scope)}" data-resume-repository-id="${escapeHtml(c.repository_id || '')}" data-resume-user-login="${escapeHtml(c.user_login || '')}">Resume</button>` : ''}
    </div>
  `).join('');
  el.querySelectorAll('[data-resume-circuit-breaker]').forEach(button => {
    button.addEventListener('click', () => resumeSpecific(
      button.dataset.resumeScope,
      button.dataset.resumeRepositoryId,
      button.dataset.resumeUserLogin,
    ));
  });
}

async function tripBreaker() {
  const scope = document.getElementById('cbScope').value;
  const reason = document.getElementById('cbReason').value.trim();
  if (!reason) { toast('Please give a reason for tripping the breaker.', true); return; }
  if (scope !== 'GLOBAL' && !confirm(`Trip the ${scope} circuit breaker? Automatic approval will be suspended until it's resumed.`)) return;
  await api('/api/circuit-breakers/trip', {
    method: 'POST', body: JSON.stringify({
      scope, repository_id: scope === 'REPOSITORY' ? document.getElementById('cbRepoSelect').value : null,
      user_login: scope === 'USER' ? document.getElementById('cbUserLogin').value.trim() : null,
      reason, suspend_minutes: Number(document.getElementById('cbSuspendMinutes').value) || null,
    }),
  });
  toast('Circuit breaker tripped.');
  loadBreakers();
}

async function resumeSpecific(scope, repositoryId, userLogin) {
  await api('/api/circuit-breakers/resume', {
    method: 'POST', body: JSON.stringify({ scope, repository_id: repositoryId || null, user_login: userLogin || null }),
  });
  toast('Circuit breaker resumed.');
  loadBreakers();
}

// ----------------------------------------------------- Evidence & Replay --
async function onEvRepoChange() {
  const repoId = document.getElementById('evRepoSelect').value;
  state.selectedEvRepoId = repoId;
  const prSel = document.getElementById('evPrSelect');
  prSel.innerHTML = '<option value="">Loading…</option>';
  document.getElementById('reviewHistoryPanel').innerHTML = '<div class="empty-state">Select a pull request above.</div>';
  if (!repoId) { prSel.innerHTML = '<option value="">Select a repository first…</option>'; return; }
  const prs = await api(`/api/repositories/${repoId}/pull-requests`);
  prSel.innerHTML = '<option value="">Select a pull request…</option>' +
    prs.map(p => `<option value="${p.id}">#${p.number} — ${escapeHtml(p.title || '').slice(0, 60)}</option>`).join('');
}

async function onEvPrChange() {
  const prId = document.getElementById('evPrSelect').value;
  state.selectedEvPrId = prId;
  if (!prId) { document.getElementById('reviewHistoryPanel').innerHTML = '<div class="empty-state">Select a pull request above.</div>'; return; }
  await renderReviewHistory(prId);
}

async function renderReviewHistory(prId) {
  const panel = document.getElementById('reviewHistoryPanel');
  panel.innerHTML = '<div class="empty-state">Loading…</div>';
  const reviews = await api(`/api/pull-requests/${prId}/reviews`);
  if (!reviews.length) { panel.innerHTML = '<div class="empty-state">No reviews yet for this pull request.</div>'; return; }
  panel.innerHTML = reviews.map((r, idx) => `
    <div class="accordion-item">
      <div class="accordion-head" onclick="toggleAccordion('rev-${r.id}', '${r.id}')">
        <div>
          <strong>${r.is_reevaluation ? 'Re-evaluation' : 'Initial Review'}</strong>
          <span class="section-sub">&nbsp;·&nbsp;${new Date(r.created_at).toLocaleString()}</span>
        </div>
        <div style="display:flex;align-items:center;gap:10px;">
          ${r.overall_score ? '<span>' + r.overall_score.toFixed(1) + '/100</span>' : ''}
          ${badge(r.decision)}
          ${r.github_action_confirmed ? '<span title="GitHub confirmed">✅</span>' : ''}
        </div>
      </div>
      <div class="accordion-body" id="rev-${r.id}"></div>
    </div>
  `).join('');
}

const _loadedAccordions = new Set();
async function toggleAccordion(domId, reviewId) {
  const body = document.getElementById(domId);
  const isOpen = body.classList.contains('open');
  document.querySelectorAll('.accordion-body.open').forEach(b => b.classList.remove('open'));
  if (isOpen) return;
  body.classList.add('open');
  if (_loadedAccordions.has(reviewId)) return;
  body.innerHTML = '<div class="empty-state">Loading evidence…</div>';
  const d = await api(`/api/reviews/${reviewId}`);
  _loadedAccordions.add(reviewId);
  body.innerHTML = `
    <p><strong>Why this decision:</strong></p>
    <ul class="reason-list">${(d.decision_reasons || []).map(r => `<li>${escapeHtml(r)}</li>`).join('') || '<li>—</li>'}</ul>

    ${!d.is_ci_wait_placeholder ? `
    <table style="margin:10px 0;"><tr><th>Dimension</th><th>Score</th></tr>
      ${Object.entries(d.scores).filter(([k]) => k !== 'overall').map(([k, v]) => `<tr><td>${k.replace(/_/g, ' ')}</td><td>${v}</td></tr>`).join('')}
    </table>` : ''}

    ${d.ci_status ? `
    <details style="margin:10px 0;">
      <summary><strong>CI Verification</strong> — ${ciBadge(d.ci_status)}</summary>
      <div class="section-sub" style="margin:8px 0;">Revision: <code>${escapeHtml(d.ci_status.sha || d.head_sha || '')}</code></div>
      ${[...(d.ci_status.checks || []), ...(d.ci_status.check_suites || [])].length ? `
        <table><tr><th>Check</th><th>Provider</th><th>Status</th><th></th></tr>
          ${[...(d.ci_status.checks || []), ...(d.ci_status.check_suites || [])].map(c => {
            const url = safeHttpUrl(c.url || c.html_url);
            return `<tr><td>${escapeHtml(c.name || 'Unnamed check')}</td><td>${escapeHtml(c.provider || c.kind || 'GitHub')}</td><td>${escapeHtml(ciCheckResult(c))}</td><td>${url ? `<a href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer">Details</a>` : '—'}</td></tr>`;
          }).join('')}
        </table>` : '<div class="empty-state">No individual CI checks were discovered.</div>'}
    </details>` : ''}

    ${d.findings.length ? `<p><strong>Findings (${d.findings.length})</strong></p>
      ${d.findings.map(f => `<div style="margin-bottom:6px;">${badge(f.severity)} <strong>${escapeHtml(f.title)}</strong> ${f.file_path ? '<code>' + escapeHtml(f.file_path + (f.line_reference ? ':' + f.line_reference : '')) + '</code>' : ''}<br><span class="section-sub">${escapeHtml(f.description || '')}</span></div>`).join('')}` : ''}

    ${d.changed_files.length ? `<p><strong>Changed Files (${d.changed_files.length})</strong></p>
      <div style="max-height:160px;overflow-y:auto;">${d.changed_files.map(f => `<div class="section-sub">${f.is_blocked_path ? '🚫 ' : ''}<code>${escapeHtml(f.path)}</code> (+${f.additions}/-${f.deletions})</div>`).join('')}</div>` : ''}

    ${d.impact_predictions.length ? `<p><strong>Change Impact (${d.impact_predictions.length})</strong></p>
      <table><tr><th>Component</th><th>Type</th><th>Confidence</th><th>Consequence</th></tr>
        ${d.impact_predictions.map(i => `<tr><td><code>${escapeHtml(i.name)}</code></td><td>${escapeHtml(i.type)}</td><td>${badge(i.confidence)}</td><td>${escapeHtml(i.consequence || '')}</td></tr>`).join('')}
      </table>` : ''}

    ${d.github_actions.length ? `<p><strong>GitHub Actions</strong></p>
      ${d.github_actions.map(a => `<div class="section-sub">${a.succeeded ? '✅' : '❌'} ${escapeHtml(a.type)} (status ${escapeHtml(a.status || '—')})</div>`).join('')}` : ''}

    ${d.policy ? `<p class="section-sub" style="margin-top:10px;">Evaluated against policy <strong>${escapeHtml(d.policy.name)} v${d.policy.version}</strong></p>` : ''}
  `;
}

async function reevaluatePR() {
  if (!state.selectedEvPrId) { toast('Select a pull request first.', true); return; }
  if (!confirm('Re-run analysis for this pull request? Prior review history will be kept, not overwritten.')) return;
  await api('/api/pull-requests/reevaluate', { method: 'POST', body: JSON.stringify({ pull_request_id: state.selectedEvPrId }) });
  toast('Re-evaluation complete.');
  renderReviewHistory(state.selectedEvPrId);
}

// ------------------------------------------------------ Impact Simulator --
async function loadImpactReviewOptions() {
  const sel = document.getElementById('impactReviewSelect');
  sel.innerHTML = '<option value="">Loading…</option>';
  const reviews = await api('/api/reviews/recent?limit=20');
  if (!reviews.length) { sel.innerHTML = '<option value="">No reviews yet</option>'; return; }
  sel.innerHTML = '<option value="">Select a review…</option>' +
    reviews.map(r => `<option value="${r.review_id}">#${r.pr_number} ${escapeHtml((r.pr_title || '').slice(0, 40))} — ${escapeHtml(r.repository_full_name || '')} (${escapeHtml(r.decision)}, ${r.impact_count} components)</option>`).join('');
}

async function onImpactReviewChange() {
  const reviewId = document.getElementById('impactReviewSelect').value;
  const panel = document.getElementById('impactPanel');
  if (!reviewId) { panel.innerHTML = '<div class="empty-state">Select a review above.</div>'; return; }
  panel.innerHTML = '<div class="empty-state">Loading…</div>';
  const items = await api(`/api/reviews/${reviewId}/impact`);
  panel.innerHTML = items.length ? `
    <table><tr><th>Component</th><th>Type</th><th>Confidence</th><th>Evidence</th><th>Potential Consequence</th></tr>
      ${items.map(i => `<tr><td><code>${escapeHtml(i.name)}</code></td><td>${escapeHtml(i.type)}</td><td>${badge(i.confidence)}</td><td>${escapeHtml(i.evidence || '')}</td><td>${escapeHtml(i.consequence || '')}</td></tr>`).join('')}
    </table>` : '<div class="empty-state">No impact predictions recorded for this review.</div>';
}

// --------------------------------------------------------- Approval Config
async function onEligRepoChange() {
  const repoId = document.getElementById('eligRepoSelect').value;
  state.selectedEligRepoId = repoId;
  const userSel = document.getElementById('eligUserSelect');
  if (!repoId) {
    userSel.innerHTML = '<option value="">Select a repository first…</option>';
    document.getElementById('eligPanel').innerHTML = '<div class="empty-state">Select a repository above to see configured users.</div>';
    return;
  }
  const authors = await api(`/api/repositories/${repoId}/authors`);
  userSel.innerHTML = '<option value="">Select a known author…</option>' +
    authors.map(a => `<option value="${escapeHtml(a.login)}">${escapeHtml(a.login)}${a.is_enabled ? ' (enabled)' : ''}</option>`).join('');
  loadEligibility();
}

async function loadEligibility() {
  const repoId = state.selectedEligRepoId;
  if (!repoId) return;
  const rows = await api(`/api/repositories/${repoId}/eligibility`);
  document.getElementById('eligPanel').innerHTML = rows.length ? `
    <table><tr><th>User</th><th>Status</th><th>Max Risk Threshold</th><th>Updated</th></tr>
      ${rows.map(r => `<tr>
        <td><span class="avatar-chip"><span class="avatar-circle">${initials(r.user_login)}</span>${escapeHtml(r.user_login || '')}</span></td>
        <td>${r.is_enabled ? '<span class="badge APPROVED">Eligible</span>' : '<span class="badge PENDING">Not eligible</span>'}</td>
        <td>${r.max_risk_threshold}</td>
        <td>${timeAgo(r.updated_at)}</td>
      </tr>`).join('')}
    </table>` : '<div class="empty-state">No users configured for this repository yet.</div>';
}

async function setEligibility() {
  const repoId = state.selectedEligRepoId;
  if (!repoId) { toast('Select a repository first.', true); return; }
  const manual = document.getElementById('eligUserManual').value.trim();
  const picked = document.getElementById('eligUserSelect').value;
  const login = manual || picked;
  if (!login) { toast('Select a known author or type a username.', true); return; }
  await api('/api/repositories/eligibility', {
    method: 'POST', body: JSON.stringify({
      repository_id: repoId, user_login: login,
      is_enabled: document.getElementById('eligEnabled').checked,
      max_risk_threshold: Number(document.getElementById('eligThreshold').value) || 39,
    }),
  });
  toast(`Saved eligibility for ${login}.`);
  document.getElementById('eligUserManual').value = '';
  onEligRepoChange();
}

// ------------------------------------------------------------- Policies ---
async function loadPolicies() {
  const panel = document.getElementById('policiesPanel');
  panel.innerHTML = '<div class="empty-state">Loading…</div>';
  const policies = await api('/api/policies');
  panel.innerHTML = policies.map(p => `
    <div class="panel" style="margin-bottom:10px;">
      <div class="panel-header">
        <div>
          <h2 class="section-title">${escapeHtml(p.name)} <span class="section-sub">v${p.version}</span></h2>
          <p class="section-sub">Scope: ${escapeHtml(p.scope)}</p>
        </div>
        ${p.is_active ? '<span class="badge APPROVED">Active</span>' : '<span class="badge PENDING">Inactive</span>'}
      </div>
      <table>
        <tr><td style="width:220px;color:var(--muted);">Auto-approval threshold</td><td>${p.content.auto_approval_risk_threshold ?? '—'}</td></tr>
        <tr><td style="color:var(--muted);">Mandatory CI checks</td><td>${(p.content.mandatory_ci_checks || []).map(c => `<code>${escapeHtml(c)}</code>`).join(' ')}</td></tr>
        <tr><td style="color:var(--muted);">Blocked paths</td><td>${(p.content.blocked_paths || []).map(c => `<code>${escapeHtml(c)}</code>`).join(' ')}</td></tr>
        <tr><td style="color:var(--muted);">Human-review-required paths</td><td>${(p.content.human_review_required_paths || []).map(c => `<code>${escapeHtml(c)}</code>`).join(' ')}</td></tr>
        <tr><td style="color:var(--muted);">Max files for auto-approval</td><td>${p.content.max_files_for_auto_approval ?? '—'}</td></tr>
      </table>
    </div>
  `).join('') || '<div class="empty-state">No policies found.</div>';
}

// --------------------------------------------------------------- Hash nav -
async function handleInitialHash() {
  const m = location.hash.match(/^#\/pr\/([a-f0-9-]+)/i);
  if (!m) return;
  try {
    const pr = await api(`/api/pull-requests/${m[1]}`);
    if (pr && pr.repository) {
      await loadRepositories();
      jumpToPrFromOverview(pr.repository.id, pr.id);
    }
  } catch (e) { /* stale/missing id in link, ignore */ }
}

// --------------------------------------------------------------- Init -----
document.getElementById('loginForm').addEventListener('submit', async event => {
  event.preventDefault();
  const button = document.getElementById('signInButton');
  button.disabled = true;
  document.getElementById('authMessage').textContent = '';
  try {
    const { data, error } = await authState.client.auth.signInWithPassword({
      email: document.getElementById('loginEmail').value.trim(),
      password: document.getElementById('loginPassword').value,
    });
    if (error) {
      showAuthGate(error.message);
      return;
    }
    authState.session = data.session;
    if (!authState.session) {
      showAuthGate('Sign-in did not return an authenticated session.');
      return;
    }
    showDashboard();
  } catch (error) {
    showAuthGate('The sign-in request could not be completed. Check your connection and try again.');
  } finally {
    button.disabled = false;
  }
});
document.getElementById('signOutButton').addEventListener('click', async () => {
  await signOutAndShowLogin('You have signed out.');
});
initializeSupabaseAuth().catch(() => {
  showAuthGate('Supabase authentication could not be initialized. Check the configuration and reload.');
});
