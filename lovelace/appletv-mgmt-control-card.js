/**
 * appletv-mgmt-control-card
 *
 * The parent's daily-driver dashboard card for the appletv_mgmt integration.
 * Consolidates per-group budgets, adult-mode toggle with countdown, pending
 * extension requests with inline Approve/Deny, and quick actions onto one
 * card.
 *
 * Reads from: sensor.<…>_enforcement_state, sensor.<…>_current_app, the 8
 * per-group sensors, switch.<…>_adult_mode.
 *
 * Writes via:
 *   - hass.callService for adult_mode, force_block, grant_extension, reset_usage
 *   - fetch() to the REST API for pending-request approve/deny
 *
 * Usage in a dashboard:
 *
 *   type: custom:appletv-mgmt-control-card
 *   profile_id: 01KRV4J2V4W6K0XMN1C01G6ENX
 *   # OR (less specific, picks the first profile)
 *   entity_prefix: sensor.apple_tv_mgmt_living_room_  # everything before "enforcement_state"
 *
 * Single-file vanilla Lit (CDN); no build step. Register as a JS module
 * resource:
 *   /local/community/appletv-mgmt-control-card/appletv-mgmt-control-card.js
 */

// Import Lit as an ES module. HA serves this file with `res_type: "module"`,
// so native `import` works. We pin to lit-element v4 (Lit 3 era) — matches
// what HA's frontend ships. Pulling from unpkg is the HACS-recommended
// pattern for community cards; the file gets browser-cached after first
// load so there's no per-render network cost.
import {
  LitElement,
  html,
  css,
} from 'https://unpkg.com/lit-element@4.1.1/lit-element.js?module';

const GROUPS = ['movies', 'tv_shows', 'gaming', 'other'];
const GROUP_LABELS = { movies: 'Movies', tv_shows: 'TV Shows', gaming: 'Gaming', other: 'Other' };

class AppleTVMgmtControlCard extends LitElement {
  static get properties() {
    return { hass: { type: Object }, config: { type: Object }, _requests: { state: true } };
  }

  constructor() {
    super();
    this._requests = [];
    this._lastFetchAt = 0;
  }

  setConfig(config) {
    this.config = {
      title: 'Apple TV',
      poll_requests_sec: 15,
      ...(config || {}),
    };
    if (!this.config.profile_id && !this.config.entity_prefix) {
      throw new Error('Provide either profile_id or entity_prefix');
    }
  }

  getCardSize() {
    return 10;
  }

  // ---------- entity discovery ----------

  _prefix() {
    return this.config.entity_prefix || null;
  }

  _entity(suffix) {
    // Find a sensor whose unique_id ends with `_<suffix>` and whose state is for this profile.
    const states = this.hass?.states || {};
    if (this.config.entity_prefix) {
      const eid = `${this.config.entity_prefix}${suffix}`;
      return states[eid] || states[`${eid}_2`] || null;
    }
    // profile_id mode: scan registry-ish via state attributes
    for (const [eid, st] of Object.entries(states)) {
      if (eid.startsWith('sensor.apple_tv_mgmt_') && eid.endsWith(suffix)) {
        return st;
      }
    }
    return null;
  }

  _switchEntity(key) {
    const states = this.hass?.states || {};
    for (const [eid, st] of Object.entries(states)) {
      if (eid.startsWith('switch.apple_tv_mgmt_') && eid.endsWith(key)) {
        return st;
      }
    }
    return null;
  }

  _profileId() {
    return this.config.profile_id || null;
  }

  // ---------- REST helpers ----------

  async _api(path, opts = {}) {
    const url = `/api/appletv_mgmt${path}`;
    const init = {
      ...opts,
      headers: {
        'Content-Type': 'application/json',
        // HA's frontend auto-attaches Authorization to /api/* fetches via the
        // service worker — but we set it explicitly for safety in case the
        // dashboard is opened in an iframe / external embed.
        ...(this.hass?.auth?.data?.access_token
          ? { Authorization: `Bearer ${this.hass.auth.data.access_token}` }
          : {}),
      },
    };
    const r = await fetch(url, init);
    if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
    return r.json();
  }

  async _refreshRequests() {
    if (Date.now() - this._lastFetchAt < (this.config.poll_requests_sec * 1000) / 2) return;
    this._lastFetchAt = Date.now();
    const pid = this._profileId();
    if (!pid) return;
    try {
      this._requests = await this._api(`/profiles/${pid}/requests?status=pending`);
    } catch (e) {
      console.warn('appletv-mgmt-control-card: requests fetch failed', e);
    }
  }

  updated() {
    // Trigger initial fetch when hass first lands + on interval.
    if (this.hass && !this._pollHandle) {
      this._refreshRequests();
      this._pollHandle = setInterval(
        () => this._refreshRequests(),
        Math.max(5, this.config.poll_requests_sec) * 1000,
      );
    }
  }

  disconnectedCallback() {
    super.disconnectedCallback();
    if (this._pollHandle) clearInterval(this._pollHandle);
    this._pollHandle = null;
  }

  // ---------- actions ----------

  async _toggleAdultMode(turnOn) {
    const sw = this._switchEntity('_adult_mode');
    if (!sw) return;
    await this.hass.callService('switch', turnOn ? 'turn_on' : 'turn_off', { entity_id: sw.entity_id });
  }

  // v0.15.0 — adult mode "until" (PO D5 expanded to the Lovelace card).
  // Opens an inline datetime-local input; on submit converts to UTC ISO
  // with Z suffix (matches the integration's §3.2.1 validator contract).
  _openAdultUntilPicker() {
    this._adultUntilPickerOpen = true;
    // Default to now + 2h.
    const d = new Date(Date.now() + 2 * 60 * 60 * 1000);
    const pad = (n) => String(n).padStart(2, '0');
    this._adultUntilValue =
      `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
    this.requestUpdate();
  }

  _closeAdultUntilPicker() {
    this._adultUntilPickerOpen = false;
    this.requestUpdate();
  }

  async _submitAdultUntil(e) {
    e?.preventDefault?.();
    const localStr = this._adultUntilValue;
    if (!localStr) return;
    let isoUtc;
    try {
      isoUtc = new Date(localStr).toISOString();
    } catch {
      alert('Invalid date');
      return;
    }
    const pid = this._profileId();
    if (!pid) return;
    try {
      await this._api(`/profiles/${pid}/adult_mode`, {
        method: 'POST',
        body: JSON.stringify({ until: isoUtc }),
      });
      this._closeAdultUntilPicker();
    } catch (err) {
      console.warn('adult_mode until POST failed', err);
      alert(`Couldn't enable adult mode: ${err?.message || err}`);
    }
  }

  async _grantExtension(minutes) {
    const pid = this._profileId();
    if (!pid) return;
    await this.hass.callService('appletv_mgmt', 'grant_extension', { profile_id: pid, minutes });
  }

  async _forceBlock() {
    const pid = this._profileId();
    if (!pid) return;
    if (!confirm('Block the Apple TV now (force enforcement)?')) return;
    await this.hass.callService('appletv_mgmt', 'force_block', { profile_id: pid });
  }

  async _resetUsage() {
    const pid = this._profileId();
    if (!pid) return;
    if (!confirm("Wipe today's extension pool and unblock the Apple TV?")) return;
    await this.hass.callService('appletv_mgmt', 'reset_usage', { profile_id: pid });
  }

  async _decideRequest(reqId, approve, minutes) {
    try {
      await this._api(`/requests/${reqId}/decide`, {
        method: 'POST',
        body: JSON.stringify(minutes !== undefined ? { approve, minutes } : { approve }),
      });
      await this._refreshRequests();
    } catch (e) {
      console.warn('decide failed', e);
    }
  }

  // ---------- helpers ----------

  _formatRemaining(seconds) {
    if (seconds == null || seconds <= 0) return '0';
    const m = Math.floor(seconds / 60);
    const s = Math.floor(seconds % 60);
    if (m < 60) return `${m}m ${s.toString().padStart(2, '0')}s`;
    const h = Math.floor(m / 60);
    return `${h}h ${(m % 60).toString().padStart(2, '0')}m`;
  }

  _adultModeRemainingSec() {
    const sw = this._switchEntity('_adult_mode');
    if (!sw || sw.state !== 'on') return null;
    const until = sw.attributes?.until;
    if (!until) return null;
    return Math.max(0, (new Date(until).getTime() - Date.now()) / 1000);
  }

  _groupRow(group, totalSec, budgetMin) {
    const usedMin = totalSec / 60;
    const budget = budgetMin || 0;
    const pct = budget > 0 ? Math.min(100, (usedMin / budget) * 100) : 0;
    const exceeded = budget > 0 && usedMin > budget;
    return html`
      <div class="group">
        <div class="group-row">
          <span class="group-name">${GROUP_LABELS[group] || group}</span>
          <span class="group-vals">
            ${usedMin.toFixed(1)} / ${budget ? `${budget} min` : '∞'}
            ${exceeded ? html`<span class="over">over</span>` : ''}
          </span>
        </div>
        <div class="bar-bg"><div class="bar ${exceeded ? 'bar-over' : ''}" style="width:${pct}%"></div></div>
      </div>
    `;
  }

  // ---------- render ----------

  render() {
    if (!this.hass) return html``;

    const enforce = this._entity('enforcement_state');
    const current = this._entity('current_app');
    const todayHistory = this._entity('today_s_app_usage') || this._entity('todays_app_usage');
    const adultSwitch = this._switchEntity('_adult_mode');
    const adultRemainingSec = this._adultModeRemainingSec();

    // Per-group totals: prefer the today_history snapshot (always populated)
    // over the disabled-by-default per-group sensors.
    const historyAttrs = todayHistory?.attributes || {};
    const apps = historyAttrs.apps || [];
    // Group totals computed from the apps list (we don't have a per-group
    // attribute on today_history; sum here using known curated mappings).
    // Fall back to the per-group sensor states when present.
    const groupTotals = {};
    GROUPS.forEach((g) => {
      const used = this._entity(`${g}_time_used_today`);
      if (used && used.state !== 'unavailable' && used.state !== 'unknown') {
        groupTotals[g] = parseFloat(used.state) * 60;
      }
    });
    const groupBudgets = {};
    GROUPS.forEach((g) => {
      const rem = this._entity(`${g}_time_remaining_today`);
      const used = this._entity(`${g}_time_used_today`);
      if (rem && used && rem.state !== 'unavailable' && used.state !== 'unavailable') {
        groupBudgets[g] = Math.round(parseFloat(used.state) + parseFloat(rem.state));
      }
    });

    const state = enforce?.state || 'unknown';
    const reason = enforce?.attributes?.active_quiet_window || enforce?.attributes?.enforce_reason;
    const stateClass = `state-${state}`;

    return html`
      <ha-card>
        <div class="header">
          <span class="title">${this.config.title}</span>
          <span class="badge ${stateClass}">${state.toUpperCase()}${reason ? ` · ${reason}` : ''}</span>
        </div>

        ${current
          ? html`<div class="now"><span class="now-label">Now:</span> ${current.state}</div>`
          : ''}

        ${GROUPS.some((g) => groupBudgets[g] != null)
          ? html`
              <div class="groups">
                ${GROUPS.map((g) =>
                  groupBudgets[g] != null ? this._groupRow(g, groupTotals[g] || 0, groupBudgets[g]) : '',
                )}
              </div>
            `
          : html`
              <div class="muted small">
                Enable the per-group sensors in Settings → Devices & Services →
                Apple TV Mgmt → Entities to see per-group budgets here.
              </div>
            `}

        <div class="actions">
          ${adultSwitch
            ? adultRemainingSec
              ? html`
                  <button class="btn btn-warn" @click=${() => this._toggleAdultMode(false)}>
                    Adult mode — ${this._formatRemaining(adultRemainingSec)} left · tap to cancel
                  </button>
                `
              : html`
                  <button class="btn btn-primary" @click=${() => this._toggleAdultMode(true)}>
                    Adult mode (default ${adultSwitch.attributes?.duration_minutes ?? 120} min)
                  </button>
                  <button class="btn btn-link" @click=${() => this._openAdultUntilPicker()}
                          title="Set adult mode until a specific time">
                    Until…
                  </button>
                `
            : ''}
          <button class="btn" @click=${() => this._grantExtension(15)}>+15 min</button>
          <button class="btn" @click=${() => this._grantExtension(-15)}>−15 min</button>
          <button class="btn btn-danger" @click=${() => this._forceBlock()}>Block now</button>
          <button class="btn" @click=${() => this._resetUsage()}>Reset today</button>
        </div>
        ${this._adultUntilPickerOpen
          ? html`
              <form class="adult-until-popover" @submit=${(e) => this._submitAdultUntil(e)}>
                <label>Adult mode until:
                  <input type="datetime-local"
                         .value=${this._adultUntilValue || ''}
                         @input=${(e) => { this._adultUntilValue = e.target.value; }} />
                </label>
                <button type="submit" class="btn btn-primary btn-sm">Enable</button>
                <button type="button" class="btn btn-sm" @click=${() => this._closeAdultUntilPicker()}>Cancel</button>
                <div class="small muted">
                  Converts to UTC client-side. Past times rejected by the integration.
                </div>
              </form>
            `
          : ''}

        <div class="requests">
          <div class="requests-header">
            Pending requests <span class="muted">(${this._requests.length})</span>
          </div>
          ${this._requests.length === 0
            ? html`<div class="muted small">None — kids haven't asked for more time.</div>`
            : this._requests.map(
                (r) => html`
                  <div class="request">
                    <div class="req-line1">
                      <strong>+${r.requested_minutes} min</strong>
                      ${r.bundle_id ? html`<span class="muted"> on ${r.bundle_id}</span>` : ''}
                    </div>
                    ${r.reason ? html`<div class="req-reason">"${r.reason}"</div>` : ''}
                    <div class="req-actions">
                      <button class="btn btn-primary" @click=${() => this._decideRequest(r.id, true)}>
                        Approve ${r.requested_minutes}
                      </button>
                      ${r.requested_minutes > 1
                        ? html`
                            <button
                              class="btn"
                              @click=${() => this._decideRequest(r.id, true, Math.floor(r.requested_minutes / 2))}
                            >
                              Approve ${Math.floor(r.requested_minutes / 2)}
                            </button>
                          `
                        : ''}
                      <button class="btn btn-danger" @click=${() => this._decideRequest(r.id, false)}>
                        Deny
                      </button>
                    </div>
                  </div>
                `,
              )}
        </div>
      </ha-card>
    `;
  }

  static get styles() {
    return css`
      ha-card { padding: 16px; }
      .header { display: flex; align-items: baseline; justify-content: space-between; margin-bottom: 8px; }
      .title { font-size: 1.15em; font-weight: 600; }
      .badge { font-size: 0.75em; padding: 3px 8px; border-radius: 4px; background: var(--divider-color); }
      .state-ok       { background: rgba(76,175,80, 0.20); color: #2e7d32; }
      .state-warning  { background: rgba(255,193,7, 0.20); color: #b28704; }
      .state-grace    { background: rgba(255,152,0, 0.20); color: #c66800; }
      .state-enforcing{ background: rgba(244,67,54, 0.18); color: #c62828; }
      .now { font-size: 0.95em; margin-bottom: 12px; }
      .now-label { color: var(--secondary-text-color); margin-right: 4px; }
      .muted { color: var(--secondary-text-color); }
      .small { font-size: 0.85em; }

      .groups { display: flex; flex-direction: column; gap: 8px; margin: 8px 0 16px; }
      .group-row { display: flex; justify-content: space-between; align-items: baseline; font-size: 0.9em; }
      .group-name { font-weight: 500; }
      .group-vals { font-variant-numeric: tabular-nums; }
      .over { color: #c62828; margin-left: 6px; font-size: 0.85em; font-weight: 500; }
      .bar-bg { height: 6px; background: var(--divider-color); border-radius: 3px; overflow: hidden; margin-top: 3px; }
      .bar { height: 100%; background: var(--primary-color); border-radius: 3px; transition: width 0.3s ease; }
      .bar-over { background: #e53935; }

      .actions { display: flex; flex-wrap: wrap; gap: 6px; margin: 12px 0; }
      .btn {
        font-family: inherit; font-size: 0.85em; padding: 7px 12px;
        border-radius: 6px; border: 1px solid var(--divider-color);
        background: var(--card-background-color); color: var(--primary-text-color);
        cursor: pointer;
      }
      .btn:hover { background: var(--divider-color); }
      .btn-primary { background: var(--primary-color); color: var(--text-primary-color); border-color: var(--primary-color); }
      .btn-primary:hover { filter: brightness(1.1); }
      .btn-danger  { color: #c62828; }
      .btn-warn    { background: rgba(255,193,7, 0.15); border-color: #ffc107; color: #b28704; }

      .requests { border-top: 1px solid var(--divider-color); padding-top: 12px; }
      .requests-header { font-weight: 500; margin-bottom: 6px; }
      .request { padding: 8px 10px; margin: 6px 0; background: var(--secondary-background-color); border-radius: 6px; }
      .req-line1 { font-size: 0.95em; }
      .req-reason { font-style: italic; color: var(--secondary-text-color); font-size: 0.85em; margin: 2px 0 6px; }
      .req-actions { display: flex; gap: 6px; flex-wrap: wrap; }
    `;
  }
}

customElements.define('appletv-mgmt-control-card', AppleTVMgmtControlCard);

window.customCards = window.customCards || [];
window.customCards.push({
  type: 'appletv-mgmt-control-card',
  name: 'Apple TV Mgmt — Control',
  description: 'Per-group budgets, adult mode, pending requests, quick actions for the appletv_mgmt integration.',
});

console.info('%c appletv-mgmt-control-card %c 0.1.0 ', 'color:#fff;background:#1976d2;padding:2px 4px;border-radius:3px', 'color:#1976d2');
